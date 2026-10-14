"""Property support is independent of entity-name/source binding."""
from __future__ import annotations
from docker.configuration import getenv as config_getenv

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import re
import unicodedata

from .chunk_context import ChunkContextManager, ExtractionChunk
from .evidence import locate_quote
from .graph_utils import append_warning
from .request_budget import fits, document_expired, split_request_failure, recover_split_attempts
from .task_runtime import synchronous, invoke, ordered_tasks, current_runtime
from .semantic_evidence import build_verification_messages
from .output_contracts import verification_schema, validate_verification_items


def locate_in_sources(quote, sources):
    for source in sources:
        found = locate_quote(ChunkContextManager([source]), quote)
        if found is not None:
            payload = found.to_dict()
            offset = int(source.metadata.get("_v31_window_start", 0))
            payload["start_char"] += offset
            payload["end_char"] += offset
            payload["span_id"] = f"{source.chunk_id}:{payload['start_char']}:{payload['end_char']}"
            return payload
    return None


def locate_property_quote(quote, sources):
    """Do not bind an ambiguous short quote to the first unrelated occurrence."""
    matches, contexts = {}, set()
    for source in sources:
        cursor = 0
        while cursor < len(source.text):
            found = ChunkContextManager([replace(source, text=source.text[cursor:])]).find_span(source.chunk_id, quote)
            if found is None:
                break
            left, right = cursor + found[0], cursor + found[1]
            fragment = replace(source, text=source.text[left:right], metadata={**source.metadata,
                '_v31_window_start': int(source.metadata.get('_v31_window_start', 0)) + left})
            span = locate_in_sources(fragment.text, [fragment])
            matches[span['span_id']] = span
            context_left = max(source.text.rfind(mark, 0, left) for mark in '\n。；') + 1
            ends = [source.text.find(mark, right) for mark in '\n。；']
            context_right = min((end + 1 for end in ends if end >= 0), default=len(source.text))
            base = int(source.metadata.get('_v31_window_start', 0))
            row_ranges = [(a - base, b - base) for a, b in source.metadata.get('_v31_table_rows', [])
                          if a - base <= left and right <= b - base
                          and 0 <= a - base < b - base <= len(source.text)]
            if row_ranges:
                context_left, context_right = row_ranges[0]
            contexts.add((source.document_id, source.text[context_left:context_right]))
            cursor = right
    # COSEC: A literal occurrence cannot choose between different supporting
    # subjects/conditions. The model must supply a disambiguating longer quote.
    return list(matches.values()) if len(matches) == 1 or len(contexts) == 1 else []


def merge_evidences(*collections):
    unique = {}
    for collection in collections:
        for evidence in collection:
            if evidence and evidence.get("span_id"):
                unique[evidence["span_id"]] = evidence
    return list(unique.values())


def _identifier_literal_state(literal, sources):
    """Presence and token completeness are separate from semantic ownership."""
    seen = False
    if not literal:
        return False, False
    def word(char):
        return bool(re.fullmatch(r'[A-Za-z0-9_]', unicodedata.normalize('NFKC', char)))
    for source in sources:
        cursor = 0
        while cursor < len(source.text):
            part = replace(source, text=source.text[cursor:])
            found = ChunkContextManager([part]).find_span(source.chunk_id, literal)
            if found is None:
                break
            left, right = cursor + found[0], cursor + found[1]
            seen = True
            prefix = left > 0 and word(source.text[left - 1]) and word(source.text[left])
            suffix = right < len(source.text) and word(source.text[right - 1]) and word(source.text[right])
            if not prefix and not suffix:
                return True, True
            cursor = right
    return seen, False


def bind_to_sources(rows, sources):
    for row in rows:
        evidence = row.get("evidence") or {}
        located = locate_in_sources(evidence.get("quote", ""), sources)
        if located is None:
            located = locate_in_sources(row.get("name", ""), sources)
        if located is not None:
            row["evidence"] = located
        row["evidences"] = merge_evidences(row.get("evidences", []), [located],
            [span for receipt in row.get("property_evidence", {}).values() for span in receipt["sources"]])
    return rows


def _verification_context(batch, sources, prior=None, priors=None):
    """Keep source coordinates and surrounding conditions, not entire documents.

    A quote locates evidence; it is not proof of subject/property entailment.
    Neighbouring text remains available to resolve pronouns and conditions.
    Oversized individual evidence is rejected by the common request budget.
    """
    excerpts = {(s.chunk_id, s.metadata.get('_v31_window_start', 0),
                 s.metadata.get('_v31_window_start', 0) + len(s.text)): s
                for s in sources if s.metadata.get('_v31_table_header')}
    for row, prop, _, spans, _ in batch:
        # COSEC: Task operands are not evidence. Carry the actual subject and
        # previous-value source passages, rather than asking the judge to infer them.
        anchors = []
        # The located candidate passage establishes the subject. Searching the
        # first occurrence of a generic name in every source imports other rows.
        subject = row.get('evidence') or {}
        if subject.get('span_id'):
            anchors.append(subject)
        elif subject.get('quote'):
            anchors.extend(locate_property_quote(subject['quote'], sources))
        row_prior = (priors or {}).get((row['type'], row['name']), prior) or {}
        if (row_prior.get('evidence') or {}).get('span_id'):
            anchors.append(row_prior['evidence'])
        anchors.extend((row_prior.get('entity_evidence') or {}).get('sources', []))
        old = (row_prior.get('property_evidence', {}).get(prop) or {}).get('sources', [])
        for span in merge_evidences(spans, anchors, old):
            cid = span['source_block_ids'][0]
            for source in sources:
                base = int(source.metadata.get('_v31_window_start', 0))
                start, end = span['start_char'] - base, span['end_char'] - base
                if source.chunk_id != cid or not (0 <= start < end <= len(source.text)):
                    continue
                left, right = max(0, start - 200), min(len(source.text), end + 200)
                # Table fields jointly identify the subject, condition, cause
                # and result. A +/-200 slice can remove a distant column and
                # turn a diagnostic code into an apparently causal statement.
                # Carry only the containing original row(s), never a neighbour.
                row_ranges = [(a - base, b - base) for a, b in source.metadata.get('_v31_table_rows', [])
                              if a - base < end and b - base > start
                              and 0 <= a - base < b - base <= len(source.text)]
                if row_ranges:
                    left, right = min(a for a, _ in row_ranges), max(b for _, b in row_ranges)
                excerpts[(cid, base + left, base + right)] = replace(source,
                    text=source.text[left:right], metadata={**source.metadata, "_v31_window_start": base + left})
                break
    # Merge only when one supplied source contains the entire union. Never
    # manufacture intervening text or cross original chunk coordinates.
    merged = []
    positions = {cid: index for index, cid in enumerate(dict.fromkeys(s.chunk_id for s in sources))}
    for part in sorted(excerpts.values(), key=lambda s: (positions[s.chunk_id], s.metadata.get('_v31_window_start', 0))):
        start = int(part.metadata.get('_v31_window_start', 0))
        if merged and not part.metadata.get('_v31_table_header'):
            previous = merged[-1]
            left = int(previous.metadata.get('_v31_window_start', 0))
            right = max(left + len(previous.text), start + len(part.text))
            if (previous.chunk_id == part.chunk_id and not previous.metadata.get('_v31_table_header')
                    and start <= left + len(previous.text)):
                source = next((s for s in sources if s.chunk_id == part.chunk_id
                    and int(s.metadata.get('_v31_window_start', 0)) <= left
                    and int(s.metadata.get('_v31_window_start', 0)) + len(s.text) >= right), None)
                if source is not None:
                    base = int(source.metadata.get('_v31_window_start', 0))
                    text = source.text[left-base:right-base]
                    if text[:len(previous.text)] == previous.text and text[start-left:start-left+len(part.text)] == part.text:
                        merged[-1] = replace(source, text=text,
                            metadata={**source.metadata, '_v31_window_start': left})
                        continue
        merged.append(part)
    return merged


async def _review_batches(pending, context_for, llm, *, attempts, warnings, call_metrics,
                    pass_name, route, assembly_planner, use_scheduler, max_attempts,
                    retry_base_seconds, max_workers, plan=None):
    """Read-only batch workers; apply verdicts/receipts later on the caller thread."""
    from .candidate_generation import generate_structured, _append_call
    limit = max(1, min(24, int(config_getenv('KG_EXTRACT_V31_VERIFICATION_BATCH_SIZE', '16'))))
    if pass_name == 'entity_verification':
        # Entity admission can be reviewed separately without multiplying all
        # attribute calls. Both paths retain the same scheduler and hard budget.
        limit = min(limit, max(1, int(config_getenv('KG_EXTRACT_V31_ENTITY_VERIFICATION_BATCH_SIZE', str(limit)))))
    output_cap = getattr(llm, 'max_tokens', None)
    if isinstance(output_cap, (int, float)) and output_cap > 0:
        limit = min(limit, max(1, int(output_cap) // 128))

    def request(batch):
        context = context_for(batch)
        if not context:
            return None
        items = [entry[-1] for entry in batch]
        return build_verification_messages(chunk=context[0], source_chunks=context,
            items=items, route=route, assembly_planner=assembly_planner, plan=plan,
            item_sources={entry[-1]['item_id']: context_for([entry]) for entry in batch})

    def partition(batch):
        if not batch:
            return
        messages = request(batch)
        if len(batch) > limit:
            for start in range(0, len(batch), limit):
                yield from partition(batch[start:start + limit])
        elif len(batch) > 1 and messages is not None and not fits(messages, llm):
            middle = len(batch) // 2
            yield from partition(batch[:middle])
            yield from partition(batch[middle:])
        else:
            yield batch

    async def review(batch):
        local_attempts, local_metrics = [], []
        async def execute(current, *, force_review=False, diagnostics=None):
            recorded_attempts, recorded_metrics = diagnostics or (local_attempts, local_metrics)
            messages = request(current)
            if messages is None:
                return {}
            items = [entry[-1] for entry in current]
            # COSEC: The exact source authority and full policy participate in
            # this request-local cache. Equal names/values alone cannot reuse proof.
            user = json.loads(messages[1]['content'])
            def cache_key(item):
                operand = {key: value for key, value in item.items() if key != 'item_id'}
                item_context = context_for([entry for entry in current if entry[-1]['item_id'] == item['item_id']])
                item_authority = [(s.chunk_id, s.document_id, s.metadata.get('_v31_window_start', 0), s.text) for s in item_context]
                return hashlib.sha256(json.dumps([operand, item_authority, messages[0]['content'], user['ontology'], user['domain_policy']],
                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            cache = current_runtime().review_cache
            cached = ({} if force_review else
                      {item['item_id']: cache[cache_key(item)] for item in items if cache_key(item) in cache})
            if cached:
                current_runtime().metrics['reused_verdicts'] += len(cached)
                remaining = [entry for entry in current if entry[-1]['item_id'] not in cached]
                if remaining:
                    cached.update(await execute(remaining, diagnostics=diagnostics))
                return cached
            before = len(recorded_attempts)
            parsed = await invoke(generate_structured, llm, messages, verification_schema(items),
                use_scheduler=use_scheduler, attempts=recorded_attempts, pass_name=pass_name,
                chunk_id=next((span['source_block_ids'][0] for entry in current for span in entry[3]), 'verification'), max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds, call_metrics=recorded_metrics,
                split_on_overflow=len(current) > 1,
                validator=lambda payload: validate_verification_items(payload, items))
            verdicts = {v['item_id']: v['supported'] for v in (parsed or {}).get('verdicts', [])}
            for item in items:
                verdict = verdicts.get(item['item_id'])
                if verdict in {'yes', 'no'}:
                    if len(cache) >= 512:
                        cache.pop(next(iter(cache)))
                    cache[cache_key(item)] = verdict
            missing = [entry for entry in current if entry[-1]['item_id'] not in verdicts]
            failures = recorded_attempts[before:]
            malformed = any(a.get('terminal') and a.get('type') in {'json', 'schema', 'incomplete'} for a in failures)
            if len(current) > 1 and malformed and not document_expired(llm):
                # Complete singleton responses can establish the known ID scope
                # when the provider repeatedly concatenates verdict fields. Do
                # not extract values from malformed JSON or reuse its cached
                # decisions as proof of a newly completed transport task.
                scope = missing or current
                recover_split_attempts(failures)
                async def singleton(entry):
                    child_attempts, child_metrics = [], []
                    result = await execute([entry], force_review=True,
                                           diagnostics=(child_attempts, child_metrics))
                    return result, child_attempts, child_metrics
                children = await ordered_tasks(singleton, scope, limit=max_workers)
                for result, child_attempts, child_metrics in children:
                    verdicts.update(result)
                    recorded_attempts.extend(child_attempts)
                    recorded_metrics.extend(child_metrics)
            elif (len(current) > 1 and missing and split_request_failure(failures)
                    and not document_expired(llm)):
                recover_split_attempts(failures)
                if len(missing) == 1:
                    verdicts.update(await execute(missing, diagnostics=diagnostics))
                else:
                    middle = len(missing) // 2
                    verdicts.update(await execute(missing[:middle], diagnostics=diagnostics))
                    verdicts.update(await execute(missing[middle:], diagnostics=diagnostics))
            return verdicts
        return batch, await execute(batch), local_attempts, local_metrics

    results = []
    for batch, verdicts, local_attempts, local_metrics in await ordered_tasks(
            review, partition(pending), limit=max_workers):
        attempts.extend(local_attempts)
        if call_metrics is not None:
            for call in local_metrics:
                _append_call(call_metrics, call)
        results.extend((entry, verdicts.get(entry[-1]['item_id'])) for entry in batch)
    return results


@synchronous
async def admit_entity_names(rows, sources, llm, *, attempts, warnings, use_scheduler=False,
                       max_attempts=3, retry_base_seconds=0.05, call_metrics=None,
                       plan=None, route=None, assembly_planner=None, max_workers=1, force_review=False):
    """Require affirmative semantic support for names that are not literal mentions."""
    from .candidate_generation import generate_structured
    admitted, pending = [], []
    # Official-document policy distinguishes duties from mere routing/signatures
    # and document titles from register fields. A literal substring alone cannot
    # establish these entity/type boundaries. Generic literal names retain their
    # existing fast path; nonliteral names always require independent support.
    review_literal = (assembly_planner.has_selected_skills if assembly_planner is not None else
                      getattr(route, 'kind', None) in {'official_document', 'rule', 'trouble'})
    for index, row in enumerate(rows):
        literal = locate_in_sources(row['name'], sources)
        primary_literal = locate_in_sources(row['name'], [source for source in sources
                                                       if not source.metadata.get('_v31_context_only')])
        if primary_literal and not review_literal and not force_review:
            admitted.append(row)
            continue
        span = locate_in_sources(row.get('evidence', {}).get('quote', ''), sources) or literal
        item = {'item_id': 'entity' if len(rows) == 1 else f'entity:{index}',
                'type': row['type'], 'name': row['name'],
                'candidate_properties': deepcopy(row.get('properties') or {}),
                'evidence_quotes': [span['quote']] if span else [],
                'entity_description': _entity_description(plan, row['type'])}
        spans = merge_evidences([span] if span else [], [literal] if literal else [])
        item['evidence_quotes'] = [source['quote'] for source in spans]
        pending.append((row, '', None, spans, item))

    accepted_ids = {id(row) for row in admitted}
    if pending:
        results = await _review_batches(pending, lambda batch: _verification_context(batch, sources), llm,
            attempts=attempts, warnings=warnings, call_metrics=call_metrics, pass_name='entity_verification',
            route=route, assembly_planner=assembly_planner, use_scheduler=use_scheduler,
            max_attempts=max_attempts, retry_base_seconds=retry_base_seconds, max_workers=max_workers, plan=plan)
        for (row, _, _, spans, item), verdict in results:
            if spans and verdict == 'yes':
                row['entity_evidence'] = {'name': row['name'], 'type': row['type'],
                                          'support': 'llm', 'sources': spans}
                accepted_ids.add(id(row))
            else:
                append_warning(warnings, 'ENTITY_SEMANTIC_UNSUPPORTED',
                    '实体名称与类型未获原文支持，已拒收', f"{row['type']}::{row['name']}")
                attempts.append({'attempt': 1, 'type': 'candidate_rejected' if verdict == 'no' else 'schema_validation', 'pass': 'entity_verification',
                    'chunk': sources[0].chunk_id, 'message': 'entity name/type not supported by source',
                    'terminal': verdict != 'no'})
    return [row for row in rows if id(row) in accepted_ids]


@synchronous
async def ground_properties(rows, sources, llm, *, attempts, warnings, use_scheduler=False,
                      max_attempts=3, retry_base_seconds=0.05, call_metrics=None, prior=None, plan=None,
                      route=None, assembly_planner=None, conflicts=None, priors=None, conflicts_by_key=None,
                      max_workers=1):
    """Remove unsupported values; accept semantic values only after explicit yes.

    Receipts are created here, never supplied by the extraction model. A forged
    quote cannot be repaired by falling back to the entity's name.
    """
    from .candidate_generation import generate_structured
    pending = []
    for index, row in enumerate(rows):
        row_prior = (priors or {}).get((row["type"], row["name"]), prior) or {}
        row_conflicts = (conflicts_by_key or {}).get((row["type"], row["name"]), conflicts) or {}
        row["property_evidence"] = {}
        specs = {p.identifier: p for p in plan.class_specs[row["type"]].properties} if plan else {}
        evidence = row.get("evidence") or {}
        property_quotes = evidence.get("property_quotes") or {}
        for prop, value in list((row.get("properties") or {}).items()):
            if value in (None, "", []):
                row["properties"].pop(prop)
                continue
            quotes = property_quotes.get(prop) or [evidence.get("quote", "")]
            # A caller-defined system-managed identifier is an existing literal
            # value, never an authorization for the extraction model to mint IDs.
            spec = specs.get(prop)
            comment = spec.comment if spec else ''
            system_managed = bool(re.search(r'由(?:结构化)?系统(?:生成|维护)|system[- ](?:generated|managed)', comment, re.I))
            declared_identity = bool(plan and prop in plan.class_specs[row['type']].identity_properties
                                     and not (spec and spec.context_inferable))
            # Definitions supplied by the caller can describe an ordinary code
            # field without declaring it the entity's identity. Do not accept
            # an alphanumeric prefix as that field's complete literal value.
            code_field = bool(spec and not spec.context_inferable and re.search(
                r'标识|编号|编码|代码|文号|\b(?:identifier|code)\b',
                f'{spec.display_name} {comment}', re.I))
            identifier_literal = str(value) if type(value) in (str, int, float) else ''
            present, complete = (_identifier_literal_state(identifier_literal, sources)
                                 if system_managed or declared_identity or code_field else (False, False))
            if ((system_managed or declared_identity) and not complete) or (code_field and present and not complete):
                row['properties'].pop(prop)
                append_warning(warnings, 'PROPERTY_IDENTIFIER_UNGROUNDED',
                    '身份或编码值缺少完整原文支持，禁止拼造或截取标识片段', f"{row['type']}::{row['name']}.{prop}")
                continue
            located_quotes = [locate_property_quote(q, sources) for q in quotes]
            supported = bool(located_quotes) and all(located_quotes)
            spans = merge_evidences([span for matches in located_quotes for span in matches])
            meaningful = any(span["quote"].strip() != row["name"] or len(span["quote"]) > 32
                             or value == row["name"] for span in spans)
            previous = row_prior.get("properties", {}).get(prop)
            changed = previous is not None and previous != value
            # Only a receipt produced earlier in this run can avoid another
            # semantic call. Textual cooccurrence says nothing about the field.
            receipt = (row_prior.get('property_evidence') or {}).get(prop) or {}
            if (prop not in row_conflicts and previous == value and receipt.get('value') == value
                    and receipt.get('support') == 'llm' and receipt.get('sources')):
                row['property_evidence'][prop] = deepcopy(receipt)
                continue
            if supported and meaningful:
                pending.append((row, prop, value, spans, {
                    "item_id": f"{index}:{prop}", "kind": "property", "type": row["type"],
                    "name": row["name"], "property": prop, "value": value,
                    # COSEC: Sibling values are untrusted operands, not proof.
                    # Field meaning can depend on direction, units or stage in
                    # other fields; checking each word alone loses that claim.
                    "candidate_properties": deepcopy({**row_prior.get('properties', {}),
                                                       **row.get('properties', {})}),
                    "property_description": ({"display_name": specs[prop].display_name, "data_type": specs[prop].data_type,
                        "comment": specs[prop].comment or "", **{key: getattr(specs[prop], key) for key in
                        ('required', 'multi_value', 'context_inferable') if getattr(specs[prop], key) is not None}} if prop in specs else {}),
                    "entity_description": _entity_description(plan, row['type']),
                    "evidence_quotes": [s["quote"] for s in spans],
                    "source_refs": [{"chunk_id": s["source_block_ids"][0],
                                     "start_char": s["start_char"], "end_char": s["end_char"]} for s in spans],
                    **({"previous_value": previous} if changed else {}),
                    **({'conflicting_values': row_conflicts[prop]} if prop in row_conflicts else {}),
                }))
            else:
                row["properties"].pop(prop)
                append_warning(warnings, "PROPERTY_EVIDENCE_UNSUPPORTED",
                    "属性引文缺失、无法定位或仅为实体名，已移除该属性", f"{row['type']}::{row['name']}.{prop}")

    def context_for(batch):
        context = _verification_context(batch, sources, prior, priors)
        if any(item[-1].get('conflicting_values') for item in batch):
            # All conflicting passages must be visible to the judge, even when
            # the proposed value cites only one of them. Budget checks remain strict.
            context = list(sources)
        return context
    if pending:
        results = await _review_batches(pending, context_for, llm, attempts=attempts, warnings=warnings,
            call_metrics=call_metrics, pass_name='property_verification', route=route,
            assembly_planner=assembly_planner, use_scheduler=use_scheduler, max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds, max_workers=max_workers, plan=plan)
        for (row, prop, value, spans, item), verdict in results:
            row_prior = (priors or {}).get((row["type"], row["name"]), prior) or {}
            if verdict == "yes":
                row["property_evidence"][prop] = {"value": deepcopy(value), "support": "llm", "sources": merge_evidences(spans,
                    ((row_prior.get("property_evidence", {}).get(prop) or {}).get("sources", [])
                     if row_prior.get("properties", {}).get(prop) == value else []))}
            else:
                row["properties"].pop(prop, None)
                append_warning(warnings, "PROPERTY_EVIDENCE_UNSUPPORTED",
                    "属性语义核验未确认支持，已移除该属性", f"{row['type']}::{row['name']}.{prop}")
    return bind_to_sources(rows, sources)


def _entity_description(plan, type_id):
    if plan is None:
        return type_id
    spec = plan.class_specs[type_id]
    return spec.display_name + ('\n' + spec.description if spec.description else '')
