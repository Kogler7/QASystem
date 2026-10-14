"""知识抽取 V3.1 的 LLM 环境配置。"""
from docker.configuration import getenv as config_getenv, declarations
from external_services.llm.quota import parse_quota_tiers


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
KG_EXTRACT_MAX_CONCURRENCY = int(_env("KG_EXTRACT_LLM_MAX_CONCURRENCY", default="16"))
KG_EXTRACT_QPS = int(_env("KG_EXTRACT_LLM_QPS", default="0"))
KG_EXTRACT_QPM = int(_env("KG_EXTRACT_LLM_QPM", default="0"))
KG_EXTRACT_REQUEST_TIMEOUT = float(_env("KG_EXTRACT_LLM_TIMEOUT_SECONDS", default="300"))
# 默认关闭 thinking（与 agent 部分一致）；设置 "0" 可开启
KG_EXTRACT_DISABLE_THINKING = _env("KG_EXTRACT_LLM_DISABLE_THINKING", default="1") != "0"


def parse_semantic_thinking(value) -> bool:
    """Validate the non-secret stage policy without echoing configuration values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError("Invalid v3.1 semantic thinking setting")


def get_llm_config(*, include_quota: bool = True, include_thinking: bool = True) -> dict:
    values = {
        "base_url": KG_EXTRACT_API_URL,
        "api_key": KG_EXTRACT_API_KEY,
        "model": KG_EXTRACT_MODEL,
        "max_tokens": KG_EXTRACT_MAX_TOKENS,
        "max_concurrency": max(1, KG_EXTRACT_MAX_CONCURRENCY),
        "qps": max(0, KG_EXTRACT_QPS),
        "qpm": max(0, KG_EXTRACT_QPM),
        "request_timeout": max(1.0, KG_EXTRACT_REQUEST_TIMEOUT),
        "disable_thinking": KG_EXTRACT_DISABLE_THINKING,
        "generation_temperature": float(_env("KG_EXTRACT_V31_GENERATION_TEMPERATURE", default="0")),
        "stream_generation": parse_semantic_thinking(config_getenv('KG_EXTRACT_V31_STREAM_GENERATION',
            declarations()['items']['KG_EXTRACT_V31_STREAM_GENERATION']['default'])),
        "verification_max_tokens": max(1, int(config_getenv('KG_EXTRACT_V31_VERIFICATION_MAX_TOKENS',
            declarations()['items']['KG_EXTRACT_V31_VERIFICATION_MAX_TOKENS']['default']))),
        "fast_verification": parse_semantic_thinking(config_getenv('KG_EXTRACT_V31_FAST_VERIFICATION',
            declarations()['items']['KG_EXTRACT_V31_FAST_VERIFICATION']['default'])),
    }
    if include_thinking:
        values["semantic_thinking"] = parse_semantic_thinking(config_getenv(
            "KG_EXTRACT_V31_SEMANTIC_THINKING",
            declarations()["items"]["KG_EXTRACT_V31_SEMANTIC_THINKING"]["default"]))
    if include_quota:
        values["quota_group"] = config_getenv("KG_EXTRACT_LLM_QUOTA_GROUP", "default")
        values["quota_tiers"] = [vars(tier) for tier in parse_quota_tiers(config_getenv(
            "KG_EXTRACT_LLM_QUOTA_TIERS_JSON",
            declarations()["items"]["KG_EXTRACT_LLM_QUOTA_TIERS_JSON"]["default"]))]
    return values
