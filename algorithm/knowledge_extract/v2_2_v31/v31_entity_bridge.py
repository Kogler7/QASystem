"""Stage 1: run vendored v3.1-aligned entity extraction, then seed a frozen pool.

Runtime does **not** import ``algorithm.knowledge_extract.v3_1``. Entity algorithm
lives in ``.entity_stage`` and must be re-synced when v3.1 entity behavior changes.
"""
from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .entity_stage.chunk_context import ExtractionChunk as EntityStageChunk
from .entity_stage.split_extractor import extract_split as entity_extract_split

from .candidate_models import EntityCandidate
from .chunk_context import ExtractionChunk
from .entity_resolver import CandidatePool
from .evidence import Evidence


V31_ENTITY_SOURCE = "v31_entity"


@dataclass
class EntityStageResult:
    """Frozen entity-stage output handed to the relation stage."""

    entities: list[dict[str, Any]]
    attempts: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    skill_selection: dict[str, Any] | None = None
    property_stats: dict[str, Any] = field(default_factory=dict)
    status: str = ""
    repair_stop_reason: str = ""
    profile: dict[str, Any] = field(default_factory=dict)


def to_entity_stage_chunks(chunks: Iterable[ExtractionChunk]) -> list[EntityStageChunk]:
    """Preserve chunk text/ids/metadata so evidence coordinates stay aligned."""
    return [EntityStageChunk.from_dict(chunk.to_dict()) for chunk in chunks]


to_v31_chunks = to_entity_stage_chunks


def _evidence_rows(entity: dict[str, Any]) -> list[dict[str, Any]]:
    evidences = entity.get("evidences")
    if isinstance(evidences, list) and evidences:
        return [row for row in evidences if isinstance(row, dict)]
    primary = entity.get("evidence")
    return [primary] if isinstance(primary, dict) else []


def entity_dict_to_candidate(entity: dict[str, Any]) -> EntityCandidate:
    """Project a frozen entity-stage dict onto a v2.2 EntityCandidate."""
    evidence_items = [
        item for item in (_evidence_rows(entity)) for item in [Evidence.from_dict(item)] if item
    ]
    name = str(entity.get("name") or "")
    mentions: list[str] = []
    if name.strip():
        mentions.append(name)
    for item in evidence_items:
        quote = str(item.quote or "").strip()
        if quote and quote not in mentions:
            mentions.append(quote)
    properties = deepcopy(entity.get("properties") or {})
    return EntityCandidate(
        candidate_id="",
        proposed_type=str(entity.get("type") or ""),
        canonical_type=entity.get("type"),
        name=name,
        properties=properties,
        evidence=evidence_items,
        source=V31_ENTITY_SOURCE,
        mentions=mentions,
        mapping_status="accepted",
    )


def run_entity_stage(
    ontology: str,
    chunks: Iterable[ExtractionChunk],
    *,
    llm: Any,
    target_types: Iterable[str] | None = None,
    include_subclasses: bool = True,
    validation_mode: str = "strict",
    model: str = "",
    unknown_content_policy: str = "ignore",
    local_previous: int = 1,
    local_next: int = 1,
    same_section: bool = True,
    max_workers: int = 1,
    use_scheduler: bool = False,
    run_id: str = "",
    mapping_weights: dict[str, float] | None = None,
    mapping_accepted_threshold: float = 0.85,
    mapping_low_confidence_threshold: float = 0.70,
    apply_mapping_threshold: bool = False,
    best_effort: bool = False,
    repairer: Any | None = None,
    max_repair_rounds: int = 2,
    max_attempts: int = 3,
    retry_base_seconds: float = 0.05,
    assembly_planner: Any | None = None,
    skill_refs: list[str] | None = None,
) -> EntityStageResult:
    """Stage 1: finish vendored entity/property path; ignore empty relations."""
    stage_chunks = to_entity_stage_chunks(chunks)
    result = entity_extract_split(
        ontology,
        stage_chunks,
        target_types=list(target_types) if target_types is not None else None,
        include_subclasses=include_subclasses,
        validation_mode=validation_mode,
        llm=llm,
        model=model,
        unknown_content_policy=unknown_content_policy,
        local_previous=local_previous,
        local_next=local_next,
        same_section=same_section,
        max_workers=max_workers,
        use_scheduler=use_scheduler,
        run_id=run_id,
        mapping_weights=mapping_weights,
        mapping_accepted_threshold=mapping_accepted_threshold,
        mapping_low_confidence_threshold=mapping_low_confidence_threshold,
        apply_mapping_threshold=apply_mapping_threshold,
        best_effort=best_effort,
        repairer=repairer,
        max_repair_rounds=max_repair_rounds,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        assembly_planner=assembly_planner,
        skill_refs=skill_refs,
    )
    raw = result.get("raw") or {}
    entities = [deepcopy(row) for row in (raw.get("entities") or []) if isinstance(row, dict)]
    profile = result.get("profile") or {}
    return EntityStageResult(
        entities=entities,
        attempts=list(result.get("attempts") or []),
        calls=list((result.get("run_trace") or {}).get("calls") or []),
        warnings=list(result.get("warnings") or []),
        skill_selection=profile.get("skill_selection"),
        property_stats=dict(profile.get("property_enrichment") or {}),
        status=str(result.get("status") or ""),
        repair_stop_reason=str(profile.get("repair_stop_reason") or ""),
        profile=dict(profile),
    )


def seed_frozen_entities(
    pool: CandidatePool,
    entities: Iterable[dict[str, Any]],
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> list[EntityCandidate]:
    """Freeze Stage-1 entities into the pool under exact type+name keys."""
    seeded: list[EntityCandidate] = []
    for entity in entities:
        candidate = entity_dict_to_candidate(entity)
        resolved = pool.seed_entity(candidate, warnings=warnings)
        if resolved is not None:
            seeded.append(resolved)
    return seeded


__all__ = [
    "V31_ENTITY_SOURCE",
    "EntityStageResult",
    "entity_dict_to_candidate",
    "run_entity_stage",
    "seed_frozen_entities",
    "to_entity_stage_chunks",
    "to_v31_chunks",
]
