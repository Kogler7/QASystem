from collections.abc import Iterable
from typing import Any

from .batch import deduplicate_chunk_graphs, extract_chunks
from .chunk_context import ExtractionChunk
from .extractor import extract_v2
from .harness_extractor import extract_harness, extract_harness_items
from .result_contract import to_public_result
from .split_extractor import extract_split


def extract(
    chunks: Iterable[ExtractionChunk],
    ontology: str,
    **kwargs: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Extract the stable public ``entities`` / ``relations`` contract."""
    return to_public_result(extract_harness(ontology=ontology, chunks=chunks, **kwargs))


__all__ = [
    "extract",
    "extract_split",
    "extract_harness",
    "extract_harness_items",
    "extract_chunks",
    "deduplicate_chunk_graphs",
]
