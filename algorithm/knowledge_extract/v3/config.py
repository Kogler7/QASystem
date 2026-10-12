"""知识抽取 V2 的 LLM 环境配置。"""
from docker.configuration import getenv as config_getenv
import os


def _env(key: str, *fallback_keys: str, default: str = "") -> str:
    for k in (key, *fallback_keys):
        v = config_getenv(k)
        if v and v.strip():
            return v.strip()
    return default


KG_EXTRACT_API_URL = _env("KG_EXTRACT_LLM_BASE_URL", "KG_EXTRACT_API_URL", default="http://localhost:9999")
KG_EXTRACT_API_KEY = _env("KG_EXTRACT_LLM_API_KEY", "KG_EXTRACT_API_KEY", default="")
KG_EXTRACT_MODEL = _env("KG_EXTRACT_LLM_MODEL", "KG_EXTRACT_MODEL", default="deepseek-v4-flash")
KG_EXTRACT_MAX_TOKENS = int(_env("KG_EXTRACT_LLM_MAX_TOKENS", "KG_EXTRACT_MAX_TOKENS", default="16384"))
# 实体输出去截断：整块调用失败后按 markdown 段落切段，分段输出再组装。
KG_EXTRACT_ENTITY_SEGMENT_TARGET_CHARS = int(_env("KG_EXTRACT_ENTITY_SEGMENT_TARGET_CHARS", default="2600"))
KG_EXTRACT_ENTITY_SEGMENT_MIN_CHARS = int(_env("KG_EXTRACT_ENTITY_SEGMENT_MIN_CHARS", default="400"))
KG_EXTRACT_ENTITY_SEGMENT_OVERLAP = int(_env("KG_EXTRACT_ENTITY_SEGMENT_OVERLAP", default="50"))
KG_EXTRACT_ENTITY_SEGMENT_MAX_DEPTH = int(_env("KG_EXTRACT_ENTITY_SEGMENT_MAX_DEPTH", default="3"))
KG_EXTRACT_MAX_CONCURRENCY = int(_env("KG_EXTRACT_LLM_MAX_CONCURRENCY", default="4"))
KG_EXTRACT_QPS = int(_env("KG_EXTRACT_LLM_QPS", default="0"))
KG_EXTRACT_QPM = int(_env("KG_EXTRACT_LLM_QPM", default="0"))
KG_EXTRACT_REQUEST_TIMEOUT = float(_env("KG_EXTRACT_LLM_TIMEOUT_SECONDS", default="300"))
# 默认关闭 thinking（与 agent 部分一致）；设置 "0" 可开启
KG_EXTRACT_DISABLE_THINKING = _env("KG_EXTRACT_LLM_DISABLE_THINKING", default="1") != "0"
KG_EXTRACT_IDENTITY_ADMISSION = _env(
    "KG_EXTRACT_IDENTITY_ADMISSION", default="quarantine"
).casefold()
# 证据准入：warn（默认）证据无法逐字定位或核验不一致时保留实体并标记
# evidence_status="unverified"，终验 EVIDENCE_INVALID 降为 warning；
# reject 恢复旧行为（拒收候选，EVIDENCE_INVALID 计为硬错误阻断发布）。
KG_EXTRACT_EVIDENCE_ADMISSION = _env(
    "KG_EXTRACT_EVIDENCE_ADMISSION", default="warn"
).casefold()
# 验证-修复循环轮数上限（TargetedRepairOrchestrator 硬上限 2），默认 2。
KG_EXTRACT_MAX_REPAIR_ROUNDS = int(_env("KG_EXTRACT_MAX_REPAIR_ROUNDS", default="2"))
# 实体/关系主调用的 LLM 最大尝试次数（瞬时失败/JSON 错误重试），默认 3。
KG_EXTRACT_LLM_MAX_ATTEMPTS = int(_env("KG_EXTRACT_LLM_MAX_ATTEMPTS", default="3"))


def get_evidence_admission() -> str:
    """证据准入模式；仅 ``reject`` 恢复旧的 fail-closed 行为，其余一律 warn。"""
    return "reject" if KG_EXTRACT_EVIDENCE_ADMISSION == "reject" else "warn"


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
        "evidence_admission": get_evidence_admission(),
        "max_repair_rounds": min(2, max(0, KG_EXTRACT_MAX_REPAIR_ROUNDS)),
        "max_attempts": max(1, KG_EXTRACT_LLM_MAX_ATTEMPTS),
    }
