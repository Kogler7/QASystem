"""Entity discovery with grounded properties and source-proven document aliases."""
from __future__ import annotations
import re
import uuid
import hashlib
import json
from copy import deepcopy
from functools import lru_cache
from collections.abc import Iterable
from typing import Any
from .candidate_generation import entity_pass as _entity_pass
from .task_runtime import document_tasks
from .chunk_context import ChunkContextManager, ExtractionChunk, ExtractionRequest
from .coverage import CoverageAuditor
from .evidence import evidence_coverage
from .graph_utils import append_warning as _append_warning, entity_uid, build_node_properties
from .mapping_scorer import EntityMappingInput, MappingScorer
from .ontology_compiler import ExtractionPlan, compile_plan
from .ontology_schema import OntologySchema
from .skill_planner import SkillAssemblyPlanner
from .prompt_router import resolve_document_route
from .property_enrichment import enrich_entity_properties
from .run_state import ExtractionRun, ExtractionValidationError
from .verification import ExtractionVerifier
from .output_contracts import validate_candidate_properties
from .structured_output import validate_json_schema
from .request_budget import document_expired
from .quality_metrics import completeness_metrics


def _custom_repair_candidates(proposal, plan):
    """Project legacy callbacks onto the ordinary candidate admission contract.

    Host evidence receipts/coordinates and retired relations are not accepted
    from an extension; only source quotations can cross this boundary.
    """
    if not isinstance(proposal, dict) or not isinstance(proposal.get('entities'), list):
        return None
    candidates = []
    for row in proposal['entities']:
        if not isinstance(row, dict) or not isinstance(row.get('evidence'), dict):
            return None
        candidates.append({'type': row.get('type'), 'mention': row.get('name'),
            'properties': deepcopy(row.get('properties', {})),
            'evidence': {key: deepcopy(value) for key, value in row['evidence'].items()
                         if key in {'quote', 'property_quotes'}}})
    payload = {'entities': candidates}
    if (validate_json_schema(payload, plan.output_schemas['entity_candidates'])
            or validate_candidate_properties(payload, plan)):
        return None
    return [{'type': row['type'], 'name': row['mention'], 'properties': row['properties'],
             'evidence': row['evidence']} for row in candidates]


def _admit_repair_scope(grouped, issues, entities, manager, warnings):
    """Uncovered blocks admit discovery; fact repairs admit only affected keys."""
    discovery_chunks, allowed_keys = set(), set()
    for issue in issues:
        if issue.get('repair_type') == 'uncovered_block':
            discovery_chunks.update(c.chunk_id for c in _repair_context_chunks([issue], manager,
                                    entities=entities, relations=[]))
            continue
        context = issue.get('context') or {}
        match = re.match(r'^entities\[(\d+)\]', str(issue.get('path') or ''))
        if match and int(match.group(1)) < len(entities):
            entity = entities[int(match.group(1))]
            allowed_keys.add((entity['type'], entity['name']))
        elif context.get('target_type') and context.get('evidence'):
            allowed_keys.add((context['target_type'], context['evidence']))
    accepted = []
    for chunk_id, rows in grouped:
        scoped = []
        for row in rows:
            if chunk_id in discovery_chunks or (row['type'], row['name']) in allowed_keys:
                scoped.append(row)
            else:
                _append_warning(warnings, 'REPAIR_OUT_OF_SCOPE',
                    '修复候选不属于待修复实体或漏抽切片，已拒收', f"{row['type']}::{row['name']}")
        accepted.append((chunk_id, scoped))
    return accepted

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
    max_chunks: int | None = None,
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
    resolved = [
        manager.get_chunk(chunk_id)
        for chunk_id in manager.chunk_ids
        if chunk_id in selected
    ]
    return resolved if max_chunks is None else resolved[:max(1, max_chunks)]


@document_tasks
def extract_split(
    ontology, chunks, *, target_types=None, include_subclasses=True,
    validation_mode="strict", llm, model="", unknown_content_policy="ignore",
    local_previous=1, local_next=1, same_section=True, max_workers=1,
    use_scheduler=False, run_id="", mapping_weights=None,
    mapping_accepted_threshold=0.85, mapping_low_confidence_threshold=0.70,
    apply_mapping_threshold=False, best_effort=False, repairer=None,
    entity_adjudicator=None, max_repair_rounds=2, max_attempts=3,
    retry_base_seconds=0.05, assembly_planner=None, skill_refs=None,
):
    """Discover per-chunk names, then let the LLM return complete entity records.

    Candidate identities are reconciled with source-backed semantic review.
    Cross-chunk consolidation of exact type/name keys retains verified fields.
    Graph UIDs exist only in the compatibility projection, never in prompts/raw.
    """
    if isinstance(chunks, (str, bytes, dict)):
        raise TypeError("chunks 必须是 ExtractionChunk 序列")
    chunks = list(chunks)
    if any(not isinstance(c, ExtractionChunk) for c in chunks):
        raise TypeError("extract_split 只接收 ExtractionChunk 序列")
    # Freeze caller-owned text/metadata before model calls and worker handoff.
    chunks = deepcopy(chunks)
    # COSEC: Window/header offsets are host-generated source provenance, never
    # caller metadata that could introduce fabricated text into model evidence.
    if any(any(str(key).startswith('_v31_') for key in chunk.metadata) for chunk in chunks):
        raise ValueError('chunk metadata 的 _v31_ 前缀保留给内部来源窗口')
    target_types = tuple(target_types) if target_types is not None else None
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
    warnings = [w.to_dict() for w in plan.schema_warnings]
    attempts, calls = [], []
    run = ExtractionRun.create(run_id=run_id or f"ke-{uuid.uuid4().hex}", ontology=ontology,
        markdown="\n".join(c.text for c in chunks), model=model,
        prompt_version="knowledge-extract-v3.1-tokenized-recovery-v37", best_effort=best_effort)
    run.start()
    for c in chunks:
        run.register_block(c.chunk_id, block_id=str(c.metadata.get("block_id") or c.chunk_id), kind="chunk")
    if not chunks:
        _append_warning(warnings, "EMPTY_INPUT", "请求没有 chunks", "chunks")
    selection = planner.select_for_request(route=route.to_dict(), ontology=ontology, plan=plan,
        chunks=chunks, llm=llm, skill_refs=skill_refs, use_scheduler=use_scheduler,
        attempts=attempts, call_metrics=calls, max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds)
    selection_ok = selection['status'] in {'selected', 'no_match'}
    if not selection_ok:
        _append_warning(warnings, 'SKILL_SELECTION_FAILED',
                        'Skill 选择失败或存在歧义，未执行抽取', 'skill_selection', selection['status'])
    discovered = _entity_pass(plan, schema, manager, request, llm, route=route,
        assembly_planner=planner, max_workers=max_workers, use_scheduler=use_scheduler,
        attempts=attempts, warnings=warnings, max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds, call_metrics=calls,
        local_previous=local_previous, local_next=local_next, same_section=same_section) if chunks and selection_ok else []
    from .candidate_identity import reconcile_candidate_identities
    discovered = reconcile_candidate_identities(discovered, manager, plan, llm, route=route,
        assembly_planner=planner, attempts=attempts, warnings=warnings, call_metrics=calls,
        use_scheduler=use_scheduler, max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds, max_workers=max_workers)
    from .official_identity import reconcile_official_identities
    discovered = reconcile_official_identities(discovered, manager, plan, llm, route=route,
        assembly_planner=planner, attempts=attempts, warnings=warnings, call_metrics=calls,
        use_scheduler=use_scheduler, max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds, max_workers=max_workers)
    entities, property_stats = enrich_entity_properties(discovered, manager, plan, llm,
        route=route, assembly_planner=planner, attempts=attempts, warnings=warnings,
        call_metrics=calls, use_scheduler=use_scheduler, local_previous=local_previous,
        local_next=local_next, same_section=same_section, max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds, max_workers=max_workers)

    verifier = ExtractionVerifier()
    rounds = []
    # Repairs touch only affected exact keys and preserve existing provenance.
    repair_stop_reason = "validation_complete"
    seen_repair_states = set()
    for round_number in range(1, max_repair_rounds + 1):
        if not selection_ok:
            repair_stop_reason = 'skill_selection_failed'
            break
        report = verifier.verify(plan, schema, manager, request, entities, [])
        issues = [i.to_dict() for i in report.repairable_issues]
        if not issues:
            break
        referenced = _repair_context_chunks(issues, manager, entities=entities, relations=[])
        if not referenced:
            repair_stop_reason = "no_repair_context"
            break
        fingerprint = repr([(e["type"], e["name"], e.get("properties"), e.get("evidences")) for e in entities])
        if fingerprint in seen_repair_states:
            repair_stop_reason = "no_progress"
            break
        seen_repair_states.add(fingerprint)
        if repairer is not None:
            # 以下为安全注释COSEC：回调输入完全脱离请求状态；回执由本地核验重建。
            try:
                if document_expired(llm):
                    raise TimeoutError('document deadline exhausted')
                proposal = repairer(deepcopy({"round": round_number, "issues": issues,
                    "entities": entities, "relations": [], "chunks": [c.to_dict() for c in referenced],
                    "schemas": plan.output_schemas}))
                if document_expired(llm):
                    raise TimeoutError('document deadline exhausted')
            except Exception as exc:
                attempts.append({'attempt': round_number, 'type': 'timeout' if isinstance(exc, TimeoutError) else 'request',
                    'pass': 'repair', 'chunk': 'callback', 'message': 'Repair callback failed',
                    'terminal': True, 'retryable': False})
                repair_stop_reason = 'repair_failed'
                break
            from .property_grounding import ground_properties, admit_entity_names
            additions = _custom_repair_candidates(proposal, plan)
            if additions is None:
                attempts.append({'attempt': round_number, 'type': 'schema', 'pass': 'repair',
                    'chunk': 'callback', 'message': 'Repair callback violated candidate contract',
                    'terminal': True, 'retryable': False})
                repair_stop_reason = 'repair_failed'
                break
            additions = admit_entity_names(additions, referenced, llm, attempts=attempts, warnings=warnings,
                use_scheduler=use_scheduler, max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds, call_metrics=calls, plan=plan,
                route=route, assembly_planner=planner)
            additions = ground_properties(additions, referenced, llm, attempts=attempts, warnings=warnings,
                use_scheduler=use_scheduler, max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds, call_metrics=calls, plan=plan,
                route=route, assembly_planner=planner)
            grouped = [(c.chunk_id, [e for e in additions
                if c.chunk_id in (e.get("evidence") or {}).get("source_block_ids", [])]) for c in referenced]
        else:
            property_issues = [issue for issue in issues if issue.get('repair_type') == 'required_property']
            target_keys = {(issue.get('context', {}).get('target_type'), issue.get('context', {}).get('evidence'))
                           for issue in property_issues}
            targets = [(entity['evidence']['source_block_ids'][0],
                        [{**deepcopy(entity), '_properties_scanned': False}]) for entity in entities
                       if (entity['type'], entity['name']) in target_keys]
            if targets:
                repaired, stats = enrich_entity_properties(targets, manager, plan, llm,
                    route=route, assembly_planner=planner, attempts=attempts, warnings=warnings,
                    call_metrics=calls, use_scheduler=use_scheduler, max_attempts=max_attempts,
                    retry_base_seconds=retry_base_seconds, max_workers=max_workers,
                    local_previous=local_previous, local_next=local_next, same_section=same_section,
                    task='repair', repair_issues=property_issues)
                entities = [entity for entity in entities if (entity['type'], entity['name']) not in target_keys] + repaired
                for key in property_stats:
                    property_stats[key] += stats[key]
                rounds.append({'round': round_number, 'affected_entities': len(targets),
                               'completed_entities': len(repaired), 'operation': 'repair_property'})
            discovery_issues = [issue for issue in issues if issue.get('repair_type') == 'uncovered_block']
            if not discovery_issues:
                continue
            discovery_sources = _repair_context_chunks(discovery_issues, manager, entities=entities, relations=[])
            # A failed transport/JSON response cannot establish a semantic
            # omission. Keep rediscovery of those chunks on the base policy.
            recovery_chunk_ids = {attempt.get('chunk') for attempt in attempts
                if attempt.get('terminal')
                and attempt.get('pass') in {'entity', 'recover_entity'}
                and attempt.get('type') in {'json', 'schema', 'incomplete', 'empty',
                                           'request', 'timeout', 'input_budget'}}
            grouped = _entity_pass(plan, schema, manager, ExtractionRequest(discovery_sources, request.target_types),
                llm, route=route, assembly_planner=planner, max_workers=max_workers,
                use_scheduler=use_scheduler, attempts=attempts, warnings=warnings,
                max_attempts=max_attempts, retry_base_seconds=retry_base_seconds,
                call_metrics=calls, task="repair", repair_issues=discovery_issues,
                recovery_chunk_ids=recovery_chunk_ids,
                local_previous=local_previous, local_next=local_next, same_section=same_section)
        grouped = _admit_repair_scope(grouped, issues, entities, manager, warnings)
        affected = {(e["type"], e["name"]) for _, rows in grouped for e in rows}
        if not affected:
            repair_stop_reason = "no_progress"
            break
        # Only affected keys are synthesized; already accepted unrelated keys
        # never enter another property request merely because repair ran.
        prior = [(e["evidence"]["source_block_ids"][0], [{**e, "_properties_scanned": True}])
                 for e in entities if (e["type"], e["name"]) in affected
                 and (e.get("evidence") or {}).get("source_block_ids")]
        completed, stats = enrich_entity_properties(prior + grouped, manager, plan, llm, route=route,
            assembly_planner=planner, attempts=attempts, warnings=warnings, call_metrics=calls,
            use_scheduler=use_scheduler, max_attempts=max_attempts, retry_base_seconds=retry_base_seconds,
            local_previous=local_previous, local_next=local_next, same_section=same_section)
        entities = [e for e in entities if (e["type"], e["name"]) not in affected] + completed
        for k in property_stats:
            property_stats[k] += stats[k]
        rounds.append({"round": round_number, "affected_entities": len(affected), "completed_entities": len(completed)})
    else:
        repair_stop_reason = "max_rounds"

    scorer = MappingScorer(weights=mapping_weights, accepted_threshold=mapping_accepted_threshold,
                           low_confidence_threshold=mapping_low_confidence_threshold)
    scores, final_entities, objects, nodes = [], [], [], []
    for entity in entities:
        candidate = EntityMappingInput(entity["type"], entity.get("properties") or {})
        score = scorer.score_entity(candidate, plan)
        scores.append({"type": entity["type"], "name": entity["name"], **score.to_dict()})
        if apply_mapping_threshold and score.status == "rejected":
            _append_warning(warnings, "MAPPING_REJECTED", "实体映射分数低于阈值", entity["name"])
            continue
        final_entities.append(entity)
        evidence = entity.get("evidences") or ([entity["evidence"]] if entity.get("evidence") else [])
        objects.append({"uid": entity_uid(entity["type"], entity["name"], None), "type": entity["type"], "identity": entity["name"], "display_name": entity["name"],
                        "properties": deepcopy(entity["properties"]), "evidence": evidence,
                        "mapping": {"status": score.status, "score": score.score}})
        # Legacy graph transport requires a uid, but this does not deduplicate,
        # change the complete model records or feed IDs back into extraction.
        nodes.append({"uid": entity_uid(entity["type"], entity["name"], None), "label": entity["type"],
                      "display_name": entity["name"],
                      "properties": build_node_properties(entity["name"], entity["properties"]),
                      "evidences": deepcopy(evidence)})
    raw = {"entities": final_entities, "relations": []}
    report = verifier.verify(plan, schema, manager, request, final_entities, [])
    if not selection_ok:
        repair_stop_reason = 'skill_selection_failed'
    elif report.hard_issues:
        repair_stop_reason = "verification_failed"
    elif not report.repairable_issues:
        repair_stop_reason = "validation_complete"
    if report.hard_issues:
        attempts.append({"attempt": 1, "type": "schema_validation", "pass": "verification", "chunk": "final",
                         "message": "; ".join(i.message for i in report.hard_issues), "terminal": True})
    failed = {(a.get("pass"), a.get("chunk")) for a in attempts if a.get("terminal")}
    for c in chunks:
        run.mark_chunk_scan(c.chunk_id, "entity", completed=selection_ok and ("entity", c.chunk_id) not in failed)
    run.absorb_attempts(attempts)
    run.finish(accepted_entities=len(final_entities), accepted_relations=0, stop_reason=repair_stop_reason)
    gaps = [g.to_dict() for g in CoverageAuditor().audit(plan, manager, request, final_entities, [])]
    trace = run.to_trace()
    trace.update(calls=calls, repair_rounds=rounds, repair_stop_reason=repair_stop_reason,
                 verification=report.to_dict())
    result = {"extract_version": "v3.1", "raw": raw, "graph": {"nodes": nodes, "relationships": []}, "triples": [],
        "intermediate_model": {"objects": objects, "relations": [], "coverage_gaps": gaps},
        "warnings": warnings, "attempts": attempts, "evidence_coverage": evidence_coverage(raw),
        "coverage_gaps": gaps, "verification": report.to_dict(),
        "mapping": {"entities": scores, "relations": [], "rejected": sum(s["status"] == "rejected" for s in scores)},
        "profile": {"document_route": route.to_dict(), "pipeline": "entity_property", "chunks": len(chunks),
            "generation_temperature": getattr(llm, 'generation_temperature', None),
            "skill_selection": selection,
            "quality": completeness_metrics(final_entities, plan),
            "prompt_assembly": {"mode": "request_skill_selection", "plans": planner.audit(),
                "calls": [c for c in calls if c.get('pass') == 'skill_selection'],
                "attempts": [a for a in attempts if a.get('pass') == 'skill_selection']},
            "input_manifest": [{"chunk_id": c.chunk_id,
                "sha256": hashlib.sha256(json.dumps(c.to_dict(), ensure_ascii=False,
                    sort_keys=True, default=str).encode('utf-8')).hexdigest()} for c in chunks],
            "entity_candidates": sum(len(es) for _, es in discovered) + sum(a.get('type') == 'candidate_rejected' for a in attempts), "relation_candidates": 0,
            "candidate_selection": {"rejected_entities": sum(a.get('type') == 'candidate_rejected' for a in attempts)},
            "accepted_entities": len(final_entities), "accepted_relations": 0, "property_enrichment": property_stats,
            "mapping_mode": "enforce" if apply_mapping_threshold else "annotate",
            "repair_rounds": rounds, "repair_stop_reason": repair_stop_reason, "llm_calls": len(calls)},
        "status": run.status.value, "run_trace": trace, "error_code": run.error_code}
    if run.error:
        result["_error"] = run.error
    return result
