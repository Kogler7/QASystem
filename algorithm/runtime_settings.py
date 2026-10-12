"""Non-secret, request-scoped runtime settings from a directory mount."""
from __future__ import annotations

import json
import os
from pathlib import Path

# Everything else, especially credentials and process topology, stays in .env.
HOT_KEYS = frozenset({
    "KG_EXTRACT_ALGO_VERSION", "KG_EXTRACT_LLM_BASE_URL", "KG_EXTRACT_LLM_MODEL",
    "KG_EXTRACT_LLM_MAX_TOKENS", "KG_EXTRACT_LLM_MAX_INPUT_TOKENS",
    "KG_EXTRACT_LLM_MAX_OUTPUT_TOKENS", "KG_EXTRACT_LLM_MAX_CONCURRENCY",
    "KG_EXTRACT_LLM_QPS", "KG_EXTRACT_LLM_QPM", "KG_EXTRACT_LLM_TIMEOUT_SECONDS",
    "KG_EXTRACT_LLM_DISABLE_THINKING", "KG_EXTRACT_LLM_QUOTA_GROUP",
    "KG_EXTRACT_V31_REQUEST_TIMEOUT", "KG_EXTRACT_V31_DOCUMENT_TIMEOUT",
    "QASYSTEM_JUDGE_BASE_URL", "QASYSTEM_JUDGE_MODEL", "QASYSTEM_JUDGE_ADAPTER",
    "QASYSTEM_JUDGE_MAX_TOKENS",
})


def read_settings() -> dict[str, str]:
    path = Path(os.getenv("QASYSTEM_RUNTIME_SETTINGS_FILE", "/app/runtime-config/settings.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ValueError("Invalid runtime settings file") from None
    if not isinstance(data, dict) or any(k not in HOT_KEYS or not isinstance(v, str) for k, v in data.items()):
        raise ValueError("Invalid runtime settings keys or values")
    return data


def getenv(key: str, default: str | None = None) -> str | None:
    if key in HOT_KEYS:
        values = read_settings()
        if key in values:
            return values[key]
    return os.getenv(key, default)
