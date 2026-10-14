"""知识抽取 V2 的 LLM 环境配置。"""
from qasystem_config.configuration import getenv as config_getenv
import os


def _env(key: str, *fallback_keys: str, default: str = "") -> str:
    for k in (key, *fallback_keys):
        v = config_getenv(k)
        if v and v.strip():
            return v.strip()
    return default


KG_EXTRACT_API_URL = ""
KG_EXTRACT_API_KEY = ""
KG_EXTRACT_MODEL = ""
KG_EXTRACT_MAX_TOKENS = int(_env("KG_EXTRACT_LLM_MAX_TOKENS", default="16384"))
KG_EXTRACT_MAX_CONCURRENCY = 4
KG_EXTRACT_QPS = 0
KG_EXTRACT_QPM = 0
KG_EXTRACT_REQUEST_TIMEOUT = float(_env("KG_EXTRACT_LLM_TIMEOUT_SECONDS", default="300"))
# 默认关闭 thinking（与 agent 部分一致）；设置 "0" 可开启
KG_EXTRACT_DISABLE_THINKING = _env("KG_EXTRACT_LLM_DISABLE_THINKING", default="1") != "0"
KG_EXTRACT_IDENTITY_ADMISSION = _env(
    "KG_EXTRACT_IDENTITY_ADMISSION", default="quarantine"
).casefold()


def get_llm_policy() -> dict:
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


def get_llm_config() -> dict:
    from algorithm.knowledge_extract.service_configuration import fill_service
    return fill_service(get_llm_policy())
