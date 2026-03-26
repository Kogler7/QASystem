"""
知识抽取：进程内调用 algorithm/uie 算法。
"""
import logging
import sys
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[5]
_UIE_ROOT = _ROOT / "algorithm" / "uie"
if _UIE_ROOT.exists() and str(_UIE_ROOT) not in sys.path:
    sys.path.insert(0, str(_UIE_ROOT))

_extraction_api = None


def _get_extraction_api():
    global _extraction_api
    if _extraction_api is None:
        try:
            from ie import extraction_api as m
            _extraction_api = m
        except Exception as e:
            logger.warning("加载知识抽取模块失败: %s", e)
            raise
    return _extraction_api


def health() -> dict[str, Any]:
    try:
        _get_extraction_api()
        return {"status": "ok"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"知识抽取模块不可用: {e}。请确认已安装 algorithm/uie 依赖。",
        }


def extract(
    main_object: str,
    text: str,
    use_templates: bool = True,
    llm_base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
) -> dict[str, Any]:
    # UIE 抽取默认依赖远端微调模型；未显式传入时，兜底为 rule_v4，避免误用通用默认模型导致 404。
    llm_model = llm_model or os.getenv("UIE_LLM_MODEL") or "rule_v4"
    api = _get_extraction_api()
    result = api.extract(
        main_object=main_object,
        text=text,
        use_templates=use_templates,
        store_to_neo4j=False,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
    )
    return {"raw": result["raw"], "graph": result["graph"]}


def store_graph(
    graph_struct: dict[str, Any],
    neo4j_uri: str = "bolt://localhost:7687",
    neo4j_user: str = "neo4j",
    neo4j_password: str = "neo4j2025",
) -> None:
    api = _get_extraction_api()
    api.store_graph_to_neo4j(
        graph_struct=graph_struct,
        neo4j_uri=neo4j_uri,
        neo4j_user=neo4j_user,
        neo4j_password=neo4j_password,
    )


def extract_from_chunked_json_file(
    data: bytes,
    main_object: str,
    use_templates: bool = True,
    llm_base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
) -> dict[str, Any]:
    """解析切块 JSON 字节内容，逐段抽取并合并，返回与 extract() 相同的 raw + graph。"""
    api = _get_extraction_api()
    return api.extract_from_chunked_json_bytes(
        data,
        main_object=main_object,
        use_templates=use_templates,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
    )
