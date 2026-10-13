"""Bounded completion of exact type/name keys with verified first-pass facts."""
from __future__ import annotations

from collections import Counter, defaultdict
from bisect import bisect_left
from copy import deepcopy
from dataclasses import replace
import json

from .candidate_generation import generate_structured
from .chunk_context import ExtractionChunk
from .output_contracts import property_entity_schema
from .prompt_router import build_chat_messages
from .property_grounding import ground_properties, merge_evidences
from .request_budget import (fits, input_limit, recover_split_attempts,
                             split_request_failure, window_chars, windows, document_expired)


def _support_sources(entity):
    """Only value-supporting spans go back to the model, never the full history."""
    seen = set()
    for receipt in entity.get('property_evidence', {}).values():
        for span in receipt['sources']:
            # All coordinates remain in the receipts; identical quotations do
            # not need to be resent hundreds of times to the completion model.
            if span['quote'] in seen:
                continue
            seen.add(span['quote'])
            yield ExtractionChunk(span['source_block_ids'][0], span['quote'],
                metadata={'_v31_window_start': span['start_char']})


def enrich_entity_properties(entity_results, manager, plan, llm, *, route,
                             assembly_planner=None, attempts, warnings,
                             call_metrics=None, use_scheduler=False,
                             local_previous=1, local_next=1, same_section=True,
                             max_attempts=3, retry_base_seconds=0.05,
                             batch_size=24, max_workers=1):
    """Copy verified facts; ask the LLM only for missing context or conflicts.

    Keys may span arbitrarily many source windows. Each request carries bounded
    source text plus the current verified state. A failed window retains earlier
    facts and a terminal attempt, rather than dropping the entire entity batch.
    """
    completed = []
    stats = {'calls': 0, 'applied': 0, 'rejected': 0, 'verifications': 0,
             'skipped': 0, 'source_windows': 0}
    groups = defaultdict(list)
    nonempty_chunks = set()
    for chunk_id, entities in entity_results:
        if entities:
            nonempty_chunks.add(chunk_id)
        for entity in entities:
            groups[(entity['type'], entity['name'])].append((chunk_id, entity))
    for key, occurrences in groups.items():
        type_id, name = key
        source_ids = list(dict.fromkeys(cid for cid, _ in occurrences))
        source_id_set = set(source_ids)
        candidates = [deepcopy(row) for _, row in occurrences]
        for (cid, _), row in zip(occurrences, candidates):
            # Direct callers/repair callbacks do not get to manufacture receipts.
            if 'property_evidence' not in row:
                ground_properties([row], [manager.get_chunk(cid)], llm,
                    attempts=attempts, warnings=warnings, use_scheduler=use_scheduler,
                    max_attempts=max_attempts, retry_base_seconds=retry_base_seconds,
                    call_metrics=call_metrics, plan=plan, route=route, assembly_planner=assembly_planner)
        state = {'type': type_id, 'name': name, 'properties': {}, 'property_evidence': {},
                 'evidence': deepcopy(candidates[0].get('evidence') or {}),
                 'evidences': merge_evidences(*(row.get('evidences', []) for row in candidates))}
        conflicts = set()
        for row in candidates:
            for prop, value in row.get('properties', {}).items():
                if prop in state['properties'] and state['properties'][prop] != value:
                    conflicts.add(prop)
                elif prop in state['properties']:
                    receipt = state['property_evidence'][prop]
                    receipt['sources'] = merge_evidences(receipt['sources'], row['property_evidence'][prop]['sources'])
                else:
                    state['properties'][prop] = deepcopy(value)
                    state['property_evidence'][prop] = deepcopy(row['property_evidence'][prop])
        for prop in conflicts:
            state['properties'].pop(prop, None)
            state['property_evidence'].pop(prop, None)
        full_sources = {cid for (cid, _), row in zip(occurrences, candidates) if not row.get('_properties_scanned')}
        needed = set(full_sources)
        if conflicts:
            needed.update(source_ids)
        # A candidate-less neighbour may contain pronouns, continuation clauses,
        # or table notes. Never cross document or (by default) section boundaries.
        if plan.class_specs[type_id].properties:
            for cid in source_ids:
                for neighbour in manager.local_context(cid, previous=local_previous,
                        next=local_next, same_section=same_section):
                    if neighbour.chunk_id not in source_id_set and neighbour.chunk_id not in nonempty_chunks:
                        needed.update((cid, neighbour.chunk_id))
                        full_sources.update((cid, neighbour.chunk_id))
        else:
            needed.clear()
        if not needed:
            stats['skipped'] += 1
            completed.append(state)
            continue

        # Index spans once. Scanning all occurrences again for every window of
        # a very long chunk would make a frequent key quadratic in text size.
        occurrence_index = defaultdict(list)
        indexed = set()
        for (cid, _), row in zip(occurrences, candidates):
            evidence = row.get('evidence') or {}
            quote = evidence.get('quote', '')
            signature = (cid, repr(row.get('properties')), quote)
            if quote and evidence.get('start_char') is not None and signature not in indexed:
                indexed.add(signature)
                occurrence_index[cid].append((evidence['start_char'], evidence['end_char'], row))
        for cid, entries in occurrence_index.items():
            entries.sort(key=lambda item: item[0])
        starts = {cid: [entry[0] for entry in entries] for cid, entries in occurrence_index.items()}
        budget_exhausted = False

        def process(sources, depth=0):
            nonlocal state, budget_exhausted
            if budget_exhausted:
                return
            if document_expired(llm):
                attempts.append({'attempt': 1, 'type': 'timeout', 'pass': 'property',
                    'chunk': sources[0].chunk_id, 'message': 'document deadline exhausted',
                    'retryable': False, 'terminal': True})
                budget_exhausted = True
                return
            support_sources = list(_support_sources(state))
            documents = sources + support_sources
            # Include only occurrences whose quote appears in these windows.
            # Full occurrence/provenance lists remain outside the prompt.
            selected, seen = [], set()
            for source in sources:
                cid = source.chunk_id
                entries = occurrence_index.get(cid, [])
                start = int(source.metadata.get('_v31_window_start', 0))
                end = start + len(source.text)
                lo = bisect_left(starts.get(cid, []), start)
                hi = bisect_left(starts.get(cid, []), end)
                for _, span_end, row in entries[lo:hi]:
                    if span_end > end:
                        continue
                    quote = row['evidence']['quote']
                    signature = repr((row.get('properties'), quote))
                    if signature not in seen:
                        seen.add(signature)
                        selected.append({'properties': row.get('properties') or {}, 'evidence': {'quote': quote}})
            known = [{'type': type_id, 'name': name, 'properties': state['properties'],
                'occurrences': selected,
                'missing_properties': {p.identifier: {'display_name': p.display_name, 'data_type': p.data_type}
                    for p in plan.class_specs[type_id].properties if p.identifier not in state['properties']}}]
            schema = property_entity_schema(known, plan)
            messages = build_chat_messages(task='property', plan=plan, route=route,
                chunk=sources[0], source_chunks=documents, known_entities=known,
                output_schema=schema, assembly_planner=assembly_planner)
            label = f"{sources[0].chunk_id}:property"

            def validate(payload):
                actual = Counter((e['type'], e['name']) for e in payload.get('entities', []))
                return [] if actual == Counter([key]) else ['必须为每个唯一 type + name 返回一次；禁止遗漏、新增、重复或重命名']

            start = len(attempts)
            parsed = None
            too_big = not fits(messages, llm)
            if too_big:
                minimum = build_chat_messages(task='property', plan=plan, route=route,
                    chunk=ExtractionChunk(sources[0].chunk_id, ''),
                    source_chunks=support_sources or None,
                    known_entities=[{**known[0], 'occurrences': []}],
                    output_schema=schema, assembly_planner=assembly_planner)
                if not fits(minimum, llm):
                    attempts.append({'attempt': 0, 'type': 'input_budget', 'pass': 'property',
                        'chunk': label, 'message': 'verified state/schema exceeds budget; remaining windows deferred',
                        'terminal': True})
                    budget_exhausted = True
                    return
            if not too_big:
                stats['calls'] += 1
                parsed = generate_structured(llm, messages, schema, use_scheduler=use_scheduler,
                    attempts=attempts, pass_name='property', chunk_id=label,
                    max_attempts=max_attempts, retry_base_seconds=retry_base_seconds,
                    call_metrics=call_metrics, validator=validate,
                    split_on_overflow=depth < 12 and (len(sources) > 1 or len(sources[0].text) > 256))
            if too_big or (parsed is None and split_request_failure(attempts[start:])):
                if document_expired(llm):
                    budget_exhausted = True
                    return
                if depth < 12 and (len(sources) > 1 or len(sources[0].text) > 256):
                    recover_split_attempts(attempts[start:])
                    if len(sources) > 1:
                        middle = len(sources) // 2
                        process(sources[:middle], depth + 1)
                        process(sources[middle:], depth + 1)
                    else:
                        for part in windows(sources[0], max(128, len(sources[0].text) // 2)):
                            process([part], depth + 1)
                    return
                if too_big:
                    attempts.append({'attempt': 0, 'type': 'input_budget', 'pass': 'property',
                        'chunk': label, 'message': 'source/schema/verified state exceeds request budget', 'terminal': True})
                return
            if parsed is None:
                stats['rejected'] += 1
                return
            before = len(call_metrics or [])
            rows = ground_properties(deepcopy(parsed['entities']), documents, llm,
                attempts=attempts, warnings=warnings, use_scheduler=use_scheduler,
                max_attempts=max_attempts, retry_base_seconds=retry_base_seconds, call_metrics=call_metrics, prior=state, plan=plan,
                route=route, assembly_planner=assembly_planner)
            stats['verifications'] += len(call_metrics or []) - before
            row = rows[0]
            # Preserve previously verified values omitted by a later local window.
            # New conflicting values are model-authored using both old support and
            # the current text. Never concatenate or invent property content here.
            state['properties'].update(row['properties'])
            state['property_evidence'].update(row['property_evidence'])
            state['evidences'] = merge_evidences(state['evidences'], row.get('evidences', []))
            if not state['evidence'].get('span_id') and row.get('evidence'):
                state['evidence'] = row['evidence']
            stats['applied'] += len(row['properties'])

        packet, size = [], 0
        text_limit = min(window_chars(llm) * 2, max(256, input_limit(llm) // 6))
        limit = min(window_chars(llm), text_limit)

        def source_windows():
            # Every source was already scanned when only a property conflict
            # remains. Revisit the supporting passages, not the whole document
            # once per key. Preserve distinct values and surrounding conditions.
            seen = set()
            by_source = defaultdict(list)
            for (cid, _), row in zip(occurrences, candidates):
                by_source[cid].append(row)
            for cid in sorted(needed, key=manager.index_of):
                source = manager.get_chunk(cid)
                if cid in full_sources:
                    yield from windows(source, limit)
                    continue
                for row in by_source[cid]:
                    for prop in conflicts & row['property_evidence'].keys():
                        for span in row['property_evidence'][prop]['sources']:
                            support_source = manager.get_chunk(span['source_block_ids'][0])
                            start = max(0, span['start_char'] - 200)
                            end = min(len(support_source.text), span['end_char'] + 200)
                            excerpt = replace(support_source, text=support_source.text[start:end],
                                metadata={**support_source.metadata, '_v31_window_start': start})
                            signature = (excerpt.text, json.dumps(row['properties'], ensure_ascii=False, sort_keys=True))
                            if signature in seen:
                                continue
                            seen.add(signature)
                            yield from windows(excerpt, limit)

        for part in source_windows():
            if budget_exhausted:
                break
            if packet and (size + len(part.text) > text_limit or len(packet) >= max(1, batch_size)):
                process(packet)
                packet, size = [], 0
            packet.append(part)
            size += len(part.text)
            stats['source_windows'] += 1
        if packet:
            process(packet)
        unresolved = sorted(conflicts - state['properties'].keys())
        if unresolved:
            from .graph_utils import append_warning
            append_warning(warnings, 'PROPERTY_CONFLICT_UNRESOLVED',
                '属性冲突仍未消解，保留实体及证据，不任意选择值', f'{type_id}::{name}', unresolved)
            attempts.append({'attempt': 1, 'type': 'schema_validation', 'pass': 'property',
                'chunk': source_ids[0], 'message': 'conflicting property values remain unresolved',
                'terminal': True})
        completed.append(state)
    return completed, stats
