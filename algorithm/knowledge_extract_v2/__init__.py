from collections.abc import Iterable
from typing import Any

from .batch import deduplicate_chunk_graphs
from .chunk_context import ExtractionChunk
from .harness_extractor import extract_harness
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
    "deduplicate_chunk_graphs",
]
