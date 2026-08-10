from .batch import deduplicate_chunk_graphs, extract_chunks, summarize_chunks
from .extractor import extract_v2, summarize_text
from typing import Any


def _graph_write_unavailable(*args: Any, **kwargs: Any):
    raise RuntimeError(
        "Graph write and knowledge-space operations are unavailable; "
        "use the application service when the capability is opened."
    )


init_knowledge_space = _graph_write_unavailable
init_knowledge_space_tree = _graph_write_unavailable
init_knowledge_map_spaces = _graph_write_unavailable
get_dimension_scales = _graph_write_unavailable
store_extraction_to_neo4j = _graph_write_unavailable


__all__ = [
    "extract_v2",
    "extract_chunks",
    "deduplicate_chunk_graphs",
    "summarize_chunks",
    "summarize_text",
    "get_dimension_scales",
    "init_knowledge_map_spaces",
    "init_knowledge_space",
    "init_knowledge_space_tree",
    "store_extraction_to_neo4j",
]
