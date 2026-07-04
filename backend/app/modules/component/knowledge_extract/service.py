"""
知识抽取：进程内调用 algorithm/uie 算法。
长文本或 Markdown 大块统一走切块抽取；超长块经 nl2cypher 递归切分细化后再喂 UIE。
"""
import importlib
import json
import logging
import sys
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 单段最大字符数：需与 rule_v4 等小上下文模型 + UML system prompt 留足 token（约 4k 总上下文）
_CHUNK_CHAR_SOFT_LIMIT = int(os.getenv("UIE_CHUNK_MAX_CHARS", "1200"))

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


def refine_chunks_for_llm(chunks: list[str]) -> list[str]:
    """
    将上游切分结果（如 Markdown 标题块）中仍过长的片段，用 algorithm/NL_to_cypher 的递归切分再细分，
    保证每段不超过 _CHUNK_CHAR_SOFT_LIMIT，与 UIE 逐块抽取的上下游一致。
    """
    from ..nl2cypher import service as nl2cypher_service

    out: list[str] = []
    for raw in chunks or []:
        c = (raw or "").strip()
        if not c:
            continue
        if len(c) <= _CHUNK_CHAR_SOFT_LIMIT:
            out.append(c)
            continue
        sub = nl2cypher_service.split_recursively(c).get("chunks") or []
        for s in sub:
            s = (s or "").strip()
            if s:
                out.append(s)
        if not sub:
            out.append(c[:_CHUNK_CHAR_SOFT_LIMIT])
            logger.warning(
                "refine_chunks_for_llm: recursive split returned empty, truncated to %d chars",
                _CHUNK_CHAR_SOFT_LIMIT,
            )
    return out


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
    text = (text or "").strip()
    if not text:
        return {
            "raw": {},
            "graph": {"nodes": [], "relationships": [], "ontology_relations": []},
        }

    api = _get_extraction_api()
    if len(text) <= _CHUNK_CHAR_SOFT_LIMIT:
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

    refined = refine_chunks_for_llm([text])
    if not refined:
        refined = [text[:_CHUNK_CHAR_SOFT_LIMIT]]
    payload = json.dumps({"chunks": refined}, ensure_ascii=False).encode("utf-8")
    return extract_from_chunked_json_file(
        payload,
        main_object,
        use_templates,
        llm_base_url,
        llm_model,
        llm_api_key,
    )


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
    from ie.chunked_json import (  # type: ignore
        extract_chunk_texts_from_payload,
        parse_chunked_json_bytes,
    )

    payload = parse_chunked_json_bytes(data)
    texts = extract_chunk_texts_from_payload(payload)
    if not texts:
        raise ValueError("切块 JSON 中未找到有效文本片段")
    texts = refine_chunks_for_llm(texts)
    if not texts:
        raise ValueError("切块细化后无有效文本")
    payload_bytes = json.dumps({"chunks": texts}, ensure_ascii=False).encode("utf-8")
    return api.extract_from_chunked_json_bytes(
        payload_bytes,
        main_object=main_object,
        use_templates=use_templates,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
    )


def upload_uml_and_parse(
    data: bytes,
    filename: str,
    main_object: Optional[str] = None,
    new_main_object: Optional[str] = None,
) -> dict[str, Any]:
    api = _get_extraction_api()
    result = api.upload_uml_and_parse(
        data=data,
        filename=filename,
        main_object=main_object,
        new_main_object=new_main_object,
    )
    graph_struct = result.get("graph") or {}
    # UML 上传后自动入库，避免前端再发一次 store-graph 请求
    api.store_graph_to_neo4j(graph_struct=graph_struct)
    result["stored_to_neo4j"] = True
    return result


def list_uml_main_objects() -> list[dict[str, str]]:
    api = _get_extraction_api()
    return api.list_uml_main_objects()


# ── 知识空间初始化 ──


def _get_knowledge_space_api():
    """延迟导入 algorithm/knowledge_extract 模块。"""
    try:
        # 确保 algorithm 在 sys.path
        algo_root = _ROOT / "algorithm"
        if str(algo_root) not in sys.path:
            sys.path.insert(0, str(algo_root))
        return importlib.import_module("knowledge_extract")
    except Exception as e:
        logger.warning("加载 knowledge_extract 模块失败: %s", e)
        raise


def init_knowledge_space(dimension_scales: dict[str, list[str]]) -> dict:
    """
    初始化知识空间：
    - 输入 {维度: [刻度列表]}，例如 {"时间":["年月"], "空间":["集团","子公司"]}
    - 检查维度是否存在
    - 检查/创建刻度节点
    - 创建 KnowledgeSpace 节点（命名：维度.刻度+维度.刻度+...）
    - 连接 KnowledgeSpace BELONGS_TO 刻度
    """
    api = _get_knowledge_space_api()
    return api.init_knowledge_space(dimension_scales)


def init_knowledge_map_spaces(
    space_name: str,
    dimension_scales: dict[str, list[str]],
) -> dict:
    """
    知识空间初始化 V3（笛卡尔积多子空间）：
    - space_name: 根知识空间名称
    - dimension_scales: {维度: [刻度列表]}
    - 按维度刻度的笛卡尔积生成多个子空间节点
    - 每个子空间连接根空间和对应刻度
    """
    api = _get_knowledge_space_api()
    return api.init_knowledge_map_spaces(space_name, dimension_scales)


def init_knowledge_space_tree(
    workspace_tree: list[dict],
    dimensions: list[dict],
) -> dict:
    """
    知识空间初始化 V2（树形结构）：
    - workspace_tree: 空间树草稿，含根空间和子空间
    - dimensions: 维度刻度列表
    - 创建 KnowledgeSpace 节点树、Dimension/Scale 节点
    - 返回 root_workspace_id
    """
    api = _get_knowledge_space_api()
    return api.init_knowledge_space_tree(workspace_tree, dimensions)


def get_dimension_scales(dimension: str) -> dict:
    """根据维度名称返回该维度下的所有刻度。"""
    api = _get_knowledge_space_api()
    return api.get_dimension_scales(dimension)


def store_extraction_to_neo4j(graph: dict) -> dict:
    """
    将知识抽取 V2 返回的 graph 结构存入 Neo4j。
    按 uid 去重：同一 uid 的节点不重复创建。
    """
    api = _get_knowledge_space_api()
    return api.store_extraction_to_neo4j(graph)


# ── 知识抽取 V2 ──


def extract_v2(
    ontology: str,
    text: str,
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """
    本体驱动的知识抽取 V2（无模板匹配）。
    输入本体 PUML + 待抽取文本，返回 {raw, graph}。
    """
    api = _get_knowledge_space_api()
    return api.extract_v2(
        ontology=ontology,
        text=text,
        base_url=base_url,
        api_key=api_key,
        model=model,
    )
