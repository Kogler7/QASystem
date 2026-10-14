"""知识抽取 V3.1 的 LLM 环境配置。"""
from docker.configuration import getenv as config_getenv


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
        "generation_temperature": float(_env("KG_EXTRACT_V31_GENERATION_TEMPERATURE", default="0")),
    }
