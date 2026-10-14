"""Request-scoped extraction configuration; snapshots never contain credentials."""
from __future__ import annotations
from docker.configuration import getenv as config_getenv, UndeclaredConfigurationError

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


def has_overrides(options: dict[str, Any]) -> bool:
    return any(options.get(key) is not None for key in OVERRIDE_FIELDS)


def _credential_env() -> str:
    return "KG_EXTRACT_LLM_API_KEY"


def resolve_config(options: dict[str, Any], *, registry: tuple[dict, Any] | None = None) -> dict[str, Any]:
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
    defaults = importlib.import_module(f"algorithm.knowledge_extract.{package}.config").get_llm_config()
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
        if provider == "default":
            values["request_timeout"] = float(setting("KG_EXTRACT_V31_REQUEST_TIMEOUT", "120"))
            values["disable_thinking"] = defaults["disable_thinking"]
        document_timeout = float(setting("KG_EXTRACT_V31_DOCUMENT_TIMEOUT", "900"))
    else:
        document_timeout = None
    checked = ProviderProfile.model_validate(values)
    values = checked.model_dump()
    values.update(version=version, provider=provider, document_timeout=document_timeout)
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
    )}
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
        result["__extraction_config"] = resolve_config(options, registry=registry)
    except (ValueError, UndeclaredConfigurationError):
        raise ExtractionConfigurationError("Invalid extraction configuration or unavailable provider profile") from None
    return result
