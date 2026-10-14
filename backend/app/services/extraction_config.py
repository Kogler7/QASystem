"""Request-scoped extraction configuration; snapshots never contain credentials."""
from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv, UndeclaredConfigurationError

from copy import deepcopy
import importlib
import json
import os
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qasystem_config.configuration import getenv
from algorithm.knowledge_extract.version import SUPPORTED_VERSIONS, get_extract_version
from external_services.model_routes import registry_snapshot, resolve_service, snapshot_entity_service


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_url: str
    api_key_env: str = Field(pattern=r"^(?:[A-Z][A-Z0-9_]*)?$")
    model: str = Field(min_length=1)
    quota_group: str = Field(min_length=1)
    max_concurrency: int = Field(default=4, ge=1)
    qps: int = Field(default=0, ge=0)
    qpm: int = Field(default=0, ge=0)
    max_tokens: int = Field(default=16384, ge=1)
    max_input_tokens: int = Field(default=65536, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    request_timeout: float = Field(default=300, gt=0, allow_inf_nan=False)
    disable_thinking: bool = True
    api_mode: Literal["auto", "chat"] = "auto"
    thinking_parameter: Literal["thinking", "enable_thinking", "none"] = "thinking"
    response_format: Literal["auto", "json_object"] = "auto"

    @model_validator(mode="after")
    def resolve_output_budget(self):
        if self.max_output_tokens is not None:
            if "max_tokens" in self.model_fields_set and self.max_tokens != self.max_output_tokens:
                raise ValueError("Conflicting output token limits")
            self.max_tokens = self.max_output_tokens
        self.max_output_tokens = self.max_tokens
        return self


OVERRIDE_FIELDS = ("version", "provider", "request_timeout", "document_timeout")


class ExtractionConfigurationError(ValueError):
    """Safe, public configuration error; never includes profile contents."""


DOCUMENT_TIMEOUT_FLOOR = 900.0
DOCUMENT_TIMEOUT_CEILING = 2400.0
DOCUMENT_TIMEOUT_BASE_SECONDS = 300.0
DOCUMENT_TIMEOUT_PER_CHUNK_SECONDS = 240.0
DOCUMENT_TIMEOUT_PER_1K_CHARS_SECONDS = 90.0
DEFAULT_CHUNK_CHARACTERS = 4000


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


def resolve_config(options: dict[str, Any], *, registry: tuple[dict, Any] | None = None, document_scale: tuple[int, int] | None = None) -> dict[str, Any]:
    """Resolve policy and registered service once; no legacy connection fallback."""
    document, revision = registry if registry is not None else registry_snapshot()
    hot = document.get('settings', {})
    def setting(key, default=None):
        return hot[key] if key in hot else config_getenv(key, default)
    version = options.get('version') or str(setting('KG_EXTRACT_ALGO_VERSION', 'latest')).strip().lower()
    if version not in SUPPORTED_VERSIONS:
        raise ValueError('Unsupported extraction version')
    if version == 'jev' and has_overrides(options):
        raise ValueError('Request configuration overrides do not support jev')
    package = {'v2.1': 'v2_1', 'v2.2': 'v2_2', 'v2.2-v31': 'v2_2_v31', 'v3.1': 'v3_1', 'jev': 'v2'}.get(version, version)
    defaults = importlib.import_module(f'algorithm.knowledge_extract.{package}.config').get_llm_policy()
    provider = options.get('provider') or 'default'
    routed = resolve_service('extract', registry=document, revision=revision,
                             service_id=None if provider == 'default' else provider)
    if routed is None:
        raise ValueError('知识抽取未选择已登记的模型服务；请使用 ops 配置抽取用途')
    values = {key: defaults[key] for key in ('max_tokens', 'request_timeout', 'disable_thinking')}
    values.update(base_url=routed['base_url'], model=routed['model'], api_key_env=routed['api_key_env'],
                  quota_group=routed.get('account_id') or routed['service_id'],
                  api_mode='auto', thinking_parameter='thinking')
    from qasystem_config.api_services import LIMIT_DEFAULTS, validate_limits
    validate_limits(routed.get('limits') or LIMIT_DEFAULTS, required=True)
    values.update({key: value for key, value in (routed.get('limits') or LIMIT_DEFAULTS).items()
                   if key in {'max_concurrency', 'qps', 'qpm'}})
    values['max_input_tokens'] = int(setting('KG_EXTRACT_LLM_MAX_INPUT_TOKENS', '65536'))
    values['max_output_tokens'] = int(setting('KG_EXTRACT_LLM_MAX_OUTPUT_TOKENS') or values['max_tokens'])
    values['max_tokens'] = values['max_output_tokens']
    capabilities = routed.get('capabilities') or {}
    if capabilities.get('max_output_tokens'):
        values['max_tokens'] = values['max_output_tokens'] = min(values['max_output_tokens'], capabilities['max_output_tokens'])
    if capabilities.get('context_tokens'):
        available = capabilities['context_tokens'] - values['max_output_tokens']
        if available < 1:
            raise ValueError('Registered context leaves no extraction input capacity')
        values['max_input_tokens'] = min(values['max_input_tokens'], available)
    for field, value in (routed.get('capabilities') or {}).items():
        if field in {'api_mode', 'thinking_parameter', 'response_format'}:
            values[field] = value
    if routed['kind'] == 'api' and (not routed['api_key_env'] or not setting(routed['api_key_env'], '').strip()):
        raise ValueError('Extraction service credential is not configured')
    if version == 'v3.1':
        values['request_timeout'] = float(setting('KG_EXTRACT_V31_REQUEST_TIMEOUT', '120'))
        document_timeout = resolve_document_timeout(setting('KG_EXTRACT_V31_DOCUMENT_TIMEOUT'), document_scale)
    else:
        document_timeout = None
    values = ProviderProfile.model_validate(values).model_dump()
    if version == 'v3.1':
        for key in ('semantic_thinking', 'stream_generation', 'verification_max_tokens', 'fast_verification'):
            values[key] = defaults[key]
    values.update(version=version, provider=provider, document_timeout=document_timeout,
                  model_service=routed['service_id'], config_revision=revision,
                  credential_required=routed['kind']=='api', service_snapshot=routed)
    for key in ('model', 'request_timeout', 'document_timeout'):
        if options.get(key) is not None:
            values[key] = options[key]
    return values


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    result = {key: config.get(key) for key in (
        "version", "provider", "model", "request_timeout", "document_timeout",
        "max_tokens", "max_input_tokens", "max_output_tokens", "disable_thinking",
        "api_mode", "thinking_parameter", "response_format",
    )}
    if config.get("version") == "v3.1":
        for key in ("semantic_thinking", "stream_generation", "verification_max_tokens", "fast_verification"):
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
