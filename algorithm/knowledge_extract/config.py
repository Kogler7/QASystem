"""知识抽取 V2 配置：从环境变量读取 LLM API 和 Neo4j 连接参数。"""
import os


def _env(key: str, *fallback_keys: str, default: str = "") -> str:
    for k in (key, *fallback_keys):
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    return default


# ── Neo4j ──
NEO4J_URI = _env("NEO4J_URI", default="bolt://localhost:7687")
NEO4J_USER = _env("NEO4J_USER", "NEO4J_USERNAME", default="neo4j")
NEO4J_PASSWORD = _env("NEO4J_PASSWORD", default="neo4j2025")


def get_neo4j_config() -> dict:
    return {
        "uri": NEO4J_URI,
        "user": NEO4J_USER,
        "password": NEO4J_PASSWORD,
    }


# ── LLM API (知识抽取 V2) ──
KG_EXTRACT_API_URL = _env("KG_EXTRACT_LLM_BASE_URL", "KG_EXTRACT_API_URL", default="http://localhost:9999")
KG_EXTRACT_API_KEY = _env("KG_EXTRACT_LLM_API_KEY", "KG_EXTRACT_API_KEY", default="")
KG_EXTRACT_MODEL = _env("KG_EXTRACT_LLM_MODEL", "KG_EXTRACT_MODEL", default="Qwen/Qwen3-8B")
KG_EXTRACT_MAX_TOKENS = int(_env("KG_EXTRACT_LLM_MAX_TOKENS", "KG_EXTRACT_MAX_TOKENS", default="16384"))


def get_llm_config() -> dict:
    return {
        "base_url": KG_EXTRACT_API_URL,
        "api_key": KG_EXTRACT_API_KEY,
        "model": KG_EXTRACT_MODEL,
        "max_tokens": KG_EXTRACT_MAX_TOKENS,
    }
