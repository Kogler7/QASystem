"""Record recovery, stage semantics and evidence boundaries agree end to end."""
from copy import deepcopy
import json

import pytest

from ..candidate_generation import generate_structured
from ..chunk_context import ExtractionChunk
from ..llm_client import StructuredLLMResponse
from ..ontology_compiler import compile_plan
from ..output_contracts import property_entity_schema, verification_schema, validate_verification_items
from ..prompt_router import build_chat_messages, resolve_document_route
from ..property_grounding import ground_properties, _verification_context
from ..semantic_evidence import build_verification_messages
from ..skill_planner import SkillAssemblyPlanner
from ..split_extractor import extract_split
from .stub_llm import StubLLM, span_item, source_documents

ONTOLOGY = '@startuml\nclass Person {\n + 职责 (duty): String\n}\n@enduml'


def generate(llm, messages, schema, **kwargs):
    attempts = []
    result = generate_structured(llm, messages, schema, use_scheduler=False, attempts=attempts,
        pass_name='test', chunk_id='c', max_attempts=2, retry_base_seconds=0, **kwargs)
    return result, attempts


def test_verification_retry_only_missing_ids_and_merges_prior_yes_no():
    items = [{'item_id': 'a'}, {'item_id': 'b'}, {'item_id': 'c'}]
    messages = build_verification_messages(chunk=ExtractionChunk('c', '原文'), items=items)
    original = deepcopy(messages)
    calls = []
    def llm(sent):
        user = json.loads(sent[1]['content'])
        calls.append(user)
        if len(calls) == 1:
            return '\n'.join(json.dumps({'item_id': key, 'supported': verdict, 'reason': 'source'})
                for key, verdict in [('a', 'yes'), ('b', 'no')]) + '\n{"item_id":"c"'
        assert user['task']['items'] == [{'item_id': 'c'}]
        assert user['output_contract']['success_schema']['properties']['item_id']['enum'] == ['c']
        return '{"item_id":"c","supported":"uncertain","reason":"not enough"}'
    result, attempts = generate(llm, messages, verification_schema(items),
        validator=lambda payload: validate_verification_items(payload, items))
    assert [(row['item_id'], row['supported']) for row in result['verdicts']] == [('a', 'yes'), ('b', 'no'), ('c', 'uncertain')]
    assert not any(attempt['terminal'] for attempt in attempts)
    assert messages == original
    assert calls[0]['documents'] == calls[1]['documents']


def test_property_retry_narrows_targets_and_retains_valid_proposals():
    plan = compile_plan(ONTOLOGY)
    chunk = ExtractionChunk('c', '甲负责巡检，乙负责运输。')
    known = [{'type': 'Person', 'name': name, 'properties': {}} for name in ['甲', '乙']]
    schema = property_entity_schema(known, plan)
    messages = build_chat_messages(task='property', plan=plan, route=resolve_document_route(plan, [chunk]),
        chunk=chunk, known_entities=known, output_schema=schema)
    seen = []
    def llm(sent):
        user = json.loads(sent[1]['content'])
        seen.append(user)
        if len(seen) == 1:
            return '{"type":"Person","name":"甲","properties":{}}\n{"type":'
        assert [entity['name'] for entity in user['task']['known_entities']] == ['乙']
        assert user['output_contract']['success_schema']['properties']['name']['enum'] == ['乙']
        return '{"type":"Person","name":"乙","properties":{}}'
    result, attempts = generate(llm, messages, schema)
    assert [row['name'] for row in result['entities']] == ['甲', '乙']
    assert not any(attempt['terminal'] for attempt in attempts)
    assert 'empty_result' not in seen[0]['output_contract']


@pytest.mark.parametrize('flagged', [False, True])
def test_delta_empty_preserves_prefix_but_cannot_hide_provider_truncation(flagged):
    plan = compile_plan(ONTOLOGY)
    chunk = ExtractionChunk('c', '甲负责巡检。')
    messages = build_chat_messages(task='entity', plan=plan, route=resolve_document_route(plan, [chunk]), chunk=chunk)
    row = {**span_item('Person', '甲', chunk.text), 'properties': {}}
    text = json.dumps(row) + '\n多余说明'
    responses = iter([StructuredLLMResponse(text=text, api='chat.completions', response_mode='plain',
        status='incomplete', finish_reason='length') if flagged else text, '{}'])
    sent = []
    def llm(message):
        sent.append(json.loads(message[1]['content']))
        return next(responses)
    result, attempts = generate(llm, messages, plan.output_schemas['entity_candidates'])
    assert result == {'entities': [row]}
    assert bool(attempts[-1]['terminal']) == flagged
    assert '重新输出本批次完整 JSONL' not in sent[1]['retry_feedback']


def test_no_change_proposal_keeps_verified_value_and_receipt():
    text = '甲负责巡检。'
    llm = StubLLM(entity_handler=lambda p, c: [{**span_item('Person', '甲', text),
        'properties': {'duty': '巡检'}}] if c['text'] == text else [],
        property_handler=lambda p, c: {'entities': [{'type': 'Person', 'name': '甲', 'properties': {}}]},
        verification_handler=lambda item, c: 'yes')
    # Keep one optional field unfilled so completion is still needed; fully
    # verified records now skip neighbour rescans altogether.
    ontology = ONTOLOGY.replace('职责 (duty): String', '职责 (duty): String\n + extra: String')
    result = extract_split(ontology, [ExtractionChunk('a', text), ExtractionChunk('b', '其他说明。')],
        llm=llm, skill_refs=[], max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    row = result['raw']['entities'][0]
    assert row['properties'] == {'duty': '巡检'}
    assert row['property_evidence']['duty']['sources'][0]['quote'] == text
    assert any(task == 'property' for task, _ in llm.calls)


def test_required_property_repair_uses_exact_key_and_delta_contract():
    text = '甲负责巡检。'
    seen = []
    def repair(prompt, chunk):
        user = json.loads(prompt)
        seen.append(user)
        assert user['task']['known_entities'][0]['name'] == '甲'
        assert user['task']['known_entities'][0]['requested_properties'] == ['duty']
        assert 'mention' not in user['output_contract']['success_schema']['properties']
        return {'entities': [{'type': 'Person', 'name': '甲', 'properties': {'duty': '巡检'},
                              'evidence': {'property_quotes': {'duty': [text]}}}]}
    ontology = ONTOLOGY.replace('@enduml', "' @required Person:duty\n@enduml")
    result = extract_split(ontology, [ExtractionChunk('c', text)], skill_refs=[],
        llm=StubLLM(entity_handler=lambda p, c: [span_item('Person', '甲', text)],
                    repair_handler=repair, verification_handler=lambda item, c: 'yes'), max_attempts=1)
    assert result['status'] == 'succeeded' and len(seen) == 1
    assert result['raw']['entities'][0]['properties'] == {'duty': '巡检'}


@pytest.mark.parametrize('ref', ['official_document@14', 'rule@10', 'trouble@10'])
def test_stage_operations_do_not_leak_into_discovery_or_review(ref):
    plan = compile_plan(ONTOLOGY)
    chunk = ExtractionChunk('c', '原文')
    route = resolve_document_route(plan, [chunk])
    planner = SkillAssemblyPlanner()
    planner.prepare(route.to_dict(), skill_refs=[ref])
    generation = build_chat_messages(task='entity', plan=plan, route=route, chunk=chunk, assembly_planner=planner)
    review = build_verification_messages(chunk=chunk, route=route, assembly_planner=planner,
                                         items=[{'item_id': 'a'}])
    for messages in [generation, review]:
        policy = json.loads(messages[1]['content'])['domain_policy']
        assert '仅补充已接受实体' not in policy
        assert 'missing property 输出更新' not in policy
        assert '仅处理给定实体的属性变更' not in policy
        assert '针对给定实体提出' not in policy
    if ref.startswith('trouble'):
        assert '观测数值或状态' in json.loads(review[1]['content'])['domain_policy']


def test_long_ontology_constraint_is_identical_in_generation_and_review():
    comment = '字段说明。' * 120 + '不包括计划或建议，只包含实际实施。'
    plan = compile_plan(ONTOLOGY.replace('String', 'String // ' + comment))
    chunk = ExtractionChunk('c', '甲负责巡检。')
    messages = build_chat_messages(task='entity', plan=plan, route=resolve_document_route(plan, [chunk]), chunk=chunk)
    llm = StubLLM(verification_handler=lambda item, source: 'yes')
    ground_properties([{'type': 'Person', 'name': '甲', 'properties': {'duty': '巡检'},
        'evidence': {'quote': chunk.text}}], [chunk], llm, attempts=[], warnings=[], plan=plan)
    review = json.loads(llm.calls[0][1])
    assert comment in json.loads(messages[1]['content'])['ontology']
    assert comment in review['ontology']
    assert "{'display_name'" not in review['ontology']
    assert '\n- type `Person`:' in review['ontology']


def test_discontinuous_documents_do_not_render_as_a_single_source():
    plan = compile_plan(ONTOLOGY)
    first = ExtractionChunk('private-a', '甲负责检查。', document_id='private-doc-a')
    second = ExtractionChunk('private-b', '乙负责运输。', document_id='private-doc-b')
    route = resolve_document_route(plan, [first])
    split = build_chat_messages(task='entity', plan=plan, route=route, chunk=first, source_chunks=[first, second])
    joined = build_chat_messages(task='entity', plan=plan, route=route,
        chunk=ExtractionChunk('single', first.text + '\n\n' + second.text))
    assert split != joined
    user = json.loads(split[1]['content'])
    assert '不同文档' in user['documents'] and 'private-' not in split[1]['content']
    assert [part['text'] for part in source_documents(user)] == [first.text, second.text]


def test_review_preserves_original_source_order_instead_of_id_sorting():
    sources = [ExtractionChunk('2', '甲负责检查。'), ExtractionChunk('10', '乙负责运输。')]
    spans = [{'source_block_ids': [source.chunk_id], 'start_char': 0, 'end_char': len(source.text),
              'span_id': f'{source.chunk_id}:0:{len(source.text)}', 'quote': source.text} for source in sources]
    batch = [({'type': 'Person', 'name': name}, 'duty', '', [span], {})
             for name, span in zip(['甲', '乙'], spans)]
    assert [source.chunk_id for source in _verification_context(batch, sources)] == ['2', '10']


def test_ambiguous_short_property_quote_is_not_bound_to_first_subject():
    source = ExtractionChunk('c', '甲负责检查。乙不负责检查。')
    row = {'type': 'Person', 'name': '乙', 'properties': {'duty': '检查'},
           'evidence': {'quote': source.text, 'property_quotes': {'duty': ['检查']}}}
    llm = StubLLM(verification_handler=lambda item, source: 'yes')
    grounded = ground_properties([row], [source], llm, plan=compile_plan(ONTOLOGY), attempts=[], warnings=[])
    assert grounded[0]['properties'] == {} and not llm.calls


def test_candidate_admission_can_use_bounded_subject_context():
    first = ExtractionChunk('a', '甲部门开展检查。', document_id='d', section_id='s')
    second = ExtractionChunk('b', '该部门负责巡检。', document_id='d', section_id='s')
    llm = StubLLM(entity_handler=lambda p, c: [], verification_handler=lambda item, source: 'yes')
    def adapter(messages):
        user = json.loads(messages[1]['content'])
        if user['task']['operation'] == 'entity':
            if source_documents(user)[0]['text'] == first.text:
                return '{}'
            assert first.text in user['documents'] and '仅辅助上下文' in user['documents']
            return json.dumps({**span_item('Person', '甲部门', second.text), 'properties': {'duty': '巡检'}})
        return llm(messages)
    result = extract_split(ONTOLOGY, [first, second], llm=adapter, skill_refs=[], max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert result['raw']['entities'][0]['name'] == '甲部门'
    assert result['raw']['entities'][0]['entity_evidence']['support'] == 'llm'


def test_conflict_review_allows_explicit_replacement_without_inventing_union():
    messages = build_verification_messages(chunk=ExtractionChunk('c', '2025年起由乙替代甲负责检查。'),
        items=[{'item_id': 'a', 'type': 'Person', 'name': '负责人', 'property': 'name',
                'value': '乙', 'previous_value': '甲', 'conflicting_values': ['甲', '乙']}])
    assert '原文明示新值替代旧值时允许采用新值' in messages[0]['content']
    assert '只选其中一个值' not in messages[0]['content']


def test_no_change_after_verified_conflict_resolution_keeps_the_receipt(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_WINDOW_CHARS', '128')
    quotes = ['甲负责巡检。', '甲负责审批。']
    calls = []
    def entity(prompt, chunk):
        for quote, value in zip(quotes, ['巡检', '审批']):
            if quote in chunk['text']:
                return [{**span_item('Person', '甲', quote), 'properties': {'duty': value}}]
        return []
    def proposal(prompt, chunk):
        calls.append(prompt)
        if len(calls) > 1:
            return {'entities': [{'type': 'Person', 'name': '甲', 'properties': {}}]}
        return {'entities': [{'type': 'Person', 'name': '甲', 'properties': {'duty': '巡检并审批'},
            'evidence': {'property_quotes': {'duty': quotes}}}]}
    result = extract_split(ONTOLOGY, [ExtractionChunk(str(i), quote + '背景。' * 150)
        for i, quote in enumerate(quotes)], skill_refs=[], max_attempts=1, max_repair_rounds=0,
        llm=StubLLM(entity_handler=entity, property_handler=proposal, verification_handler=lambda item, source: 'yes'))
    assert len(calls) > 1
    assert result['status'] == 'succeeded'
    assert result['raw']['entities'][0]['properties'] == {'duty': '巡检并审批'}
    assert result['raw']['entities'][0]['property_evidence']['duty']['value'] == '巡检并审批'


def test_candidate_context_honors_explicit_neighbor_limits():
    chunks = [ExtractionChunk('a', '甲负责巡检。'), ExtractionChunk('b', '乙负责运输。')]
    seen = []
    def discover(prompt, chunk):
        seen.append(json.loads(prompt)['documents'])
        return []
    result = extract_split(ONTOLOGY, chunks, llm=StubLLM(entity_handler=discover), skill_refs=[],
        local_previous=0, local_next=0, max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert seen == [chunk.text for chunk in chunks]


def test_overflow_splits_keep_accepted_entity_and_resume_only_missing_candidates():
    text = '甲负责巡检。' + '背景。' * 130 + '乙负责运输。'
    calls = []
    def llm(messages):
        user = json.loads(messages[1]['content'])
        calls.append(user)
        if len(calls) == 1:
            prefix = json.dumps({**span_item('Person', '甲', '甲负责巡检。'), 'properties': {}})
            return StructuredLLMResponse(text=prefix + '\n{"type":', api='chat.completions',
                response_mode='plain', status='incomplete', finish_reason='length')
        if user['task']['operation'] == 'evidence_verification':
            return StubLLM()(messages)
        if user['task']['operation'] == 'property':
            return '\n'.join(json.dumps({'type': row['type'], 'name': row['name'], 'properties': {}})
                for row in user['task']['known_entities'])
        assert user['task']['operation'] == 'entity'
        assert user['task']['saved_entity_keys'] == [{'type': 'Person', 'name': '甲'}]
        primary = source_documents(user)[0]['text']
        return json.dumps({**span_item('Person', '乙', '乙负责运输。'), 'properties': {}}) if '乙负责运输。' in primary else '{}'
    result = extract_split(ONTOLOGY, [ExtractionChunk('c', text)], llm=llm, skill_refs=[],
        max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert [(row['type'], row['name']) for row in result['raw']['entities']] == [('Person', '甲'), ('Person', '乙')]
    assert any(attempt.get('recovery') == 'split_request' for attempt in result['attempts'])


def test_repeating_only_a_truncated_prefix_cannot_claim_discovery_complete():
    plan = compile_plan(ONTOLOGY)
    chunk = ExtractionChunk('c', '甲负责巡检。乙负责运输。')
    messages = build_chat_messages(task='entity', plan=plan, route=resolve_document_route(plan, [chunk]), chunk=chunk)
    row = {**span_item('Person', '甲', '甲负责巡检。'), 'properties': {}}
    responses = iter([StructuredLLMResponse(text=json.dumps(row), api='chat.completions', response_mode='plain',
        status='incomplete', finish_reason='length'), json.dumps(row)])
    result, attempts = generate(lambda messages: next(responses), messages, plan.output_schemas['entity_candidates'])
    assert result == {'entities': [row]} and attempts[-1]['terminal']


def test_valid_prefix_does_not_hide_a_complete_but_ungrounded_candidate():
    from ..output_contracts import validate_candidate_properties
    plan = compile_plan(ONTOLOGY)
    chunk = ExtractionChunk('c', '甲负责巡检。')
    good = {**span_item('Person', '甲', chunk.text), 'properties': {}}
    bad = {**span_item('Person', '火星人', '火星人驾驶飞船。'), 'properties': {}}
    messages = build_chat_messages(task='entity', plan=plan, route=resolve_document_route(plan, [chunk]), chunk=chunk)
    attempts = []
    result = generate_structured(lambda m: json.dumps(good) + '\n' + json.dumps(bad), messages,
        plan.output_schemas['entity_candidates'], attempts=attempts, use_scheduler=False, pass_name='entity',
        chunk_id='c', max_attempts=1, retry_base_seconds=0,
        validator=lambda payload: validate_candidate_properties(payload, plan, chunk=chunk))
    assert result == {'entities': [good]}
    assert attempts[-1]['terminal'] and attempts[-1]['rejected_records'] == 1


def test_recovered_prefix_still_scans_later_properties_of_the_same_entity():
    old, new = '甲负责巡检。', '甲负责审批。'
    text = old + '背景。' * 130 + new
    first = True
    stub = StubLLM(verification_handler=lambda item, source: 'yes')
    def llm(messages):
        nonlocal first
        user = json.loads(messages[1]['content'])
        if user['task']['operation'] == 'entity':
            if first:
                first = False
                row = {**span_item('Person', '甲', old), 'properties': {'duty': '巡检'}}
                return StructuredLLMResponse(text=json.dumps(row) + '\n{"type":', api='chat.completions',
                    response_mode='plain', status='incomplete', finish_reason='length')
            return '{}'
        if user['task']['operation'] == 'property':
            if new in user['documents']:
                return json.dumps({'type': 'Person', 'name': '甲', 'properties': {'duty': '巡检并审批'},
                    'evidence': {'property_quotes': {'duty': [old, new]}}})
            return '{"type":"Person","name":"甲","properties":{}}'
        return stub(messages)
    result = extract_split(ONTOLOGY, [ExtractionChunk('c', text)], llm=llm, skill_refs=[],
        max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert result['raw']['entities'][0]['properties'] == {'duty': '巡检并审批'}
    assert {span['quote'] for span in result['raw']['entities'][0]['property_evidence']['duty']['sources']} == {old, new}
