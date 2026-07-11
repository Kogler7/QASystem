from typing import Any

from mcp_services.runtime import mcp


@mcp.tool(name="kg_extract_v2")
def extract_knowledge(
    ontology: str,
    text: str,
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Extract entities and relationships from text using a PlantUML ontology."""
    from algorithm.knowledge_extract import extract_v2

    return extract_v2(ontology=ontology, text=text, model=model, base_url=base_url)


@mcp.tool(name="kg_get_dimension_scales")
def get_dimension_scales(dimension: str) -> dict[str, Any]:
    """Return all scales associated with a knowledge-space dimension."""
    from algorithm.knowledge_extract import get_dimension_scales as load_scales

    return load_scales(dimension=dimension)


@mcp.tool(name="kg_init_knowledge_space")
def init_knowledge_space(dimension_scales: dict[str, list[str]]) -> dict[str, Any]:
    """Create a knowledge space from a dimension-to-scales mapping."""
    from algorithm.knowledge_extract import init_knowledge_space as initialize

    return initialize(dimension_scales=dimension_scales)


@mcp.tool(name="kg_store_extraction")
def store_extraction(graph: dict[str, Any]) -> dict[str, Any]:
    """Store an extracted graph in Neo4j, deduplicating nodes by uid."""
    from algorithm.knowledge_extract import store_extraction_to_neo4j

    return store_extraction_to_neo4j(graph=graph)
