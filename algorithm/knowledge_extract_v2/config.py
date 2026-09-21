"""知识抽取 V2 的 LLM / Jev 环境配置。"""
import os


THINKING_ELIGIBLE_PASSES = frozenset({"relation", "repair_relation"})
DEFAULT_THINKING_PASSES = "relation,repair_relation"


def _env(key: str, *fallback_keys: str, default: str = "") -> str:
    for k in (key, *fallback_keys):
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    return default


def _csv_set(value: str) -> frozenset[str]:
    return frozenset(item.strip().casefold() for item in value.split(",") if item.strip())


KG_EXTRACT_API_URL = _env("KG_EXTRACT_LLM_BASE_URL", "KG_EXTRACT_API_URL", default="http://localhost:9999")
KG_EXTRACT_API_KEY = _env("KG_EXTRACT_LLM_API_KEY", "KG_EXTRACT_API_KEY", default="")
KG_EXTRACT_MODEL = _env("KG_EXTRACT_LLM_MODEL", "KG_EXTRACT_MODEL", default="deepseek-v4-flash")
KG_EXTRACT_MAX_TOKENS = int(_env("KG_EXTRACT_LLM_MAX_TOKENS", "KG_EXTRACT_MAX_TOKENS", default="16384"))
KG_EXTRACT_MAX_CONCURRENCY = int(_env("KG_EXTRACT_LLM_MAX_CONCURRENCY", default="4"))
KG_EXTRACT_QPS = int(_env("KG_EXTRACT_LLM_QPS", default="0"))
KG_EXTRACT_QPM = int(_env("KG_EXTRACT_LLM_QPM", default="0"))
KG_EXTRACT_REQUEST_TIMEOUT = float(_env("KG_EXTRACT_LLM_TIMEOUT_SECONDS", default="300"))
# 默认关闭 thinking（与 agent 部分一致）；设置 "0" 可按 THINKING_PASSES 开启
KG_EXTRACT_DISABLE_THINKING = _env("KG_EXTRACT_LLM_DISABLE_THINKING", default="1") != "0"


def thinking_passes_from_env() -> frozenset[str]:
    """Allowlisted passes that may enable thinking when the global kill switch is off."""
    return _csv_set(_env("KG_EXTRACT_THINKING_PASSES", default=DEFAULT_THINKING_PASSES)) & THINKING_ELIGIBLE_PASSES


def get_llm_config() -> dict:
    disable_thinking = _env("KG_EXTRACT_LLM_DISABLE_THINKING", default="1") != "0"
    max_tokens = int(_env("KG_EXTRACT_LLM_MAX_TOKENS", "KG_EXTRACT_MAX_TOKENS", default="16384"))
    thinking_max_tokens = int(_env("KG_EXTRACT_THINKING_MAX_TOKENS", default="32768"))
    return {
        "base_url": _env("KG_EXTRACT_LLM_BASE_URL", "KG_EXTRACT_API_URL", default="http://localhost:9999"),
        "api_key": _env("KG_EXTRACT_LLM_API_KEY", "KG_EXTRACT_API_KEY", default=""),
        "model": _env("KG_EXTRACT_LLM_MODEL", "KG_EXTRACT_MODEL", default="deepseek-v4-flash"),
        "max_tokens": max_tokens,
        "thinking_max_tokens": max(max_tokens, thinking_max_tokens),
        "thinking_passes": thinking_passes_from_env(),
        "max_concurrency": max(1, int(_env("KG_EXTRACT_LLM_MAX_CONCURRENCY", default="4"))),
        "qps": max(0, int(_env("KG_EXTRACT_LLM_QPS", default="0"))),
        "qpm": max(0, int(_env("KG_EXTRACT_LLM_QPM", default="0"))),
        "request_timeout": max(1.0, float(_env("KG_EXTRACT_LLM_TIMEOUT_SECONDS", default="300"))),
        "disable_thinking": disable_thinking,
    }


def get_jev_config() -> dict:
    accept = float(_env("KG_EXTRACT_JEV_NOUL_ACCEPT", default="0.9"))
    reject = float(_env("KG_EXTRACT_JEV_NOUL_REJECT", default="0.1"))
    if not (0.0 <= reject <= accept <= 1.0):
        raise ValueError(
            "Jev 阈值需满足 0 <= noul_reject <= noul_accept <= 1，"
            f"收到 accept={accept}, reject={reject}"
        )
    confidence = float(_env("KG_EXTRACT_JEV_CHOICE_CONFIDENCE", default="0.9"))
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"KG_EXTRACT_JEV_CHOICE_CONFIDENCE 需在 [0, 1]，收到 {confidence}")
    return {
        "enabled": _env("KG_EXTRACT_JEV_ENABLED", default="0") != "0",
        "model_path": _env(
            "KG_EXTRACT_JEV_MODEL_PATH",
            default="/models/open-jev-deberta-v3-large",
        ),
        "noul_accept": accept,
        "noul_reject": reject,
        "choice_confidence": confidence,
    }
