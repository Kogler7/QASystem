"""版本包入口：暴露 backend 适配器需要的 aggregate / public 契约。"""
from collections.abc import Iterable
from typing import Any

from .batch import deduplicate_chunk_graphs
from .artifacts import export_artifacts
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


def extract_aggregate(
    ontology: str,
    chunks: Iterable[ExtractionChunk],
    *,
    target_types: list[str] | None = None,
    validation_mode: str = "permissive",
    best_effort: bool = True,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    max_workers: int | None = None,
    llm: Any | None = None,
    use_scheduler: bool = True,
    assembly_planner: Any | None = None,
    skills_dir: str | None = None,
    skill_refs: list[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Run the pipeline over ``chunks`` and return the aggregate contract."""
    return extract_harness(
        ontology=ontology,
        chunks=chunks,
        target_types=target_types,
        validation_mode=validation_mode,
        best_effort=best_effort,
        model=model,
        base_url=base_url,
        api_key=api_key,
        max_workers=max_workers,
        llm=llm,
        use_scheduler=use_scheduler,
        assembly_planner=assembly_planner,
        skills_dir=skills_dir,
        skill_refs=skill_refs,
    )


__all__ = [
    "extract",
    "extract_aggregate",
    "extract_split",
    "extract_harness",
    "export_artifacts",
    "deduplicate_chunk_graphs",
]
