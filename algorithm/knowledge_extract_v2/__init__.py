from typing import Any

from .batch import deduplicate_chunk_graphs, extract_chunks
from .extractor import extract_v2
from .split_extractor import extract_split


def init_knowledge_space(*args: Any, **kwargs: Any):
    from .knowledge_space import init_knowledge_space as implementation

    return implementation(*args, **kwargs)


def init_knowledge_space_tree(*args: Any, **kwargs: Any):
    from .knowledge_space import init_knowledge_space_tree as implementation

    return implementation(*args, **kwargs)


def init_knowledge_map_spaces(*args: Any, **kwargs: Any):
    from .knowledge_space import init_knowledge_map_spaces as implementation

    return implementation(*args, **kwargs)


def get_dimension_scales(*args: Any, **kwargs: Any):
    from .knowledge_space import get_dimension_scales as implementation

    return implementation(*args, **kwargs)


def store_extraction_to_neo4j(*args: Any, **kwargs: Any):
    from .knowledge_space import store_extraction_to_neo4j as implementation

    return implementation(*args, **kwargs)


__all__ = [
    "extract_v2",
    "extract_split",
    "extract_chunks",
    "deduplicate_chunk_graphs",
    "get_dimension_scales",
    "init_knowledge_map_spaces",
    "init_knowledge_space",
    "init_knowledge_space_tree",
    "store_extraction_to_neo4j",
]
