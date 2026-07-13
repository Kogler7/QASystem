import json
from functools import lru_cache
from typing import Any

from mcp_services.runtime import mcp
from mcp_services.result import ToolCode, normalize_tool_result, tool_error, tool_success


@lru_cache(maxsize=1)
def _answer_tool():
    from algorithm.ans.QA.config import Config
    from algorithm.ans.QA.neo4j_client import MultiServerWikidataQueryClient
    from algorithm.ans.QA.tools.answer_generation import AnswerGenerationTool

    config = Config.load()
    client = MultiServerWikidataQueryClient(
        neo4j_uri=config.NEO4J_URI,
        username=config.NEO4J_USER,
        password=config.NEO4J_PASSWORD,
    )
    return AnswerGenerationTool(client, config)


@mcp.tool(name="link_entities")
def link_entities(keywords: list[str] | str, question: str = "") -> dict[str, Any]:
    """Find graph entities matching recognized keywords."""
    try:
        if isinstance(keywords, str):
            try:
                keywords = json.loads(keywords)
            except json.JSONDecodeError:
                keywords = [keywords]
        result = _answer_tool().entity_linker.link_entities_with_llm_judge(
            question, keywords=keywords
        )
        candidates = []
        for entity in result.get("all_candidates", []):
            candidates.append(
                {
                    "name": entity.get("properties", {}).get("name", entity.get("qid", "")),
                    "id": entity.get("qid", ""),
                    "score": entity.get("relevance_score", 0),
                    "is_relevant": entity.get("is_relevant", False),
                    "properties": entity.get("properties", {}),
                    "llm_judgment": entity.get("llm_judgment", ""),
                }
            )
        return tool_success({"candidates": candidates}, "实体链接完成")
    except Exception as exc:
        return tool_error(ToolCode.REASONING_FAILED, "实体链接失败", str(exc))


@mcp.tool(name="query_entity_properties")
def query_entity_properties(entity_id: str) -> dict[str, Any]:
    """Return all labels and properties for a graph entity."""
    try:
        node = _answer_tool().entity_linker.get_node_full_info(entity_id)
    except Exception as exc:
        return tool_error(ToolCode.REASONING_FAILED, "实体属性查询失败", str(exc))
    if not node:
        return tool_error(ToolCode.REASONING_FAILED, f"未找到实体: {entity_id}")
    return tool_success({
        "entity_id": entity_id,
        "name": node.get("name", ""),
        "labels": node.get("labels", []),
        "properties": node.get("properties", {}),
    }, "实体属性查询完成")


@mcp.tool(name="search_graph")
def search_graph(start_entity_id: str, question: str = "", depth: int = 1) -> dict[str, Any]:
    """Search one graph depth from an entity and return raw triples."""
    try:
        tool = _answer_tool()
        node = tool.entity_linker.get_node_full_info(start_entity_id)
        entity_name = node.get("name", start_entity_id) if node else start_entity_id
        result = tool.search_single_depth(
            question=question,
            topic_entity={start_entity_id: entity_name},
            depth=depth,
        )
        result["start_entity_id"] = start_entity_id
        return normalize_tool_result(result, ToolCode.REASONING_FAILED, "图谱搜索完成")
    except Exception as exc:
        return tool_error(ToolCode.REASONING_FAILED, "图谱搜索失败", str(exc))


@mcp.tool(name="get_reasoning_rules")
def get_reasoning_rules(question: str = "") -> dict[str, Any]:
    """Return all reasoning rules or those applicable to a question."""
    try:
        if question:
            from algorithm.ans.QA.rules_config import get_applicable_rules

            return tool_success(get_applicable_rules(question), "推理规则查询完成")
        from algorithm.ans.QA.rules_config import REASONING_RULES

        return tool_success(REASONING_RULES["rules"], "推理规则查询完成")
    except ModuleNotFoundError as exc:
        return tool_error(
            ToolCode.TOOL_NOT_IMPLEMENTED,
            "推理规则模块尚未提供",
            {"missing_module": exc.name},
        )
