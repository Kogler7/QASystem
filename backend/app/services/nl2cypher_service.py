"""
向量转化存储与自然语言转 Cypher：进程内调用 algorithm/NL_to_cypher 算法（按函数封装），不启动独立服务。
使用前需安装 algorithm/NL_to_cypher 依赖，并配置 Ollama（见 algorithm/NL_to_cypher/requirements.txt）。
Ollama 地址可通过环境变量 OLLAMA_URL、OLLAMA_MODEL_NAME 覆盖。
"""
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BACKEND_APP = Path(__file__).resolve().parent.parent
_ROOT = _BACKEND_APP.parents[1]
_NL2CYPHER_ROOT = _ROOT / "algorithm" / "NL_to_cypher"
if _NL2CYPHER_ROOT.exists() and str(_NL2CYPHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_NL2CYPHER_ROOT))

_generator = None


def _get_generator():
    global _generator
    if _generator is None:
        try:
            from text2cypher import Text2CypherGenerator
            _generator = Text2CypherGenerator()
        except Exception as e:
            logger.warning("加载 NL2Cypher 模块失败: %s", e)
            raise
    return _generator


def health() -> dict[str, Any]:
    """检查自然语言转 Cypher 模块是否可加载（依赖 Ollama 可用性由调用方自行保证）。"""
    try:
        _get_generator()
        return {"status": "ok"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"自然语言转 Cypher 模块不可用: {e}。请确认已安装 algorithm/NL_to_cypher 依赖（见 requirements.txt）并配置 Ollama。",
        }


def generate(question: str, graph_schema: str) -> dict[str, Any]:
    """
    根据自然语言问题与图谱 Schema 生成 Cypher 查询。
    由路由层用 asyncio.to_thread 调用。
    """
    gen = _get_generator()
    cypher = gen.generate(question, graph_schema or "")
    return {"cypher": (cypher or "").strip()}
