"""Bounded completion of exact type/name keys with verified first-pass facts."""
from __future__ import annotations

from collections import Counter, defaultdict
from bisect import bisect_left
from copy import deepcopy
from dataclasses import replace
import json
from docker.configuration import getenv as config_getenv, declarations

from .candidate_generation import generate_structured, _append_call
from .chunk_context import ExtractionChunk
from .output_contracts import property_entity_schema, validate_property_proposals
from .prompt_router import build_chat_messages
from .property_grounding import ground_properties, merge_evidences
from .request_budget import (fits, input_limit, recover_split_attempts,
                             split_request_failure, window_chars, windows, document_expired, RecoveryBudget)
from .source_windows import source_parts, table_windows
from .task_runtime import synchronous, invoke, TaskBatcher
import asyncio


def _support_sources(entity, manager):
    """Only value-supporting spans go back to the model, never the full history."""
    seen = set()
    receipts = list(entity.get('property_evidence', {}).values())
    if entity.get('evidence', {}).get('span_id'):
        receipts.append({'sources': [entity['evidence']]})
    for receipt in receipts:
        for span in receipt['sources']:
            # All coordinates remain in the receipts; identical quotations do
            # not need to be resent hundreds of times to the completion model.
            source = manager.get_chunk(span['source_block_ids'][0])
            rows = table_windows(source, 0)
            containing = next((row for row in rows or [] if any(
                left <= span['start_char'] < span['end_char'] <= right
                for left, right in row.metadata.get('_v31_table_rows', []))), None)
            if containing is not None:
                for part in source_parts(containing):
                    if part.text not in seen:
                        seen.add(part.text)
                        yield part
                continue
            left, right = max(0, span['start_char'] - 200), min(len(source.text), span['end_char'] + 200)
            text = source.text[left:right]
            if text in seen:
                continue
            seen.add(text)
            yield replace(source, text=text, metadata={**source.metadata, '_v31_window_start': left})


@synchronous
async def enrich_entity_properties(entity_results, manager, plan, llm, *, route,
                             assembly_planner=None, attempts, warnings,
                             call_metrics=None, use_scheduler=False,
                             local_previous=1, local_next=1, same_section=True,
                             max_attempts=3, retry_base_seconds=0.05,
                             batch_size=None, max_workers=1, task='property', repair_issues=None):
    """Copy verified facts; ask the LLM only for missing context or conflicts.

    Keys may span arbitrarily many source windows. Each request carries bounded
    source text plus the current verified state. A failed window retains earlier
    facts and a terminal attempt, rather than dropping the entire entity batch.
    """
    if batch_size is None:
        batch_size = config_getenv('KG_EXTRACT_V31_PROPERTY_BATCH_SIZE',
            declarations()['items']['KG_EXTRACT_V31_PROPERTY_BATCH_SIZE']['default'])
    batch_size = max(1, min(24, int(batch_size)))
    completed = {}
    stats = {'calls': 0, 'applied': 0, 'rejected': 0, 'verifications': 0,
             'skipped': 0, 'source_windows': 0}
    groups = defaultdict(list)
    for chunk_id, entities in entity_results:
        for entity in entities:
            groups[(entity['type'], entity['name'])].append((chunk_id, entity))
    async def complete_key(key, occurrences):
        type_id, name = key
        source_ids = list(dict.fromkeys(cid for cid, _ in occurrences))
        source_id_set = set(source_ids)
        candidates = [deepcopy(row) for _, row in occurrences]
        for (cid, _), row in zip(occurrences, candidates):
            # Direct callers/repair callbacks do not get to manufacture receipts.
            if 'property_evidence' not in row:
                await invoke(ground_properties, [row], [manager.get_chunk(cid)], llm,
                    attempts=attempts, warnings=warnings, use_scheduler=use_scheduler,
                    max_attempts=max_attempts, retry_base_seconds=retry_base_seconds,
                    call_metrics=call_metrics, plan=plan, route=route, assembly_planner=assembly_planner)
        state = {'type': type_id, 'name': name, 'properties': {}, 'property_evidence': {},
                 'evidence': deepcopy(candidates[0].get('evidence') or {}),
                 'evidences': merge_evidences(*(row.get('evidences', []) for row in candidates))}
        receipt = next((row['entity_evidence'] for row in candidates if row.get('entity_evidence')), None)
        if receipt:
            state['entity_evidence'] = deepcopy(receipt)
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
        conflict_values = {prop: [] for prop in conflicts}
        conflict_sources = {}
        for row in candidates:
            for prop in conflicts & row['properties'].keys():
                if row['properties'][prop] not in conflict_values[prop]:
                    conflict_values[prop].append(deepcopy(row['properties'][prop]))
                for source in _support_sources({**row, 'property_evidence': {
                        prop: row['property_evidence'][prop]}}, manager):
                    conflict_sources.setdefault(source.text, source)
        unresolved_conflicts = set(conflicts)
        full_sources = {cid for (cid, _), row in zip(occurrences, candidates) if not row.get('_properties_scanned')}
        attempted = {p for row in candidates for p in row.get('_attempted_properties', row.get('properties', {}))}
        missing = {p.identifier for p in plan.class_specs[type_id].properties} - attempted
        if missing:
            for cid in source_ids:
                ranges = sorted(row.get('_scanned_range', (0, len(manager.get_chunk(cid).text)))
                                for source_id, row in occurrences if source_id == cid)
                covered = 0
                for left, right in ranges:
                    if left > covered:
                        break
                    covered = max(covered, right)
                if covered < len(manager.get_chunk(cid).text):
                    full_sources.add(cid)
        needed = set(full_sources)
        if conflicts:
            needed.update(source_ids)
        # A neighbour may contain other entities AND pronouns/continuation clauses,
        # or table notes. Never cross document or (by default) section boundaries.
        missing_verified = {p.identifier for p in plan.class_specs[type_id].properties} - state['properties'].keys()
        if plan.class_specs[type_id].properties and (full_sources or conflicts or missing_verified):
            for cid in source_ids:
                for neighbour in manager.local_context(cid, previous=local_previous,
                        next=local_next, same_section=same_section):
                    if neighbour.chunk_id not in source_id_set:
                        needed.update((cid, neighbour.chunk_id))
                        full_sources.update((cid, neighbour.chunk_id))
        elif not plan.class_specs[type_id].properties:
            needed.clear()
        if not needed:
            stats['skipped'] += 1
            completed[key] = state
            return

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

        async def process(sources, depth=0, *, focused_conflict=False):
            nonlocal state, budget_exhausted
            if budget_exhausted:
                return
            if document_expired(llm):
                attempts.append({'attempt': 1, 'type': 'timeout', 'pass': 'property',
                    'chunk': sources[0].chunk_id, 'message': 'document deadline exhausted',
                    'retryable': False, 'terminal': True})
                budget_exhausted = True
                return
            support_sources = list(_support_sources(state, manager))
            support_sources += [s for s in conflict_sources.values()
                                if s.text not in {p.text for p in support_sources}]
            documents = [p for s in sources for p in source_parts(s)]
            for source in support_sources:
                if not any(source.text in present.text for present in documents):
                    documents.append(source)
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
            # Every conflict comparison sees all distinct values and their source
            # passages, including when ordinary source windows have been split.
            for row in candidates:
                values = {p: v for p, v in row['properties'].items() if p in conflicts}
                entry = {'properties': values, 'evidence': {'quote': row.get('evidence', {}).get('quote', '')}}
                if values and entry not in selected:
                    selected.append(entry)
            known = [{'type': type_id, 'name': name, 'properties': state['properties'],
                'occurrences': selected,
                'missing_properties': {p.identifier: {'display_name': p.display_name, 'data_type': p.data_type}
                    for p in plan.class_specs[type_id].properties if p.identifier not in state['properties']}}]
            request_task = 'repair' if focused_conflict else task
            pass_name = ('repair_property' if request_task == 'repair' else
                         'property_conflict' if unresolved_conflicts else 'property')
            request_issues = repair_issues
            if focused_conflict:
                known[0]['requested_properties'] = sorted(unresolved_conflicts)
                request_issues = [{'code': 'PROPERTY_CONFLICT_UNRESOLVED', 'repair_type': 'property_conflict',
                    'context': {'target_type': type_id, 'evidence': name,
                                'required': sorted(unresolved_conflicts)}}]
            elif task == 'repair':
                requested = {prop for issue in repair_issues or []
                    if (issue.get('context', {}).get('target_type'), issue.get('context', {}).get('evidence')) == key
                    for prop in issue.get('context', {}).get('required', [])}
                known[0]['requested_properties'] = sorted(requested)
            schema = property_entity_schema(known, plan)
            messages = build_chat_messages(task=request_task, plan=plan, route=route,
                chunk=sources[0], source_chunks=documents, known_entities=known,
                output_schema=schema, assembly_planner=assembly_planner, repair_issues=request_issues)
            label = f"{sources[0].chunk_id}:property"

            parsed = None
            too_big = not fits(messages, llm)
            if too_big:
                minimum = build_chat_messages(task=request_task, plan=plan, route=route,
                    chunk=ExtractionChunk(sources[0].chunk_id, ''),
                    source_chunks=support_sources or None,
                    known_entities=[{**known[0], 'occurrences': []}],
                    output_schema=schema, assembly_planner=assembly_planner, repair_issues=request_issues)
                if not fits(minimum, llm):
                    attempts.append({'attempt': 0, 'type': 'input_budget', 'pass': 'property',
                        'chunk': label, 'message': 'verified state/schema exceeds budget; remaining windows deferred',
                        'terminal': True})
                    budget_exhausted = True
                    return
            if not too_big:
                ticket = {'key': key, 'messages': messages, 'schema': schema,
                    'task': request_task, 'repair_issues': request_issues,
                    'pass_name': pass_name,
                    'known': known, 'documents': documents, 'sources': sources,
                    'prior': state, 'conflicts': conflict_values,
                    'label': label,
                    'split': depth < 12 and (len(sources) > 1 or len(sources[0].text) > 256)}
                signature = (request_task, pass_name, key[0], tuple((source.chunk_id, source.metadata.get('_v31_window_start', 0), source.text)
                                           for source in sources))
                parsed, request_failures = await batcher.submit(signature, ticket)
            if too_big or (parsed is None and split_request_failure(request_failures)):
                if document_expired(llm):
                    budget_exhausted = True
                    return
                if depth < 12 and (len(sources) > 1 or (len(sources[0].text) > 256 and not sources[0].metadata.get('_v31_atomic_table'))):
                    recover_split_attempts(request_failures if not too_big else [])
                    if len(sources) > 1:
                        middle = len(sources) // 2
                        await process(sources[:middle], depth + 1, focused_conflict=focused_conflict)
                        await process(sources[middle:], depth + 1, focused_conflict=focused_conflict)
                    else:
                        for part in windows(sources[0], max(128, len(sources[0].text) // 2)):
                            await process([part], depth + 1, focused_conflict=focused_conflict)
                    return
                if too_big:
                    attempts.append({'attempt': 0, 'type': 'input_budget', 'pass': 'property',
                        'chunk': label, 'message': 'source/schema/verified state exceeds request budget', 'terminal': True})
                return
            if parsed is None:
                stats['rejected'] += 1
                return
            row = parsed['entities'][0]
            for prop in conflicts:
                if prop in row['properties']:
                    unresolved_conflicts.discard(prop)
                elif prop in unresolved_conflicts:
                    # Empty delta cannot resolve a pending conflict. Once a
                    # proposal passed review with ALL conflicting passages,
                    # subsequent no-change deltas do not delete that receipt.
                    state['properties'].pop(prop, None)
                    state['property_evidence'].pop(prop, None)
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
            # Entity batching must not shrink an entity's source coverage packet.
            # Retain the existing 24-window packing bound and text budget.
            if packet and (size + len(part.text) > text_limit or len(packet) >= 24):
                await process(packet)
                packet, size = [], 0
            packet.append(part)
            size += len(part.text)
            stats['source_windows'] += 1
        if packet:
            await process(packet)
        # One targeted pass uses only the complete conflicting support set.
        # It cannot rename the entity, choose a value without review, or retry forever.
        if unresolved_conflicts and conflict_sources and not budget_exhausted:
            await process(list(conflict_sources.values()), focused_conflict=True)
        unresolved = sorted(unresolved_conflicts)
        if unresolved:
            from .graph_utils import append_warning
            append_warning(warnings, 'PROPERTY_CONFLICT_UNRESOLVED',
                '属性冲突仍未消解，保留实体及证据，不任意选择值', f'{type_id}::{name}', unresolved)
            attempts.append({'attempt': 1, 'type': 'schema_validation', 'pass': 'property',
                'chunk': source_ids[0], 'message': 'conflicting property values remain unresolved',
                'terminal': True})
        completed[key] = state

    def packet_for(tickets):
        known = [ticket['known'][0] for ticket in tickets]
        documents = {}
        for ticket in tickets:
            for source in ticket['documents']:
                documents[(source.chunk_id, source.metadata.get('_v31_window_start', 0), source.text)] = source
        schema = property_entity_schema(known, plan)
        request_task = tickets[0].get('task', task)
        request_issues = [issue for ticket in tickets for issue in ticket.get('repair_issues') or []]
        messages = build_chat_messages(task=request_task, plan=plan, route=route,
            chunk=tickets[0]['sources'][0], source_chunks=list(documents.values()),
            known_entities=known, output_schema=schema, assembly_planner=assembly_planner, repair_issues=request_issues)
        return messages, schema

    async def execute(tickets, job):
        # Source coverage remains sequential for each entity. Only independent
        # keys reading the same source windows share a completion request.
        messages, schema = (tickets[0]['messages'], tickets[0]['schema']) if len(tickets) == 1 else packet_for(tickets)
        if len(tickets) > 1 and not fits(messages, llm):
            middle = len(tickets) // 2
            return await execute(tickets[:middle], job) + await execute(tickets[middle:], job)
        expected = Counter(ticket['key'] for ticket in tickets)
        def validate(payload):
            actual = Counter((row['type'], row['name']) for row in payload.get('entities', []))
            return validate_rows(payload) if actual == expected else ['必须为每个唯一 type + name 返回一次；禁止遗漏、新增、重复或重命名']
        def validate_rows(payload):
            errors = validate_property_proposals(payload)
            targets = {ticket['key']: ticket['known'][0] for ticket in tickets}
            for row in payload.get('entities', []):
                target = targets.get((row['type'], row['name']))
                if target is None:
                    errors.append('unknown entity key')
                elif 'requested_properties' in target and set(row['properties']) - set(target['requested_properties']):
                    errors.append('repair property outside requested scope')
            return errors
        job['calls'] += 1
        local_attempts, local_warnings, local_metrics = job['attempts'], job['warnings'], job['metrics']
        before = len(local_attempts)
        parsed = await invoke(generate_structured, llm, messages, schema, use_scheduler=use_scheduler,
            attempts=local_attempts, pass_name=tickets[0]['pass_name'], chunk_id=tickets[0]['label'],
            max_attempts=max_attempts, retry_base_seconds=retry_base_seconds,
            call_metrics=local_metrics, validator=validate,
            item_validator=validate_rows,
            recovery_budget=job['recovery_budget'],
            split_on_overflow=any(ticket['split'] for ticket in tickets))
        recovered_keys = {(row['type'], row['name']) for row in (parsed or {}).get('entities', [])}
        remaining = [ticket for ticket in tickets if ticket['key'] not in recovered_keys]
        resumed = []
        if remaining and len(tickets) > 1 and split_request_failure(local_attempts[before:]) and not document_expired(llm):
            recover_split_attempts(local_attempts[before:])
            middle = max(1, len(remaining) // 2)
            resumed = await execute(remaining[:middle], job)
            if remaining[middle:]:
                resumed += await execute(remaining[middle:], job)
        failures = local_attempts[before:]
        documents = {}
        for ticket in tickets:
            for source in ticket['documents']:
                documents[(source.chunk_id, source.metadata.get('_v31_window_start', 0), source.text)] = source
        verify_start = len(local_metrics)
        grounded = await invoke(ground_properties, deepcopy((parsed or {}).get('entities', [])), list(documents.values()), llm,
            attempts=local_attempts, warnings=local_warnings, use_scheduler=use_scheduler,
            max_attempts=max_attempts, retry_base_seconds=retry_base_seconds,
            call_metrics=local_metrics, plan=plan, route=route, assembly_planner=assembly_planner,
            max_workers=job['verification_workers'],
            priors={ticket['key']: ticket['prior'] for ticket in tickets},
            conflicts_by_key={ticket['key']: ticket['conflicts'] for ticket in tickets})
        job['verifications'] += len(local_metrics) - verify_start
        rows = {(row['type'], row['name']): row for row in grounded}
        resumed_keys = {ticket['key'] for ticket, _ in resumed}
        return [(ticket, ({'entities': [rows[ticket['key']]]} if ticket['key'] in rows else None, failures))
                for ticket in tickets if ticket['key'] not in resumed_keys] + resumed

    jobs = []
    async def run_packet(tickets):
        # Local diagnostics merge by submission order; task completion stays dynamic.
        job = {'attempts': [], 'warnings': [], 'metrics': [], 'calls': 0, 'verifications': 0,
               'recovery_budget': RecoveryBudget(max_attempts, len(tickets)),
               'verification_workers': max(1, max_workers)}
        jobs.append(job)
        return await execute(tickets, job)
    batcher = TaskBatcher(run_packet, batch_size)
    key_tasks = [asyncio.create_task(complete_key(key, occurrences)) for key, occurrences in groups.items()]
    try:
        await asyncio.gather(*key_tasks)
    finally:
        for task in key_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*key_tasks, return_exceptions=True)
        await batcher.close()
    for job in jobs:
        attempts.extend(job['attempts'])
        warnings.extend(job['warnings'])
        if call_metrics is not None:
            for call in job['metrics']:
                _append_call(call_metrics, call)
        stats['calls'] += job['calls']
        stats['verifications'] += job['verifications']
    return [completed[key] for key in groups], stats
