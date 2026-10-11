"""Request-scoped extraction configuration; snapshots never contain credentials."""
from __future__ import annotations

from copy import deepcopy
import importlib
import json
import os
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from algorithm.knowledge_extract.version import SUPPORTED_VERSIONS, get_extract_version


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_url: str
    api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    model: str = Field(min_length=1)
    quota_group: str = Field(min_length=1)
    max_concurrency: int = Field(default=4, ge=1)
    qps: int = Field(default=0, ge=0)
    qpm: int = Field(default=0, ge=0)
    max_tokens: int = Field(default=16384, ge=1)
    request_timeout: float = Field(default=300, gt=0, allow_inf_nan=False)
    disable_thinking: bool = True
    api_mode: Literal["auto", "chat"] = "auto"
    thinking_parameter: Literal["thinking", "enable_thinking", "none"] = "thinking"


OVERRIDE_FIELDS = ("version", "provider", "request_timeout", "document_timeout")


class ExtractionConfigurationError(ValueError):
    """Safe, public configuration error; never includes profile contents."""


def has_overrides(options: dict[str, Any]) -> bool:
    return any(options.get(key) is not None for key in OVERRIDE_FIELDS)


def _credential_env() -> str:
    return next((name for name in ("KG_EXTRACT_LLM_API_KEY", "KG_EXTRACT_API_KEY")
                 if os.getenv(name, "").strip()), "KG_EXTRACT_LLM_API_KEY")


def resolve_config(options: dict[str, Any]) -> dict[str, Any]:
    """Resolve non-secret defaults once at submission, not again on retry."""
    version = options.get("version") or get_extract_version()
    if version not in SUPPORTED_VERSIONS:
        raise ValueError("Unsupported extraction version")
    if version == "jev" and has_overrides(options):
        raise ValueError("Request configuration overrides do not support jev")
    package = "v2" if version in {"v3.1", "jev"} else version
    defaults = importlib.import_module(f"algorithm.knowledge_extract.{package}.config").get_llm_config()
    provider = options.get("provider") or "default"
    if provider == "default":
        values = {key: defaults[key] for key in (
            "base_url", "model", "max_tokens", "request_timeout", "disable_thinking",
            "max_concurrency", "qps", "qpm",
        )}
        values.update(api_key_env=_credential_env(),
                      quota_group=os.getenv("KG_EXTRACT_LLM_QUOTA_GROUP", "default"),
                      api_mode="auto", thinking_parameter="thinking")
    else:
        try:
            profiles = json.loads(os.getenv("KG_EXTRACT_PROVIDERS_JSON", "{}"))
            values = ProviderProfile.model_validate(profiles[provider]).model_dump()
        except Exception:
            # Pydantic/JSON errors may echo configuration values. Never expose them.
            raise ValueError("Unknown or invalid extraction provider profile") from None
        url = urlsplit(values["base_url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Provider base_url must be an HTTP(S) URL without credentials or query")
        if not os.getenv(values["api_key_env"], "").strip():
            raise ValueError("Extraction provider credential is not configured")
    if version == "v3.1":
        if provider == "default":
            values["request_timeout"] = float(os.getenv("KG_EXTRACT_V31_REQUEST_TIMEOUT", "120"))
            from algorithm.knowledge_extract.v3_1.llm_client import thinking_disabled
            values["disable_thinking"] = thinking_disabled()
        document_timeout = float(os.getenv("KG_EXTRACT_V31_DOCUMENT_TIMEOUT", "900"))
    else:
        document_timeout = None
    values.update(version=version, provider=provider, document_timeout=document_timeout)
    for key in ("model", "request_timeout", "document_timeout"):
        if options.get(key) is not None:
            values[key] = options[key]
    return values


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    return {key: config.get(key) for key in (
        "version", "provider", "model", "request_timeout", "document_timeout",
        "max_tokens", "disable_thinking",
        "api_mode", "thinking_parameter",
    )}


def snapshot_payload(operation: str, payload: Any) -> Any:
    if not isinstance(payload, dict) or operation not in {"graph.extract", "knowledge.ingestion"}:
        return payload
    result = deepcopy(payload)
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
        result["__extraction_config"] = resolve_config(options)
    except ValueError:
        raise ExtractionConfigurationError("Invalid extraction configuration or unavailable provider profile") from None
    return result
