"""知识抽取 V2 的 LLM 环境配置。"""
from docker.configuration import getenv as config_getenv
import os


def _env(key: str, *fallback_keys: str, default: str = "") -> str:
    for k in (key, *fallback_keys):
        v = config_getenv(k)
        if v and v.strip():
            return v.strip()
    return default


KG_EXTRACT_API_URL = _env("KG_EXTRACT_LLM_BASE_URL", default="http://localhost:9999")
KG_EXTRACT_API_KEY = _env("KG_EXTRACT_LLM_API_KEY", default="")
KG_EXTRACT_MODEL = _env("KG_EXTRACT_LLM_MODEL", default="deepseek-v4-flash")
KG_EXTRACT_MAX_TOKENS = int(_env("KG_EXTRACT_LLM_MAX_TOKENS", default="16384"))
KG_EXTRACT_MAX_CONCURRENCY = int(_env("KG_EXTRACT_LLM_MAX_CONCURRENCY", default="4"))
KG_EXTRACT_QPS = int(_env("KG_EXTRACT_LLM_QPS", default="0"))
KG_EXTRACT_QPM = int(_env("KG_EXTRACT_LLM_QPM", default="0"))
KG_EXTRACT_REQUEST_TIMEOUT = float(_env("KG_EXTRACT_LLM_TIMEOUT_SECONDS", default="300"))
# 默认关闭 thinking（与 agent 部分一致）；设置 "0" 可开启
KG_EXTRACT_DISABLE_THINKING = _env("KG_EXTRACT_LLM_DISABLE_THINKING", default="1") != "0"
KG_EXTRACT_IDENTITY_ADMISSION = _env(
    "KG_EXTRACT_IDENTITY_ADMISSION", default="quarantine"
).casefold()


def get_llm_config() -> dict:
    return {
        "base_url": KG_EXTRACT_API_URL,
        "api_key": KG_EXTRACT_API_KEY,
        "model": KG_EXTRACT_MODEL,
        "max_tokens": KG_EXTRACT_MAX_TOKENS,
        "max_concurrency": max(1, KG_EXTRACT_MAX_CONCURRENCY),
        "qps": max(0, KG_EXTRACT_QPS),
        "qpm": max(0, KG_EXTRACT_QPM),
        "request_timeout": max(1.0, KG_EXTRACT_REQUEST_TIMEOUT),
        "disable_thinking": KG_EXTRACT_DISABLE_THINKING,
        "identity_admission": (
            KG_EXTRACT_IDENTITY_ADMISSION
            if KG_EXTRACT_IDENTITY_ADMISSION in {"observe", "quarantine"}
            else "quarantine"
        ),
    }


def get_v22_clef_config() -> dict:
    """Clef 预判开关。默认关闭，避免未配置服务时改变抽取结果。"""
    enabled = _env("KG_EXTRACT_V22_CLEF_ENABLED", default="0") != "0"
    try:
        accept = float(_env("KG_EXTRACT_V22_CLEF_ACCEPT", default="0.9"))
    except ValueError:
        accept = 0.9
    if not 0.5 < accept <= 1.0:
        accept = 0.9
    try:
        max_questions = int(_env("KG_EXTRACT_V22_CLEF_MAX_QUESTIONS", default="16"))
    except ValueError:
        max_questions = 16
    # 服务上限 CLEF_MAX_QUESTIONS=16；超长时 gate 会拆题，不必再人为压到 8。
    max_questions = min(16, max(1, max_questions))
    try:
        timeout = float(_env("KG_EXTRACT_V22_CLEF_TIMEOUT", default="10"))
    except ValueError:
        timeout = 10.0
    if timeout <= 0:
        timeout = 10.0
    try:
        max_calls = int(_env("KG_EXTRACT_V22_CLEF_MAX_CALLS", default="512"))
    except ValueError:
        max_calls = 512
    max_calls = max(1, max_calls)
    try:
        state_chars = int(_env("KG_EXTRACT_V22_CLEF_STATE_CHARS", default="800"))
    except ValueError:
        state_chars = 800
    state_chars = max(64, state_chars)
    try:
        max_inflight = int(_env("KG_EXTRACT_V22_CLEF_MAX_INFLIGHT", default="2"))
    except ValueError:
        max_inflight = 2
    # 服务队列 CLEF_MAX_QUEUE_SIZE=4；默认 2 留出排队余量，1 即完全串行。
    max_inflight = min(4, max(1, max_inflight))
    return {
        "enabled": enabled,
        "accept": accept,
        "max_questions": max_questions,
        "timeout": timeout,
        "max_calls": max_calls,
        "state_chars": state_chars,
        "max_inflight": max_inflight,
    }


def get_jev_config() -> dict:
    # 统一使用当前版本的 JEV 阈值名称。
    accept = float(_env("KG_EXTRACT_JEV_ACCEPT", default="0.9"))
    reject = float(_env("KG_EXTRACT_JEV_REJECT", default="0.9"))
    if not (0.0 <= accept <= 1.0 and 0.0 <= reject <= 1.0):
        raise ValueError(
            "Jev 阈值需分别落在 [0, 1]，"
            f"收到 accept={accept}, reject={reject}"
        )
    confidence = float(_env("KG_EXTRACT_JEV_CHOICE_CONFIDENCE", default="0.9"))
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"KG_EXTRACT_JEV_CHOICE_CONFIDENCE 需在 [0, 1]，收到 {confidence}")
    precision = _env("KG_EXTRACT_JEV_PRECISION", default="bf16")
    if precision not in {"bf16", "fp32"}:
        raise ValueError(f"KG_EXTRACT_JEV_PRECISION 仅支持 bf16 或 fp32，收到 {precision}")
    batch_questions = int(_env("KG_EXTRACT_JEV_BATCH_QUESTIONS", default="32"))
    if batch_questions < 0:
        raise ValueError(f"KG_EXTRACT_JEV_BATCH_QUESTIONS 需 >= 0，收到 {batch_questions}")
    return {
        "enabled": _env("KG_EXTRACT_JEV_ENABLED", default="0") != "0",
        "model_path": _env("KG_EXTRACT_JEV_MODEL_PATH", default="/models/nanojev"),
        "accept": accept,
        "reject": reject,
        "choice_confidence": confidence,
        "precision": precision,
        "batch_questions": batch_questions,
    }
