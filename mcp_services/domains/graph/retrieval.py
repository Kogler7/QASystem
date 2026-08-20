import json
from functools import lru_cache
from typing import Any, Literal

from mcp_services.result import ToolCode, normalize_tool_result, tool_error, tool_success
from mcp_services.runtime import mcp
from mcp_services.dispatch import maybe_submit


@lru_cache(maxsize=1)
def _graph_components():
    from algorithm.ans.QA.config import Config
    from algorithm.ans.QA.entity_linker import EntityLinker
    from algorithm.ans.QA.knowledge_map_client import KnowledgeMapClient

    config = Config.load()
    km_client = KnowledgeMapClient()
    return km_client, EntityLinker(km_client, config)


def _search_neighbors(
    node_id: str,
    direction: str,
    limit: int,
    relationship_types: list[str] | None = None,
) -> dict[str, Any]:
    """经知识地图接口查询一跳邻居（自动带权限过滤）。

    接口已放开标签要求，故沿用原 search_neighbors 设计：不约束节点类型与关系类型。
    返回结构与原 search_neighbors 一致。
    """
    from algorithm.ans.QA.knowledge_map_client import KnowledgeMapClient

    km = KnowledgeMapClient()

    # 起点名称
    rows = km.execute_cypher(
        "MATCH (n {id:$id}) RETURN n.name AS name LIMIT 1",
        {"id": node_id},
    )
    start_name = rows[0].get("name", "") or "" if rows else ""

    # 方向过滤用"有向边模式"而非 startNode()/endNode()——接口受限引擎可能不支持这两个函数
    if direction == "outgoing":
        pattern = "MATCH (n {id:$id})-[r]->(m) "
    elif direction == "incoming":
        pattern = "MATCH (n {id:$id})<-[r]-(m) "
    else:
        pattern = "MATCH (n {id:$id})-[r]-(m) "

    params: dict[str, Any] = {"id": node_id, "limit": limit}
    if relationship_types:
        pattern += "WHERE type(r) IN $relationship_types "
        params["relationship_types"] = relationship_types

    statement = (
        pattern +
        "RETURN type(r) AS rel, m.id AS id, m.name AS name, labels(m) AS labels "
        "LIMIT $limit"
    )
    rows = km.execute_cypher(statement, params)

    triples: list[dict[str, Any]] = []
    found_entities: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    head = {"id": node_id, "name": start_name, "labels": [], "properties": {}}
    for row in rows:
        mid = row.get("id", "")
        mname = row.get("name", "") or ""
        mlabels = row.get("labels") or []
        tail = {"id": mid, "name": mname, "labels": mlabels, "properties": {}}
        triples.append(
            {
                "head_entity": head,
                "relation": row.get("rel", ""),
                "relationship_properties": {},
                "tail_entity": tail,
            }
        )
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            found_entities.append(tail)

    return {
        "depth": 1,
        "triples": triples,
        "found_entities": found_entities,
        "has_more": bool(found_entities),
        "filters": {
            "relationship_types": relationship_types or [],
            "direction": direction,
            "limit": limit,
        },
    }


@mcp.tool(name="link_entities")
def link_entities(keywords: list[str] | str, question: str = "") -> dict[str, Any]:
    """实体链接：将用户关键词对齐到知识图谱中的 KnowledgeUnit 节点。name 模糊匹配（CONTAINS），精确匹配项排最前。返回候选单元列表，含相关性评分和 LLM 判断理由。"""
    queued = maybe_submit(
        "mcp.graph.link_entities",
        {"keywords": keywords, "question": question},
        resource_class="io.graph",
    )
    if queued is not None:
        return queued
    try:
        if isinstance(keywords, str):
            try:
                keywords = json.loads(keywords)
            except json.JSONDecodeError:
                keywords = [keywords]
        _client, entity_linker = _graph_components()
        result = entity_linker.link_entities_with_llm_judge(question, keywords=keywords)
        candidates = [
            {
                "name": entity.get("properties", {}).get("name", entity.get("qid", "")),
                "id": entity.get("qid", ""),
                "labels": entity.get("labels", []),
                "score": entity.get("relevance_score", 0),
                "is_relevant": entity.get("is_relevant", False),
                "llm_judgment": entity.get("reason", ""),
            }
            for entity in result.get("all_candidates", [])
        ]
        return tool_success({"candidates": candidates}, "实体链接完成")
    except Exception as exc:
        return tool_error(ToolCode.GRAPH_FAILED, "实体链接失败", str(exc))


# 干系人本体的 MetaClass uid（固定值；如本体 uid 变更则更新此常量）
STAKEHOLDER_META_UID = "ac611c54-8584-4e02-8056-f222eaca9cff"


@mcp.tool(name="get_stakeholders")
def get_stakeholders() -> dict[str, Any]:
    """获取当前知识图谱中的所有干系人实例。

    干系人 = KnowledgeUnit 中 resource_type 指向 Stakeholder 本体（uid 固定）的节点。
    返回干系人列表 [{id, name}, ...]，供 Agent 按问题语义筛选。
    """
    queued = maybe_submit("mcp.graph.get_stakeholders", {}, resource_class="io.graph")
    if queued is not None:
        return queued

    from algorithm.ans.QA.knowledge_map_client import KnowledgeMapClient

    km = KnowledgeMapClient()
    try:
        rows = km.execute_cypher(
            "MATCH (n:KnowledgeUnit) WHERE n.resource_type CONTAINS $uid "
            "RETURN n.id AS id, n.name AS name LIMIT 50",
            {"uid": STAKEHOLDER_META_UID},
        )
        stakeholders = [{"id": r.get("id"), "name": r.get("name")} for r in rows]
        return tool_success({"stakeholders": stakeholders}, "干系人获取完成")
    except Exception as exc:
        return tool_error(ToolCode.GRAPH_FAILED, "干系人获取失败", str(exc))


@mcp.tool(name="query_entity_properties")
def query_entity_properties(entity_id: str) -> dict[str, Any]:
    """实体属性查询：根据实体ID获取节点的标签列表和全部属性。完整属性 = 节点自身字段 + 关联的 KnowledgeProperty 逻辑属性。"""
    queued = maybe_submit(
        "mcp.graph.query_entity",
        {"entity_id": entity_id},
        resource_class="io.graph",
    )
    if queued is not None:
        return queued
    try:
        _client, entity_linker = _graph_components()
        node = entity_linker.get_node_full_info(entity_id)
    except Exception as exc:
        return tool_error(ToolCode.GRAPH_FAILED, "实体属性查询失败", str(exc))
    if not node:
        return tool_error(ToolCode.GRAPH_FAILED, f"未找到实体: {entity_id}")
    return tool_success(
        {
            "entity_id": entity_id,
            "name": node.get("name", ""),
            "labels": node.get("labels", []),
            "properties": node.get("properties", {}),
        },
        "实体属性查询完成",
    )


@mcp.tool(name="search_graph")
def search_graph(
    start_entity_id: str,
    question: str = "",
    depth: Literal[1] = 1,
    relationship_types: list[str] | None = None,
    direction: Literal["outgoing", "incoming", "both"] = "both",
    limit: int = 50,
) -> dict[str, Any]:
    """图谱邻居查询：从指定节点出发沿关系边探索一跳邻居（经知识地图接口，自动带权限过滤）。不约束节点类型与关系类型。返回全部邻接 triples，由 Agent 自行判断哪些路径与问题相关。

    使用方式：search_graph(start_entity_id="<UUID>", direction="outgoing", question=用户问题)"""
    if depth != 1:
        return tool_error(ToolCode.INVALID_ARGUMENT, "原子图谱搜索仅支持 depth=1，请由 Agent 逐层探索")
    queued = maybe_submit(
        "mcp.graph.search",
        {
            "start_entity_id": start_entity_id,
            "question": question,
            "depth": depth,
            "relationship_types": relationship_types,
            "direction": direction,
            "limit": limit,
        },
        resource_class="io.graph",
    )
    if queued is not None:
        return queued
    try:
        result = _search_neighbors(start_entity_id, direction, limit, relationship_types)

        if question:
            result["navigation_context"] = question
        result["start_entity_id"] = start_entity_id
        return normalize_tool_result(result, ToolCode.GRAPH_FAILED, "图谱搜索完成")
    except Exception as exc:
        return tool_error(ToolCode.GRAPH_FAILED, "图谱搜索失败", str(exc))
