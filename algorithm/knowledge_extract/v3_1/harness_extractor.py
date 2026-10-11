"""Chunk-only entrypoint for the V3.1 extraction pipeline."""
from __future__ import annotations

from typing import Any, Callable, Iterable
import json
import threading

from .skill_planner import SkillAssemblyPlanner
from .skill_registry import SkillRegistry
from .candidate_generation import generate_structured
from .chunk_context import ExtractionChunk
from .config import get_llm_config
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
    assembly_planner: SkillAssemblyPlanner | None = None,
    skills_dir: str | None = None,
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
    planning_attempts: list[dict[str, Any]] = []
    planning_calls: list[dict[str, Any]] = []
    planning_lock = threading.Lock()

    def run_planning(messages: list[dict[str, str]], output_schema: dict[str, Any], operation: str):
        # Use the same caller-supplied runtime and existing scheduling path as
        # extraction. The shared planner owns no client, queue or rate limiter.
        attempts: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        target = json.loads(messages[1]["content"])["assembly_target"]["user"]
        chunk_id = target["documents"][0]["current_chunk"]["chunk_id"]
        try:
            return generate_structured(
                resolved_llm, messages, output_schema, use_scheduler=use_scheduler,
                attempts=attempts, pass_name=operation, chunk_id=chunk_id,
                max_attempts=max_attempts, retry_base_seconds=retry_base_seconds,
                call_metrics=calls,
            )
        finally:
            with planning_lock:
                planning_attempts.extend(attempts)
                planning_calls.extend(calls)

    if assembly_planner is not None and skills_dir is not None:
        raise ValueError("skills_dir cannot be combined with an explicitly configured assembly_planner")
    planner = assembly_planner if assembly_planner is not None else SkillAssemblyPlanner(run_planning, SkillRegistry(skills_dir))
    result = extract_split(
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
        entity_adjudicator=entity_adjudicator,
        max_repair_rounds=max_repair_rounds,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        assembly_planner=planner,
    )
    plans = planner.audit()
    if plans:
        result.setdefault("profile", {})["prompt_assembly"] = {
            "mode": "model_skill_plan", "plans": plans,
            "calls": planning_calls, "attempts": planning_attempts,
        }
    return result


__all__ = ["extract_harness"]
