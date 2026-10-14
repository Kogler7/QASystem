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


def resolve_config(options: dict[str, Any], *, registry: tuple[dict, Any] | None = None) -> dict[str, Any]:
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
    package = {'v2.1': 'v2_1', 'v2.2': 'v2_2', 'v3.1': 'v3_1', 'jev': 'v2'}.get(version, version)
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
    from docker.api_services import LIMIT_DEFAULTS
    values.update(routed.get('limits') or LIMIT_DEFAULTS)
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
        document_timeout = float(setting('KG_EXTRACT_V31_DOCUMENT_TIMEOUT', '900'))
    else:
        document_timeout = None
    values = ProviderProfile.model_validate(values).model_dump()
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
