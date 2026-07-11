import json
from typing import Any

from mcp_services.runtime import mcp


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
    return {"candidates": candidates}


@mcp.tool(name="query_entity_properties")
def query_entity_properties(entity_id: str) -> dict[str, Any]:
    """Return all labels and properties for a graph entity."""
    node = _answer_tool().entity_linker.get_node_full_info(entity_id)
    if not node:
        return {"error": f"未找到实体: {entity_id}"}
    return {
        "entity_id": entity_id,
        "name": node.get("name", ""),
        "labels": node.get("labels", []),
        "properties": node.get("properties", {}),
    }


@mcp.tool(name="search_graph")
def search_graph(start_entity_id: str, question: str = "", depth: int = 1) -> dict[str, Any]:
    """Search one graph depth from an entity and return raw triples."""
    tool = _answer_tool()
    node = tool.entity_linker.get_node_full_info(start_entity_id)
    entity_name = node.get("name", start_entity_id) if node else start_entity_id
    result = tool.search_single_depth(
        question=question,
        topic_entity={start_entity_id: entity_name},
        depth=depth,
    )
    result["start_entity_id"] = start_entity_id
    return result


@mcp.tool(name="get_reasoning_rules")
def get_reasoning_rules(question: str = "") -> list[dict[str, Any]]:
    """Return all reasoning rules or those applicable to a question."""
    if question:
        from algorithm.ans.QA.rules_config import get_applicable_rules

        return get_applicable_rules(question)
    from algorithm.ans.QA.rules_config import REASONING_RULES

    return REASONING_RULES["rules"]
