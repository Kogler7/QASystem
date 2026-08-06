from typing import Any

from mcp_services.runtime import mcp
from mcp_services.result import ToolCode, invoke_tool
from mcp_services.dispatch import maybe_submit


@mcp.tool(name="kg_extract_v2")
def extract_knowledge(
    ontology: str,
    text: str,
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Extract entities and relationships from text using a PlantUML ontology."""
    queued = maybe_submit(
        "mcp.knowledge.extract",
        {"ontology": ontology, "text": text, "model": model, "base_url": base_url},
        resource_class="io.llm",
    )
    if queued is not None:
        return queued
    return invoke_tool(
        lambda: _extract(ontology, text, model, base_url),
        ToolCode.KNOWLEDGE_FAILED,
        "知识抽取完成",
    )


def _extract(ontology, text, model, base_url):
    from algorithm.knowledge_extract import extract_v2

    return extract_v2(ontology=ontology, text=text, model=model, base_url=base_url)


@mcp.tool(name="kg_get_dimension_scales")
def get_dimension_scales(dimension: str) -> dict[str, Any]:
    """Return all scales associated with a knowledge-space dimension."""
    return invoke_tool(lambda: _load_scales(dimension), ToolCode.KNOWLEDGE_FAILED, "维度刻度查询")


def _load_scales(dimension):
    from algorithm.knowledge_extract import get_dimension_scales

    return get_dimension_scales(dimension=dimension)


@mcp.tool(name="kg_init_knowledge_space")
def init_knowledge_space(dimension_scales: dict[str, list[str]]) -> dict[str, Any]:
    """Create a knowledge space from a dimension-to-scales mapping."""
    return invoke_tool(lambda: _initialize(dimension_scales), ToolCode.KNOWLEDGE_FAILED, "知识空间初始化")


def _initialize(dimension_scales):
    from algorithm.knowledge_extract import init_knowledge_space

    return init_knowledge_space(dimension_scales=dimension_scales)


@mcp.tool(name="kg_store_extraction")
def store_extraction(graph: dict[str, Any]) -> dict[str, Any]:
    """Store an extracted graph in Neo4j, deduplicating nodes by uid."""
    queued = maybe_submit(
        "mcp.knowledge.store",
        {"graph": graph},
        resource_class="io.graph",
    )
    if queued is not None:
        return queued
    return invoke_tool(lambda: _store(graph), ToolCode.KNOWLEDGE_FAILED, "知识图谱存储")


def _store(graph):
    from algorithm.knowledge_extract import store_extraction_to_neo4j

    return store_extraction_to_neo4j(graph=graph)
