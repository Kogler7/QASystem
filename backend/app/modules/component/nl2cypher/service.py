"""
向量转化存储与自然语言转 Cypher：进程内调用 algorithm/NL_to_cypher 算法。
使用前需安装 algorithm/NL_to_cypher 依赖，并配置 Ollama。
"""
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[5]
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
    try:
        _get_generator()
        return {"status": "ok"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"自然语言转 Cypher 模块不可用: {e}。请确认已安装 algorithm/NL_to_cypher 依赖并配置 Ollama。",
        }


def generate(question: str, graph_schema: str) -> dict[str, Any]:
    gen = _get_generator()
    cypher = gen.generate(question, graph_schema or "")
    return {"cypher": (cypher or "").strip()}


# ---------------------------------------------------------------------------
# 文本切片器接口
# ---------------------------------------------------------------------------

def _get_splitter():
    """懒加载 NL_to_cypher.splitter 模块并返回引用"""
    try:
        import splitter as _mod  # type: ignore
        return _mod
    except Exception as e:
        logger.warning("加载切片模块失败: %s", e)
        raise


def _make_docs(text: str):
    # 避免在顶层引入 langchain 依赖，动态导入
    from langchain_core.documents import Document

    return [Document(page_content=text)]


def split_by_character(text: str) -> dict[str, Any]:
    mod = _get_splitter()
    docs = _make_docs(text)
    split_docs = mod.split_by_character(docs)
    return {"chunks": [d.page_content for d in split_docs]}


def split_recursively(text: str) -> dict[str, Any]:
    mod = _get_splitter()
    docs = _make_docs(text)
    split_docs = mod.split_recursively(docs)
    return {"chunks": [d.page_content for d in split_docs]}


def split_by_markdown(text: str) -> dict[str, Any]:
    mod = _get_splitter()
    docs = _make_docs(text)
    split_docs = mod.split_by_markdown(docs)
    return {"chunks": [d.page_content for d in split_docs]}


def split_by_language_python(text: str) -> dict[str, Any]:
    mod = _get_splitter()
    docs = _make_docs(text)
    split_docs = mod.split_by_language_python(docs)
    return {"chunks": [d.page_content for d in split_docs]}


def text2vector(text: str) -> dict[str, Any]:
    """将用户输入的文本切片、向量化并存入数据库。

    返回内容包含状态和生成的片段列表，可用于前端调试。
    """
    try:
        import rag
    except Exception as e:
        logger.warning("加载 RAG 模块失败: %s", e)
        raise

    docs = _make_docs(text)
    # 使用默认的递归字符切分作为基础
    split_docs = rag.txt_split(docs)

    # 嵌入并写入 Chroma 数据库，函数内部已负责持久化
    rag.embedding(split_docs)

    return {"status": "ok", "chunks": [d.page_content for d in split_docs]}