"""Entity discovery followed by complete LLM records, without entity merging."""
from __future__ import annotations
import re
import uuid
from copy import deepcopy
from functools import lru_cache
from collections.abc import Iterable
from typing import Any
from .candidate_generation import (_normalize_span_entities, bind_entity_evidence as _bind_entity_evidence,
    entity_pass as _entity_pass, generate_structured as _generate_structured)
from .output_contracts import output_schema_for_types as _output_schema_for_types
from .candidate_models import EntityCandidate
from .chunk_context import ChunkContextManager, ExtractionChunk, ExtractionRequest
from .coverage import CoverageAuditor
from .evidence import Evidence, evidence_coverage
from .graph_utils import append_warning as _append_warning, entity_uid, build_node_properties
from .mapping_scorer import MappingScorer
from .ontology_compiler import ExtractionPlan, compile_plan
from .ontology_schema import OntologySchema
from .skill_planner import SkillAssemblyPlanner
from .prompt_router import build_chat_messages, resolve_document_route
from .property_enrichment import enrich_entity_properties
from .run_state import ExtractionRun, ExtractionValidationError
from .verification import ExtractionVerifier

@lru_cache(maxsize=32)
def _compile_runtime(
    ontology: str,
    target_types: tuple[str, ...] | None,
    include_subclasses: bool,
    mode: str,
    unknown_content_policy: str,
) -> tuple[ExtractionPlan, OntologySchema]:
    """Reuse immutable ontology artifacts across repeated harness runs."""
    plan = compile_plan(
        ontology,
        target_types=target_types,
        include_subclasses=include_subclasses,
        mode=mode,
        unknown_content_policy=unknown_content_policy,
    )
    return plan, OntologySchema.parse(ontology or "")


def _repair_context_chunks(
    issues: Iterable[dict[str, Any]],
    manager: ChunkContextManager,
    *,
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    max_chunks: int = 3,
) -> list[ExtractionChunk]:
    """Resolve verifier issues to evidence chunks without guessing document prefixes."""
    known_ids = set(manager.chunk_ids)
    selected: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"source_block_ids", "chunk_ids"} and isinstance(item, list):
                    selected.update(str(chunk_id) for chunk_id in item if str(chunk_id) in known_ids)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str):
            for chunk_id in known_ids:
                if value == chunk_id or value.startswith(f"{chunk_id}:"):
                    selected.add(chunk_id)

    issue_rows = list(issues)
    for row in issue_rows:
        path = str(row.get("path") or "")
        collect(path)
        collect(row.get("context"))
        match = re.match(r"^(entities|relations)\[(\d+)\]", path)
        if match is None:
            continue
        facts = entities if match.group(1) == "entities" else relations
        index = int(match.group(2))
        if index < len(facts):
            collect(facts[index])

    if not selected and len(manager.chunk_ids) <= 3:
        selected.update(manager.chunk_ids)
    return [
        manager.get_chunk(chunk_id)
        for chunk_id in manager.chunk_ids
        if chunk_id in selected
    ][: max(1, max_chunks)]


def extract_split(
    ontology, chunks, *, target_types=None, include_subclasses=True,
    validation_mode="strict", llm, model="", unknown_content_policy="ignore",
    local_previous=1, local_next=1, same_section=True, max_workers=1,
    use_scheduler=False, run_id="", mapping_weights=None,
    mapping_accepted_threshold=0.85, mapping_low_confidence_threshold=0.70,
    apply_mapping_threshold=False, best_effort=False, repairer=None,
    entity_adjudicator=None, max_repair_rounds=2, max_attempts=3,
    retry_base_seconds=0.05, assembly_planner=None,
):
    """Discover per-chunk names, then let the LLM return complete entity records.

    No entity pool, ID assignment or alias resolution. Cross-chunk consolidation
    of exact type/name keys is authored by the property LLM.
    Graph UIDs exist only in the compatibility projection, never in prompts/raw.
    """
    if isinstance(chunks, (str, bytes, dict)):
        raise TypeError("chunks 必须是 ExtractionChunk 序列")
    chunks = list(chunks)
    if any(not isinstance(c, ExtractionChunk) for c in chunks):
        raise TypeError("extract_split 只接收 ExtractionChunk 序列")
    if validation_mode.casefold() not in {"strict", "permissive"}:
        raise ValueError("validation_mode 仅支持 strict 或 permissive")
    if entity_adjudicator is not None:
        raise ValueError("Entity adjudication/merging is no longer supported")
    plan, schema = _compile_runtime(ontology, tuple(target_types) if target_types is not None else None,
                                    include_subclasses, validation_mode.casefold(), unknown_content_policy)
    if validation_mode.casefold() == "strict" and any(w.severity == "error" for w in plan.schema_warnings):
        raise ExtractionValidationError("ontology strict validation failed", stage="ontology_compile")
    manager = ChunkContextManager(chunks)
    request = ExtractionRequest(chunks=chunks, target_types=list(target_types) if target_types is not None else None)
    route = resolve_document_route(plan, chunks)
    planner = assembly_planner if assembly_planner is not None else SkillAssemblyPlanner()
    planner.prepare(route.to_dict())
    warnings = [w.to_dict() for w in plan.schema_warnings]
    attempts, calls = [], []
    run = ExtractionRun.create(run_id=run_id or f"ke-{uuid.uuid4().hex}", ontology=ontology,
        markdown="\n".join(c.text for c in chunks), model=model,
        prompt_version="knowledge-extract-v3.1-type-name-properties-v1", best_effort=best_effort)
    run.start()
    for c in chunks:
        run.register_block(c.chunk_id, block_id=str(c.metadata.get("block_id") or c.chunk_id), kind="chunk")
    if not chunks:
        _append_warning(warnings, "EMPTY_INPUT", "请求没有 chunks", "chunks")
    discovered = _entity_pass(plan, schema, manager, request, llm, route=route,
        assembly_planner=planner, max_workers=max_workers, use_scheduler=use_scheduler,
        attempts=attempts, warnings=warnings, max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds, call_metrics=calls) if chunks else []
    entities, property_stats = enrich_entity_properties(discovered, manager, plan, llm,
        route=route, assembly_planner=planner, attempts=attempts, warnings=warnings,
        call_metrics=calls, use_scheduler=use_scheduler, local_previous=local_previous,
        local_next=local_next, same_section=same_section, max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds, max_workers=max_workers)

    verifier = ExtractionVerifier()
    rounds = []
    # Repairs re-run complete-record synthesis with all original occurrences,
    # so a repaired key cannot create a second final entity.
    all_occurrences = list(discovered)
    for round_number in range(1, max_repair_rounds + 1):
        report = verifier.verify(plan, schema, manager, request, entities, [])
        issues = [i.to_dict() for i in report.repairable_issues]
        if not issues:
            break
        referenced = _repair_context_chunks(issues, manager, entities=entities, relations=[])
        if not referenced:
            break
        if repairer is not None:
            proposal = repairer({"round": round_number, "issues": issues,
                "entities": deepcopy(entities), "relations": [], "chunks": [c.to_dict() for c in manager],
                "schemas": deepcopy(plan.output_schemas)})
            additions = _bind_entity_evidence(proposal.get("entities", []), manager)
            additions = [e for e in additions if e.get("type") in plan.allowed_types]
        else:
            repair_chunk = ExtractionChunk(f"repair-{round_number}", "\n\n".join(c.text for c in referenced)[:8000])
            output_schema = _output_schema_for_types(plan, "entity_candidates", plan.relevant_types(repair_chunk.text))
            messages = build_chat_messages(task="repair", plan=plan, route=route, chunk=repair_chunk,
                output_schema=output_schema, repair_issues=issues, assembly_planner=planner)
            parsed = _generate_structured(llm, messages, output_schema, use_scheduler=use_scheduler,
                attempts=attempts, pass_name="repair_entity", chunk_id=repair_chunk.chunk_id,
                max_attempts=max_attempts, retry_base_seconds=retry_base_seconds, call_metrics=calls)
            additions = _normalize_span_entities(parsed or {}, chunk=repair_chunk, schema=schema,
                                                 target_types=request.target_types, warnings=warnings)
            additions = _bind_entity_evidence(additions, manager, chunk_ids=[c.chunk_id for c in referenced])
        if not additions:
            break
        grouped = []
        for c in referenced:
            rows = [e for e in additions if c.chunk_id in (e.get("evidence") or {}).get("source_block_ids", [])]
            if rows:
                grouped.append((c.chunk_id, rows))
        all_occurrences.extend(grouped)
        completed, stats = enrich_entity_properties(all_occurrences, manager, plan, llm, route=route,
            assembly_planner=planner, attempts=attempts, warnings=warnings, call_metrics=calls,
            use_scheduler=use_scheduler, max_attempts=max_attempts, retry_base_seconds=retry_base_seconds)
        if completed:
            entities = completed
        for k in property_stats:
            property_stats[k] += stats[k]
        rounds.append({"round": round_number, "added_entities": len(completed)})
        if not completed:
            break

    scorer = MappingScorer(weights=mapping_weights, accepted_threshold=mapping_accepted_threshold,
                           low_confidence_threshold=mapping_low_confidence_threshold)
    scores, final_entities, objects, nodes = [], [], [], []
    for entity in entities:
        candidate = EntityCandidate("", entity["type"], entity["type"], entity["name"],
            entity.get("properties") or {}, [Evidence.from_dict(entity.get("evidence"))] if entity.get("evidence") else [], "llm")
        score = scorer.score_entity(candidate, plan)
        scores.append({"type": entity["type"], "name": entity["name"], **score.to_dict()})
        if apply_mapping_threshold and score.status == "rejected":
            _append_warning(warnings, "MAPPING_REJECTED", "实体映射分数低于阈值", entity["name"])
            continue
        final_entities.append(entity)
        evidence = [entity["evidence"]] if entity.get("evidence") else []
        objects.append({"type": entity["type"], "identity": entity["name"], "display_name": entity["name"],
                        "properties": deepcopy(entity["properties"]), "evidence": evidence,
                        "mapping": {"status": "accepted", "score": score.score}})
        # Legacy graph transport requires a uid, but this does not deduplicate,
        # change the complete model records or feed IDs back into extraction.
        nodes.append({"uid": entity_uid(entity["type"], entity["name"], None), "label": entity["type"],
                      "properties": build_node_properties(entity["name"], entity["properties"]),
                      "evidences": deepcopy(evidence)})
    raw = {"entities": final_entities, "relations": []}
    report = verifier.verify(plan, schema, manager, request, final_entities, [])
    if validation_mode.casefold() == "strict" and report.hard_issues:
        attempts.append({"attempt": 1, "type": "schema_validation", "pass": "verification", "chunk": "final",
                         "message": "; ".join(i.message for i in report.hard_issues), "terminal": True})
    failed = {(a.get("pass"), a.get("chunk")) for a in attempts if a.get("terminal")}
    for c in chunks:
        run.mark_chunk_scan(c.chunk_id, "entity", completed=("entity", c.chunk_id) not in failed)
    run.absorb_attempts(attempts)
    run.finish(accepted_entities=len(final_entities), accepted_relations=0)
    gaps = [g.to_dict() for g in CoverageAuditor().audit(plan, manager, request, final_entities, [])]
    trace = run.to_trace()
    trace.update(calls=calls, repair_rounds=rounds, verification=report.to_dict())
    result = {"raw": raw, "graph": {"nodes": nodes, "relationships": []}, "triples": [],
        "intermediate_model": {"objects": objects, "relations": [], "coverage_gaps": gaps},
        "warnings": warnings, "attempts": attempts, "evidence_coverage": evidence_coverage(raw),
        "coverage_gaps": gaps, "verification": report.to_dict(),
        "mapping": {"entities": scores, "relations": [], "rejected": sum(s["status"] == "rejected" for s in scores)},
        "profile": {"document_route": route.to_dict(), "pipeline": "entity_property", "chunks": len(chunks),
            "entity_candidates": sum(len(es) for _, es in discovered), "relation_candidates": 0,
            "accepted_entities": len(final_entities), "accepted_relations": 0, "property_enrichment": property_stats,
            "mapping_mode": "enforce" if apply_mapping_threshold else "annotate",
            "repair_rounds": rounds, "repair_stop_reason": "validation_complete", "llm_calls": len(calls)},
        "status": run.status.value, "run_trace": trace, "error_code": run.error_code}
    if run.error:
        result["_error"] = run.error
    return result
