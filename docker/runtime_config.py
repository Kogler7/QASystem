"""Single reader for persisted, non-secret application configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path

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

LEGACY_MODEL_KEYS = frozenset({"LLM_MODEL_DIR", "LLM_SERVED_MODEL_NAME", "LLM_GPU_DEVICE_IDS",
    "LLM_BIND", "LLM_HOST_PORT", "LLM_GPU_MEMORY_UTILIZATION", "LLM_MAX_MODEL_LEN"})


def runtime_path(root: Path | None = None, env=None) -> Path:
    env = os.environ if env is None else env
    root = Path.cwd() if root is None else Path(root)
    explicit = env.get("QASYSTEM_RUNTIME_SETTINGS_FILE")
    directory = env.get("QASYSTEM_RUNTIME_CONFIG_DIR")
    path = Path(explicit) if explicit else Path("/app/runtime-config/config.json") if Path("/app/runtime-config").is_dir() else (
        Path(directory) / "config.json" if directory else Path("runtime-config/config.json"))
    return path if path.is_absolute() else root / path


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ValueError("Invalid runtime configuration file") from None


def read_document(path: Path | None = None, *, root: Path | None = None, env=None) -> dict:
    path = runtime_path(root, env) if path is None else Path(path)
    data = _json(path)
    if data is None and path.name == "config.json":
        legacy = _json(path.with_name("settings.json"))
        models = _json(path.with_name("models.json"))
        data = {"settings": {} if legacy is None else legacy,
                "models": {} if models is None else models}
    elif data is None:
        data = {"settings": {}, "models": {}}
    elif path.name != "config.json" and isinstance(data, dict) and "settings" not in data:
        data = {"settings": data, "models": {}}
    if not isinstance(data, dict) or set(data) - {"settings", "models"}:
        raise ValueError("Invalid runtime configuration structure")
    settings, models = data.get("settings", {}), data.get("models", {})
    if not isinstance(settings, dict) or any(k not in HOT_KEYS or not isinstance(v, str) for k, v in settings.items()):
        raise ValueError("Invalid runtime settings keys or values")
    if not isinstance(models, dict) or any(not isinstance(m, dict) for m in models.values()):
        raise ValueError("Invalid runtime model configuration")
    return {"settings": settings, "models": models}


def read_settings(path: Path | None = None, *, root: Path | None = None, env=None) -> dict[str, str]:
    return read_document(path, root=root, env=env)["settings"]
