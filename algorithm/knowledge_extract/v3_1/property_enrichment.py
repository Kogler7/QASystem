"""LLM-authored complete entities keyed by type/name, without ID-based merging."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy

from .candidate_generation import generate_structured, bind_entity_evidence
from .output_contracts import property_entity_schema
from .prompt_router import build_chat_messages


def enrich_entity_properties(entity_results, manager, plan, llm, *, route,
                             assembly_planner=None, attempts, warnings,
                             call_metrics=None, use_scheduler=False,
                             local_previous=1, local_next=1, same_section=True,
                             max_attempts=3, retry_base_seconds=0.05,
                             batch_size=24, max_workers=1):
    """Co-locate exact type/name occurrences; the LLM consolidates their facts.

    Python only schedules keys and source chunks, never combines property values.
    Each key stays in one batch regardless of how many chunks mention it.
    """
    completed = []
    stats = {"calls": 0, "applied": 0, "rejected": 0, "verifications": 0}
    groups = defaultdict(list)
    for chunk_id, entities in entity_results:
        for entity in entities:
            groups[(entity["type"], entity["name"])].append((chunk_id, entity))
    by_type = defaultdict(list)
    for key in groups:
        by_type[key[0]].append(key)
    batches = [keys[start:start + max(1, batch_size)]
               for keys in by_type.values()
               for start in range(0, len(keys), max(1, batch_size))]
    for batch_index, batch_keys in enumerate(batches, 1):
        source_ids = list(dict.fromkeys(cid for key in batch_keys for cid, _ in groups[key]))
        sources = [manager.get_chunk(cid) for cid in source_ids]
        chunk = sources[0]
        batch = [{"type": t, "name": n} for t, n in batch_keys]
        catalog = [{"type": t, "name": n,
                    "properties": {},
                    "occurrences": [{"chunk_id": cid, "properties": deepcopy(e.get("properties") or {}),
                                     "evidence": deepcopy(e.get("evidence") or {})}
                                    for cid, e in groups[(t, n)]],
                    "missing_properties": {p.identifier: {"display_name": p.display_name,
                                                          "data_type": p.data_type}
                        for p in plan.class_specs[t].properties}}
                   for t, n in batch_keys]
        schema = property_entity_schema(batch, plan)
        messages = build_chat_messages(task="property", plan=plan, route=route,
            chunk=chunk, source_chunks=sources, known_entities=catalog, output_schema=schema,
            assembly_planner=assembly_planner)
        label = f"cross-chunk:batch-{batch_index}"
        parsed = generate_structured(llm, messages, schema, use_scheduler=use_scheduler,
            attempts=attempts, pass_name="property", chunk_id=label,
            max_attempts=max_attempts, retry_base_seconds=retry_base_seconds,
            call_metrics=call_metrics)
        stats["calls"] += 1
        if parsed is None:
            continue
        rows = parsed.get("entities", [])
        expected = Counter((e["type"], e["name"]) for e in batch)
        actual = Counter((e["type"], e["name"]) for e in rows)
        error = None
        if actual != expected:
            error = "属性阶段必须为每个唯一 type + name 返回一个完整实体，禁止遗漏、新增或重复"
        for row in rows:
            if error:
                break
            allowed = {p.identifier for p in plan.class_specs[row["type"]].properties}
            if not set(row["properties"]).issubset(allowed):
                error = "属性不属于该实体 type 的本体定义"
        if error:
            stats["rejected"] += len(rows)
            attempts.append({"attempt": 1, "type": "schema_validation", "pass": "property",
                             "chunk": label, "message": error, "terminal": True})
            continue
        # Binding adds provenance only; type/name/properties are copied verbatim
        # from the model. No property patching or entity reconciliation follows.
        completed.extend(bind_entity_evidence(deepcopy(rows), manager, chunk_ids=source_ids))
        stats["applied"] += sum(len(e["properties"]) for e in rows)
    return completed, stats
