"""Chunk-only entrypoint for the single Knowledge Extract V2 pipeline."""
from __future__ import annotations

from typing import Any, Callable, Iterable

from .chunk_context import ExtractionChunk
from .config import get_llm_config
from .llm_client import create_llm_client
from .run_state import ExtractionValidationError
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
    use_router: bool = True,
    repairer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    entity_adjudicator: Callable[[dict[str, Any]], str | dict[str, Any]] | None = None,
    identity_admission: str | None = None,
    max_repair_rounds: int | None = None,
    max_attempts: int | None = None,
    retry_base_seconds: float = 0.05,
) -> dict[str, Any]:
    """Run the two-pass pipeline over caller-provided chunks.

    ``max_repair_rounds`` / ``max_attempts`` 不传时读
    ``KG_EXTRACT_MAX_REPAIR_ROUNDS``（默认 2）/ ``KG_EXTRACT_LLM_MAX_ATTEMPTS``
    （默认 3）。
    """
    ontology = (ontology or "").strip()
    if not ontology:
        raise ExtractionValidationError(
            "本体定义不能为空",
            stage="ontology_compile",
            error_code="ONTOLOGY_EMPTY",
        )
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
        use_router=use_router,
        repairer=repairer,
        entity_adjudicator=entity_adjudicator,
        identity_admission=identity_admission or config.get("identity_admission", "quarantine"),
        max_repair_rounds=(
            config.get("max_repair_rounds", 2)
            if max_repair_rounds is None
            else max_repair_rounds
        ),
        max_attempts=(
            config.get("max_attempts", 3)
            if max_attempts is None
            else max_attempts
        ),
        retry_base_seconds=retry_base_seconds,
    )


__all__ = ["extract_harness"]
