"""V3.1: semantic extraction harness with Clef decisions and V2 contracts."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..v2.batch import deduplicate_chunk_graphs
from ..v2.chunk_context import ExtractionChunk
from ..v2.result_contract import to_public_result
from .harness import extract_harness


def extract(chunks: Iterable[ExtractionChunk], ontology: str, **kwargs: Any) -> dict[str, Any]:
    return to_public_result(extract_harness(ontology=ontology, chunks=chunks, **kwargs))


def extract_aggregate(ontology: str, chunks: Iterable[ExtractionChunk], **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("validation_mode", "permissive")
    kwargs.setdefault("best_effort", True)
    return extract_harness(ontology=ontology, chunks=chunks, **kwargs)


__all__ = ["ExtractionChunk", "extract", "extract_aggregate", "extract_harness", "deduplicate_chunk_graphs"]
