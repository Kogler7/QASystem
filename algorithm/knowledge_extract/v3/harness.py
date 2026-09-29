"""Bounded observe/propose/validate/amend loop; no domain prompt router."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from collections import defaultdict

from .client import JSONClient, ProviderConfig
from .context import plan_observations
from .facts import Registry, RELATION_FIELDS, coalesce_results, display_property_names
from .prompts import build_messages, repair_messages, PROMPT_VERSION
from .schema import compile_ontology


def _bounded_context(known, windows, limit=160):
    """Keep named cross-window references useful without resending whole documents."""
    source = '\n'.join(window['text'] for window in windows)
    ranked = sorted(enumerate(known),key=lambda pair:(pair[1]['name'] in source, pair[0]<3, pair[0]),reverse=True)
    selected = []
    for _,entity in ranked[:limit]:
        item = dict(entity)
        item['properties'] = {key:value for key,value in entity['properties'].items() if len(json.dumps(value,ensure_ascii=False))<=512}
        omitted = set(entity['properties'])-set(item['properties'])
        if omitted:
            item['omitted_property_keys'] = sorted(omitted)
        selected.append(item)
    return selected


def _review_context(entities, planned, limit):
    """Attach full properties and matching original excerpts to each review node."""
    windows = [window for observation in planned for window in observation['source_windows']]
    candidates = []
    for entity in sorted(entities, key=lambda item:(item['type'],item['name']))[:limit]:
        sources = {}
        for ref in entity.get('source_refs', []):
            matches = [window for window in windows if window['chunk_id'] == ref.get('chunk_id')
                       and ('start_offset' not in ref or window['start_offset'] == ref['start_offset'])]
            for window in matches:
                key = (window['chunk_id'],window['start_offset'],window['end_offset'])
                if key in sources:
                    continue
                text = window['text']
                needle = ref.get('quote') or entity['name']
                position = text.find(needle)
                lo = max(0,position-250) if position >= 0 else 0
                hi = min(len(text),lo+1200)
                sources[key] = {'chunk_id':window['chunk_id'], 'text':text[lo:hi],
                    'start_offset':window['start_offset']+lo,'end_offset':window['start_offset']+hi,
                    'excerpt_truncated':lo>0 or hi<len(text), 'contains_mention':position>=0}
        ordered = sorted(sources.values(),key=lambda item:not item['contains_mention'])
        if not ordered:
            continue  # Never offer a node for semantic merging without its text.
        candidates.append({'type':entity['type'],'name':entity['name'],
                           'properties':entity['properties'], 'alternatives':entity.get('alternatives', []),
                           'source_texts':ordered[:2], 'source_texts_omitted':max(0,len(ordered)-2)})
    return candidates


def extract_detailed(chunks, ontology: str, *, llm=None, api_config='eval_api.md', audit: bool = True,
                     window_chars: int = 3000, max_calls: int | None = None,
                     max_input_bytes: int = 128000, max_total_tokens: int | None = None,
                     max_workers: int = 4, semantic_review: bool = True,
                     review_min_entities: int = 40, review_max_entities: int = 160) -> dict:
    """Extract sparse scored facts from upstream chunks.

    Input budgets use UTF-8 bytes as a conservative token upper bound. This
    deliberately exposes an incomplete result instead of truncating context.
    Evidence is a locator only. ``confidence`` is not a calibrated probability.
    """
    started = time.monotonic()
    compiled = compile_ontology(ontology)
    observations, diagnostics = plan_observations(chunks, window_chars=window_chars)
    documents = defaultdict(list)
    for observation in observations:
        documents[observation['document_id']].append(observation)
    hard_limit = 2 * len(observations) + (4 if semantic_review else 3) * len(documents)
    call_limit = hard_limit if max_calls is None else min(hard_limit, max(0, max_calls))
    client = llm if llm is not None else JSONClient(ProviderConfig.from_file(api_config))
    output_reserve = getattr(getattr(client, 'config', None), 'max_tokens', 16384)
    stats = {'calls':0, 'call_limit':call_limit, 'observations':len(observations),
             'documents':len(documents), 'audit_calls':0, 'repair_calls':0,
             'review_calls':0, 'merged_entities':0,
             'reserved_tokens':0, 'prompt_version':PROMPT_VERSION}
    events, entities, relations = [], [], []
    failed = 0
    budget_lock = threading.Lock()

    def invoke(messages, kind, observation):
        size = len(json.dumps(messages, ensure_ascii=False).encode())
        code = None
        with budget_lock:
            if size > max_input_bytes:
                code = 'INPUT_BUDGET_EXCEEDED'
            elif stats['calls'] >= call_limit:
                code = 'CALL_BUDGET_EXHAUSTED'
            elif max_total_tokens is not None and stats['reserved_tokens'] + size + output_reserve > max_total_tokens:
                code = 'TOKEN_BUDGET_EXHAUSTED'
            if code:
                return None, [{'path':'request','code':code,'message':'request not sent'}], False
            stats['calls'] += 1
            stats['reserved_tokens'] += size + output_reserve
            stats[kind+'_calls'] = stats.get(kind+'_calls', 0) + 1
            event = {'observation_id':observation['id'], 'document_id':observation['document_id'], 'kind':kind, 'input_bytes':size}
            events.append(event)
        try:
            raw = client(messages)
        except Exception as error:
            # Never persist exception text: provider bodies may contain credentials.
            code = getattr(error, 'code', 'TRANSPORT_ERROR')
            if not isinstance(code, str) or not (code.startswith('HTTP_') and code[5:].isdigit() or code in {'TRANSPORT_ERROR','PROVIDER_NO_CHOICES','PROVIDER_INVALID_RESPONSE'}):
                code = 'LLM_ERROR'
            event['status'] = code
            return None, [{'path':'response','code':code,'message':'LLM request failed'}], bool(getattr(error,'retryable', True))
        text = getattr(raw, 'text', None)
        if text is None:
            text = json.dumps(raw, ensure_ascii=False) if isinstance(raw, dict) else str(raw)
        event['usage'] = getattr(raw, 'usage', None)
        event['finish_reason'] = getattr(raw,'finish_reason',None)
        if event['finish_reason'] == 'length' or getattr(raw,'status',None) == 'incomplete':
            event['status'] = 'OUTPUT_TRUNCATED'
            return text, [{'path':'response','code':'OUTPUT_TRUNCATED','message':'response not complete; no partial JSON accepted'}], False
        if event['finish_reason'] not in {None,'stop'}:
            event['status'] = 'NONFINAL_RESPONSE'
            return text, [{'path':'response','code':'NONFINAL_RESPONSE','message':'provider did not finish normally'}], False
        event['status'] = 'received'
        return text, [], False

    def run_document(item):
        document_id, planned = item
        failed = 0
        document_diagnostics = []
        registry = Registry(compiled, document_id)
        repair_used = 0

        def process(observation, *, task='extract', kind='extract', allow_repair=True):
            nonlocal repair_used
            windows = observation['source_windows']
            all_known = registry.known_entities()
            known = _bounded_context(all_known, windows)
            issues = [{'code':'COVERAGE_AUDIT','path':'source_windows','message':'逐行检查遗漏的实体、有效属性、跨段关系；只补充有依据的事实，允许无增量。'}] if task == 'amend' else []
            def messages_for_context():
                keys = {(entity['type'],entity['name']) for entity in known}
                links = [{key:relation[key] for key in RELATION_FIELDS} for relation in registry.relations
                         if (relation['from_type'],relation['from_name']) in keys
                         and (relation['to_type'],relation['to_name']) in keys] if task == 'amend' else []
                return build_messages(compiled.prompt_contract,windows,known,task=task,issues=issues,accepted_relations=links)
            messages = messages_for_context()
            while known and len(json.dumps(messages,ensure_ascii=False).encode()) > max_input_bytes:
                known.pop()
                messages = messages_for_context()
            if len(known)<len(all_known):
                document_diagnostics.append({'code':'REGISTRY_CONTEXT_BOUNDED','observation_id':observation['id'],'included':len(known),'total':len(all_known)})
            attempts = 0
            previous_failure = None
            while True:
                if kind == 'repair':
                    if repair_used >= 2:
                        document_diagnostics.append({'code':'REPAIR_BUDGET_EXHAUSTED','observation_id':observation['id']})
                        return False
                    repair_used += 1
                text, errors, retryable = invoke(messages, kind, observation)
                attempts += 1
                if text is None and retryable and kind == 'extract' and attempts < 2:
                    document_diagnostics.extend(dict(error, observation_id=observation['id'],attempt=attempts) for error in errors)
                    continue
                if text is not None and not errors:
                    try:
                        payload = json.loads(text, parse_float=str)
                        if not isinstance(payload, dict) or not all(isinstance(payload.get(key), list) for key in ('entities','relations')):
                            errors = [{'path':'response','code':'INVALID_COLLECTIONS','message':'JSON must contain entities and relations arrays'}]
                        else:
                            errors = registry.apply(payload, windows)
                    except (ValueError, TypeError):
                        errors = [{'path':'response','code':'INVALID_JSON','message':'return one complete JSON object'}]
                if not errors:
                    return True
                document_diagnostics.extend(dict(error, observation_id=observation['id']) for error in errors)
                blocking = [error for error in errors if error['code'] not in {'PROPERTY_CONFLICT'}]
                if not blocking:
                    return True
                progress = (tuple((error['path'],error['code']) for error in blocking),len(registry.entities),len(registry.relations),sum(len(e['properties']) for e in registry.entities))
                if progress == previous_failure:
                    document_diagnostics.append({'code':'NO_REPAIR_PROGRESS','observation_id':observation['id']})
                    return False
                previous_failure = progress
                if text is None or not allow_repair or repair_used >= 2 or stats['calls'] >= call_limit:
                    return False
                if any(error['code'] == 'OUTPUT_TRUNCATED' for error in errors):
                    main = [window for window in windows if not window.get('context_only')]
                    if len(main) != 1 or len(main[0]['text']) < 256 or repair_used > 0:
                        return False
                    # Two smaller observations consume the document's two repair slots.
                    original = main[0]
                    midpoint = len(original['text']) // 2
                    results = []
                    for part, (lo, hi) in enumerate(((0,midpoint),(midpoint,len(original['text'])))):
                        smaller = dict(original, text=original['text'][lo:hi], start_offset=original['start_offset']+lo, end_offset=original['start_offset']+hi)
                        if original.get('document_start_offset') is not None:
                            smaller['document_start_offset'] = original['document_start_offset']+lo
                        results.append(process(dict(observation, id=f"{observation['id']}:split{part}", source_windows=[smaller]), kind='repair',allow_repair=False))
                    return all(results)
                known = _bounded_context(registry.known_entities(),windows)
                messages = repair_messages(messages, text, blocking[:20])
                messages[-1]['content'] = json.dumps({'task':'amend', 'issues':blocking[:20], 'known_entities':known,
                    'instruction':'返回含 entities/relations 两个数组的 JSON 对象，仅提交错误涉及的增量；优先引用这里已接受实体的 type/name，不生成 ID。'}, ensure_ascii=False)
                kind = 'repair'

        for observation in planned:
            if not process(observation):
                failed += 1
        if audit and planned and any(w['text'].strip() for o in planned for w in o['source_windows']):
            # Prioritize source observations with the fewest accepted fact refs.
            coverage = defaultdict(int)
            for fact in registry.entities + registry.relations:
                for ref in fact.get('source_refs', []):
                    coverage[ref.get('chunk_id')] += 1
            ordered = sorted(planned, key=lambda o: (sum(coverage[w['chunk_id']] for w in o['source_windows']), o['id']))
            selected, total = [], 0
            for observation in ordered:
                for window in observation['source_windows']:
                    if window.get('context_only'):
                        continue
                    if len(selected) < 12 and total + len(window['text']) <= window_chars * 2:
                        selected.append(window)
                        total += len(window['text'])
            document_diagnostics.append({'code':'AUDIT_COVERAGE', 'document_id':document_id, 'visited':len(selected), 'audit_not_visited':len(planned)-len(selected)})
            if selected and not process({'id':f'{document_id}:audit','document_id':document_id,'source_windows':selected}, task='amend', kind='audit'):
                failed += 1
        if semantic_review and len(registry.entities) >= max(2, review_min_entities):
            # A bounded review of named nodes; the controller alone applies merges.
            windows, size = [], 0
            for observation in planned:
                for window in observation['source_windows']:
                    if not window.get('context_only') and size + len(window['text']) <= 2 * window_chars:
                        windows.append(window)
                        size += len(window['text'])
            known = _review_context(registry.entities, planned, limit=max(2, review_max_entities))
            def review_messages():
                keys = {(entity['type'],entity['name']) for entity in known}
                links = [{field:relation[field] for field in RELATION_FIELDS} for relation in registry.relations
                         if (relation['from_type'],relation['from_name']) in keys
                         and (relation['to_type'],relation['to_name']) in keys][:300]
                return build_messages(compiled.prompt_contract, windows, known, task='review', accepted_relations=links)
            messages = review_messages()
            while len(known) > 2 and len(json.dumps(messages, ensure_ascii=False).encode()) > max_input_bytes:
                known.pop()
                messages = review_messages()
            observation = {'id':f'{document_id}:review','document_id':document_id}
            text, errors, _ = invoke(messages, 'review', observation)
            if text is not None and not errors:
                try:
                    payload = json.loads(text, parse_float=str)
                    if not isinstance(payload, dict) or not isinstance(payload.get('merges'), list) or payload.get('entities') or payload.get('relations'):
                        raise ValueError('review must contain merge proposals only')
                    allowed = {(entity['type'],entity['name']) for entity in known}
                    groups = []
                    for group in payload['merges']:
                        refs = ([group.get('canonical')] + group.get('members', [])) if isinstance(group,dict) and isinstance(group.get('members'),list) else []
                        if not refs or any(not isinstance(ref,dict) or (ref.get('type'),ref.get('name')) not in allowed for ref in refs):
                            document_diagnostics.append({'code':'UNKNOWN_REVIEW_MEMBER','observation_id':observation['id']})
                            continue
                        groups.append(group)
                    before = len(registry.entities)
                    document_diagnostics.extend(dict(error, observation_id=observation['id']) for error in registry.apply_merges(groups))
                    with budget_lock:
                        stats['merged_entities'] += before - len(registry.entities)
                    document_diagnostics.append({'code':'SEMANTIC_REVIEW','document_id':document_id,
                        'reviewed_entities':len(known),'not_reviewed_entities':before-len(known),
                        'merged_entities':before-len(registry.entities)})
                except (ValueError, TypeError):
                    errors = [{'code':'INVALID_REVIEW_JSON','path':'response','message':'expected bounded merge proposals'}]
            if errors:
                failed += 1
                document_diagnostics.extend(dict(error, observation_id=observation['id']) for error in errors)
        facts = registry.facts()
        return facts, failed, document_diagnostics
    items = list(documents.items())
    workers = max(1, int(max_workers or 1))
    if workers == 1 or len(items) <= 1:
        results = [run_document(item) for item in items]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(items)), thread_name_prefix="v3-doc") as pool:
            results = list(pool.map(run_document, items))
    for facts, document_failed, document_diagnostics in results:
        diagnostics.extend(document_diagnostics)
        entities.extend(facts['entities'])
        relations.extend(facts['relations'])
        failed += document_failed
    combined = display_property_names(coalesce_results([{'entities':entities,'relations':relations}]), compiled.schema)
    entities, relations = combined['entities'], combined['relations']
    stats.update(elapsed_seconds=round(time.monotonic()-started,3), failed_observations=failed,
                 accepted_entities=len(entities), accepted_relations=len(relations))
    status = 'success' if failed == 0 else ('incomplete' if entities or any(event.get('status') == 'received' for event in events) else 'failed')
    # Reached budget with an invalid response is incomplete even with an empty graph.
    if failed and any(d['code'] in {'INPUT_BUDGET_EXCEEDED','CALL_BUDGET_EXHAUSTED','TOKEN_BUDGET_EXHAUSTED','OUTPUT_TRUNCATED','INVALID_JSON','INVALID_COLLECTIONS','REPAIR_BUDGET_EXHAUSTED','NO_REPAIR_PROGRESS'} for d in diagnostics):
        status = 'incomplete'
    return {'status':status,'entities':entities,'relations':relations,'diagnostics':diagnostics,'stats':stats,'trace':events,
            'confidence_kind':'uncalibrated_support_score'}
