"""Chunk-only entrypoint for the single Knowledge Extract V2 pipeline."""
from __future__ import annotations

from typing import Any, Callable, Iterable

from .chunk_context import ExtractionChunk
from .config import get_llm_config
from .jev_client import get_jev_client, jev_enabled
from .jev_harness import default_entity_adjudicator
from .llm_client import create_llm_client
from .split_extractor import extract_split


def extract_harness(
    ontology: str,
    chunks: Iterable[ExtractionChunk],
    *,
    target_types: Iterable[str] | None = None,
    include_subclasses: bool = True,
    validation_mode: str = "strict",
    llm: Any | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    max_tokens: int | None = None,
    request_timeout: float | None = None,
    use_scheduler: bool = True,
    max_workers: int | None = None,
    best_effort: bool = False,
    repairer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    entity_adjudicator: Callable[[dict[str, Any]], str | dict[str, Any]] | None = None,
    max_repair_rounds: int = 2,
    max_attempts: int = 3,
    retry_base_seconds: float = 0.05,
    clef: Any = None,
    skill_refs: list[str] | None = None,
    assembly_planner: Any | None = None,
    seed_entities: Iterable[dict[str, Any]] | None = None,
    relation_only: bool = False,
) -> dict[str, Any]:
    """Run Stage1 v3.1 entities then Stage2 v2.2 relations over caller chunks.

    ``relation_only=True`` seeds frozen entities and runs only the LLM relation stage.
    """
    ontology = (ontology or "").strip()
    if not ontology:
        raise ValueError("本体定义不能为空")
    if validation_mode.casefold() not in {"strict", "permissive"}:
        raise ValueError("validation_mode 仅支持 strict 或 permissive")

    config = get_llm_config()
    resolved_llm = llm if llm is not None else create_llm_client(
        base_url=base_url or config["base_url"],
        api_key=api_key or config["api_key"],
        model=model or config["model"],
        max_tokens=max_tokens or config["max_tokens"],
        request_timeout=(
            config.get("request_timeout")
            if request_timeout is None
            else request_timeout
        ),
    )
    resolved_adjudicator = entity_adjudicator
    if jev_enabled():
        get_jev_client()
        if resolved_adjudicator is None:
            resolved_adjudicator = default_entity_adjudicator
    workers = max(1, int(max_workers or config.get("max_concurrency", 1)))
    if getattr(resolved_llm, "max_concurrency", None) is None:
        resolved_llm.max_concurrency = workers
    return extract_split(
        ontology,
        chunks=chunks,
        target_types=list(target_types) if target_types is not None else None,
        include_subclasses=include_subclasses,
        validation_mode=validation_mode,
        llm=resolved_llm,
        model=getattr(resolved_llm, "model", None) or model or config.get("model", ""),
        max_workers=workers,
        use_scheduler=use_scheduler,
        best_effort=best_effort,
        repairer=repairer,
        entity_adjudicator=resolved_adjudicator,
        max_repair_rounds=max_repair_rounds,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        clef=clef,
        skill_refs=skill_refs,
        assembly_planner=assembly_planner,
        seed_entities=list(seed_entities) if seed_entities is not None else None,
        relation_only=relation_only,
    )


__all__ = ["extract_harness"]
