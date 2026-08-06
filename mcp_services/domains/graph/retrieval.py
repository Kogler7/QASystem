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
    from algorithm.ans.QA.neo4j_client import MultiServerWikidataQueryClient

    config = Config.load()
    client = MultiServerWikidataQueryClient(
        neo4j_uri=config.NEO4J_URI,
        username=config.NEO4J_USER,
        password=config.NEO4J_PASSWORD,
    )
    return client, EntityLinker(client, config)


@mcp.tool(name="link_entities")
def link_entities(keywords: list[str] | str, question: str = "") -> dict[str, Any]:
    """Link query terms to graph entity candidates using graph data plus semantic ranking."""
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
                "score": entity.get("relevance_score", 0),
                "is_relevant": entity.get("is_relevant", False),
                "properties": entity.get("properties", {}),
                "llm_judgment": entity.get("llm_judgment", ""),
            }
            for entity in result.get("all_candidates", [])
        ]
        return tool_success({"candidates": candidates}, "实体链接完成")
    except Exception as exc:
        return tool_error(ToolCode.GRAPH_FAILED, "实体链接失败", str(exc))


@mcp.tool(name="query_entity_properties")
def query_entity_properties(entity_id: str) -> dict[str, Any]:
    """Return labels and complete properties for one graph entity."""
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
    """Return deterministic one-hop triples, optionally filtered by relation and direction."""
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
        client, _entity_linker = _graph_components()
        result = client.client.search_neighbors(
            start_entity_id,
            relationship_types=relationship_types,
            direction=direction,
            limit=limit,
        )
        if question:
            result["navigation_context"] = question
        result["start_entity_id"] = start_entity_id
        return normalize_tool_result(result, ToolCode.GRAPH_FAILED, "图谱搜索完成")
    except Exception as exc:
        return tool_error(ToolCode.GRAPH_FAILED, "图谱搜索失败", str(exc))
