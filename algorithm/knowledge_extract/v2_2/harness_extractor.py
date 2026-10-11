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
) -> dict[str, Any]:
    """Run the two-pass pipeline over caller-provided chunks."""
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
    return extract_split(
        ontology,
        chunks=chunks,
        target_types=list(target_types) if target_types is not None else None,
        include_subclasses=include_subclasses,
        validation_mode=validation_mode,
        llm=resolved_llm,
        model=getattr(resolved_llm, "model", None) or model or config.get("model", ""),
        max_workers=max(1, int(max_workers or config.get("max_concurrency", 1))),
        use_scheduler=use_scheduler,
        best_effort=best_effort,
        repairer=repairer,
        entity_adjudicator=resolved_adjudicator,
        max_repair_rounds=max_repair_rounds,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        clef=clef,
    )


__all__ = ["extract_harness"]
