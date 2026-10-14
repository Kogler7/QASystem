"""Request-scoped extraction configuration; snapshots never contain credentials."""
from __future__ import annotations
from docker.configuration import getenv as config_getenv, declarations, UndeclaredConfigurationError

from copy import deepcopy
import importlib
import json
import os
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from algorithm.runtime_settings import getenv
from algorithm.knowledge_extract.version import SUPPORTED_VERSIONS, get_extract_version
from external_services.model_routes import registry_snapshot, resolve_service, snapshot_entity_service
from external_services.llm.quota import parse_quota_tiers


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_url: str
    api_key_env: str = Field(pattern=r"^(?:[A-Z][A-Z0-9_]*)?$")
    model: str = Field(min_length=1)
    quota_group: str = Field(min_length=1)
    max_concurrency: int = Field(default=16, ge=1)
    qps: int = Field(default=0, ge=0)
    qpm: int = Field(default=0, ge=0)
    quota_tiers: list[dict[str, int]] | None = None
    max_tokens: int = Field(default=16384, ge=1)
    max_input_tokens: int = Field(default=65536, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    request_timeout: float = Field(default=300, gt=0, allow_inf_nan=False)
    disable_thinking: bool = True
    semantic_thinking: bool | None = None
    api_mode: Literal["auto", "chat"] = "auto"
    thinking_parameter: Literal["thinking", "enable_thinking", "none"] = "thinking"
    response_format: Literal["auto", "json_object"] = "auto"

    @model_validator(mode="after")
    def resolve_output_budget(self):
        if self.quota_tiers is not None:
            self.quota_tiers = [vars(tier) for tier in parse_quota_tiers(self.quota_tiers)]
        if self.max_output_tokens is not None:
            if "max_tokens" in self.model_fields_set and self.max_tokens != self.max_output_tokens:
                raise ValueError("Conflicting output token limits")
            self.max_tokens = self.max_output_tokens
        self.max_output_tokens = self.max_tokens
        return self


OVERRIDE_FIELDS = ("version", "provider", "request_timeout", "document_timeout")

# v3.1 文档总预算（秒）的自适应系数。实测基线（百炼 deepseek-v4-flash，16384 输出
# token 满额）：单次调用约 105–110s；一片切片要走「实体轮 → 校验扇出 → 修复波次」
# 约 3 波。固定 900s 会让中等文档死在半途（official_01 3 片 4.5K 字符，900s 撞线时
# 仍有分片在重试），所以默认值按输入规模自适应；KG_EXTRACT_V31_DOCUMENT_TIMEOUT
# 与请求级 document_timeout 仍是显式覆盖。
DOCUMENT_TIMEOUT_FLOOR = 900.0
DOCUMENT_TIMEOUT_CEILING = 2400.0
DOCUMENT_TIMEOUT_BASE_SECONDS = 300.0
DOCUMENT_TIMEOUT_PER_CHUNK_SECONDS = 240.0
DOCUMENT_TIMEOUT_PER_1K_CHARS_SECONDS = 90.0
DEFAULT_CHUNK_CHARACTERS = 4000


class ExtractionConfigurationError(ValueError):
    """Safe, public configuration error; never includes profile contents."""


def has_overrides(options: dict[str, Any]) -> bool:
    return any(options.get(key) is not None for key in OVERRIDE_FIELDS)


def adaptive_document_timeout(characters: int, chunks: int) -> float:
    """按输入规模估算 v3.1 文档预算，结果落在 [FLOOR, CEILING] 内。"""
    estimate = (DOCUMENT_TIMEOUT_BASE_SECONDS
                + DOCUMENT_TIMEOUT_PER_1K_CHARS_SECONDS * (max(0, characters) / 1000.0)
                + DOCUMENT_TIMEOUT_PER_CHUNK_SECONDS * max(0, chunks))
    return float(min(max(estimate, DOCUMENT_TIMEOUT_FLOOR), DOCUMENT_TIMEOUT_CEILING))


def resolve_document_timeout(configured: Any, scale: tuple[int, int] | None) -> float:
    """显式配置 > 按输入规模自适应 > 拿不到规模时退回单片默认。"""
    if configured is not None and str(configured).strip():
        return float(configured)
    return adaptive_document_timeout(*scale) if scale else DOCUMENT_TIMEOUT_FLOOR


def _content_characters(content: Any) -> int:
    """原文长度：文本按字符数，二进制（PDF 等）退化为字节上界。"""
    if content is None:
        return 0
    if isinstance(content, (bytes, bytearray)):
        try:
            return len(bytes(content).decode("utf-8"))
        except UnicodeDecodeError:
            return len(content)
    return len(str(content))


def document_scale(operation: str, payload: dict[str, Any]) -> tuple[int, int] | None:
    """(总字符数, 片数)。graph.extract 的 items 就是切片，取值精确；
    knowledge.ingestion 在提交时只有原文，按请求自带的切片尺寸估算片数。"""
    if operation == "graph.extract":
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return None
        characters = sum(_content_characters(item.get("text")) for item in items if isinstance(item, dict))
        return characters, len(items)
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return None
    characters = sum(_content_characters(entry.get("content")) for entry in files if isinstance(entry, dict))
    size = DEFAULT_CHUNK_CHARACTERS
    chunking = (payload.get("options") or {}).get("chunking")
    if isinstance(chunking, dict) and isinstance(chunking.get("size"), int) and chunking["size"] > 0:
        size = chunking["size"]
    return characters, max(1, -(-characters // size))


def _credential_env() -> str:
    return "KG_EXTRACT_LLM_API_KEY"


def resolve_config(options: dict[str, Any], *, registry: tuple[dict, Any] | None = None,
                   document_scale: tuple[int, int] | None = None) -> dict[str, Any]:
    """Resolve non-secret defaults once at submission, not again on retry."""
    document, revision = registry if registry is not None else registry_snapshot()
    hot = document.get('settings', {})
    def setting(key, default=None):
        return hot.get(key, config_getenv(key, default))
    version = options.get("version") or str(setting("KG_EXTRACT_ALGO_VERSION", "latest")).strip().lower()
    if version not in SUPPORTED_VERSIONS:
        raise ValueError("Unsupported extraction version")
    if version == "jev" and has_overrides(options):
        raise ValueError("Request configuration overrides do not support jev")
    package = {"v2.1": "v2_1", "v2.2": "v2_2", "v3.1": "v3_1", "jev": "v2"}.get(version, version)
    config_module = importlib.import_module(f"algorithm.knowledge_extract.{package}.config")
    defaults = config_module.get_llm_config(**({"include_quota": False, "include_thinking": False} if version == "v3.1" else {}))
    # Snapshot once: retries retain the submitted configuration.
    casts = {"BASE_URL": ("base_url", str), "MODEL": ("model", str),
             "MAX_TOKENS": ("max_tokens", int), "MAX_CONCURRENCY": ("max_concurrency", int),
             "QPS": ("qps", int), "QPM": ("qpm", int),
             "TIMEOUT_SECONDS": ("request_timeout", float),
             "DISABLE_THINKING": ("disable_thinking", lambda v: v.lower() not in {"0", "false", "no"})}
    for suffix, (field, cast) in casts.items():
        key = "KG_EXTRACT_LLM_" + suffix
        if key in hot:
            defaults[field] = cast(hot[key])
    provider = options.get("provider") or "default"
    quota_tiers = [vars(tier) for tier in parse_quota_tiers(setting(
        "KG_EXTRACT_LLM_QUOTA_TIERS_JSON",
        declarations()["items"]["KG_EXTRACT_LLM_QUOTA_TIERS_JSON"]["default"]))]
    routed = resolve_service('extract', registry=document, revision=revision) if provider == 'default' else None
    if provider == "default":
        values = {key: defaults[key] for key in (
            "base_url", "model", "max_tokens", "request_timeout", "disable_thinking",
            "max_concurrency", "qps", "qpm",
        )}
        values.update(api_key_env=_credential_env(),
                      quota_group=setting("KG_EXTRACT_LLM_QUOTA_GROUP", "default"),
                      api_mode="auto", thinking_parameter="thinking")
        values["max_input_tokens"] = int(setting("KG_EXTRACT_LLM_MAX_INPUT_TOKENS", "65536"))
        values["max_output_tokens"] = int(setting("KG_EXTRACT_LLM_MAX_OUTPUT_TOKENS") or values["max_tokens"])
        values["max_tokens"] = values["max_output_tokens"]
        if values["max_input_tokens"] < 1 or values["max_output_tokens"] < 1:
            raise ValueError("Token limits must be positive")
        if routed:
            values.update(base_url=routed['base_url'], model=routed['model'],
                          api_key_env=routed['api_key_env'])
            if routed['kind'] == 'api' and not setting(routed['api_key_env'], '').strip():
                raise ValueError('Extraction service credential is not configured')
    else:
        try:
            profiles = json.loads(setting("KG_EXTRACT_PROVIDERS_JSON", "{}"))
            values = ProviderProfile.model_validate(profiles[provider]).model_dump()
        except Exception:
            # Pydantic/JSON errors may echo configuration values. Never expose them.
            raise ValueError("Unknown or invalid extraction provider profile") from None
        url = urlsplit(values["base_url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Provider base_url must be an HTTP(S) URL without credentials or query")
        if not values['api_key_env'] or not setting(values["api_key_env"], "").strip():
            raise ValueError("Extraction provider credential is not configured")
    if version == "v3.1":
        if values.get("semantic_thinking") is None:
            values["semantic_thinking"] = config_module.parse_semantic_thinking(setting(
                "KG_EXTRACT_V31_SEMANTIC_THINKING",
                declarations()["items"]["KG_EXTRACT_V31_SEMANTIC_THINKING"]["default"]))
        if provider == "default":
            values["request_timeout"] = float(setting("KG_EXTRACT_V31_REQUEST_TIMEOUT", "120"))
            values["disable_thinking"] = defaults["disable_thinking"]
        document_timeout = resolve_document_timeout(
            setting("KG_EXTRACT_V31_DOCUMENT_TIMEOUT"), document_scale)
    else:
        document_timeout = None
    checked = ProviderProfile.model_validate(values)
    values = checked.model_dump()
    if values["quota_tiers"] is None:
        values["quota_tiers"] = quota_tiers
    values.update(version=version, provider=provider, document_timeout=document_timeout)
    if version == 'v3.1':
        for key in ('stream_generation', 'verification_max_tokens', 'fast_verification'):
            values[key] = defaults[key]
    if routed:
        values.update(model_service=routed['service_id'], config_revision=revision,
                      credential_required=routed['kind'] == 'api')
    for key in ("model", "request_timeout", "document_timeout"):
        if options.get(key) is not None:
            values[key] = options[key]
    return values


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    result = {key: config.get(key) for key in (
        "version", "provider", "model", "request_timeout", "document_timeout",
        "max_tokens", "max_input_tokens", "max_output_tokens", "disable_thinking",
        "api_mode", "thinking_parameter", "response_format",
        "max_concurrency", "qps", "qpm", "quota_tiers",
    )}
    if config.get("version") == "v3.1":
        result["semantic_thinking"] = config.get("semantic_thinking", False)
        for key in ('stream_generation', 'verification_max_tokens', 'fast_verification'):
            result[key] = config.get(key)
    if config.get("version") in {"v0", "v2", "v2.1", "v3", "latest"}:
        # Provider profiles can retain legacy preferences, but extraction no
        # longer sends a response_format parameter on the wire.
        result.update(response_format=None, output_format="jsonl", api_mode="chat")
    return result


def snapshot_payload(operation: str, payload: Any) -> Any:
    if not isinstance(payload, dict) or operation not in {"graph.extract", "knowledge.ingestion", "document.chunk"}:
        return payload
    result = deepcopy(payload)
    registry = registry_snapshot()
    result.pop('__entity_count_service', None)
    chunk_options = (result.get('options') or {})
    if operation == 'knowledge.ingestion':
        chunk_options = chunk_options.get('chunking') or {}
    if operation in {'knowledge.ingestion', 'document.chunk'} and chunk_options.get('adaptive_entity_count'):
        service = snapshot_entity_service(chunk_options.get('entity_count_model', 'clef-flash'),
            explicit='entity_count_model' in chunk_options, registry=registry[0], revision=registry[1])
        result['__entity_count_service'] = service or {}
    if operation == 'document.chunk':
        return result
    options = result.get("options") or {}
    scale = document_scale(operation, result)
    if operation == "knowledge.ingestion":
        options = options.get("extraction") or {}
    from backend.app.schemas import ExtractionOptions

    try:
        options = ExtractionOptions.model_validate(options).model_dump()
    except Exception:
        raise ExtractionConfigurationError("Invalid extraction options") from None
    if not str(result.get("ontology") or "").strip():
        if has_overrides(options):
            raise ExtractionConfigurationError("Extraction configuration overrides require an ontology")
        return result
    # Always replace caller-supplied internal data at the submission boundary.
    try:
        result["__extraction_config"] = resolve_config(options, registry=registry, document_scale=scale)
    except (ValueError, UndeclaredConfigurationError):
        raise ExtractionConfigurationError("Invalid extraction configuration or unavailable provider profile") from None
    return result
