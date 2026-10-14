"""Regressions from the v16 live traces; facts and completeness stay separate."""
import json

import pytest

from ..candidate_generation import generate_structured
from ..chunk_context import ExtractionChunk
from ..llm_client import StructuredLLMResponse
from ..ontology_compiler import compile_plan
from ..output_contracts import property_entity_schema, verification_schema
from ..split_extractor import extract_split
from .stub_llm import StubLLM, span_item, source_documents

ONTOLOGY = '@startuml\nclass Person {\n + duty: String\n}\n@enduml'
ROW = {'type': 'Person', 'mention': '甲', 'properties': {}, 'evidence': {'quote': '甲'}}


def generate(responses, schema=None, **kwargs):
    attempts, calls = [], []
    sequence = iter(responses)
    result = generate_structured(lambda m: next(sequence),
        [{'role': 'system', 'content': 'JSONL'}, {'role': 'user', 'content': '{"task":{"operation":"entity"}}'}],
        schema or compile_plan(ONTOLOGY).output_schemas['entity_candidates'],
        use_scheduler=False, attempts=attempts, call_metrics=calls, pass_name='entity',
        chunk_id='c', max_attempts=len(responses), retry_base_seconds=0, **kwargs)
    return result, attempts, calls


@pytest.mark.parametrize('fence', ['json', 'jsonl', ''])
def test_complete_fence_is_normalized_then_fully_validated(fence):
    result, attempts, calls = generate([f'```{fence}\n{json.dumps(ROW)}\n```'])
    assert result == {'entities': [ROW]} and not attempts
    assert calls[-1]['response_normalizations'] == ['complete_markdown_fence']


def test_property_fence_with_complete_exact_keys_does_not_create_terminal_failure():
    row = {'type': 'Person', 'name': '甲', 'properties': {}}
    result, attempts, _ = generate(['```jsonl\n' + json.dumps(row) + '\n```'],
        property_entity_schema([row], compile_plan(ONTOLOGY)))
    assert result == {'entities': [row]} and not attempts


def test_fenced_verdict_still_checks_every_expected_id():
    row = {'item_id': 'a', 'supported': 'yes', 'reason': 'source'}
    result, attempts, _ = generate(['```jsonl\n' + json.dumps(row) + '\n```'],
        verification_schema([{'item_id': 'a'}, {'item_id': 'b'}]))
    assert result == {'verdicts': [row]}
    assert attempts[-1]['terminal'] and attempts[-1]['missing_item_ids'] == ['b']


def test_final_empty_marker_is_not_a_bad_entity_record():
    result, attempts, calls = generate([json.dumps(ROW) + '\n{}'])
    assert result == {'entities': [ROW]} and not attempts
    assert calls[-1]['response_normalizations'] == ['final_empty_marker']


@pytest.mark.parametrize('text', ['{}\n' + json.dumps(ROW), json.dumps(ROW) + '\n{"type":\n{}',
    '解释\n```jsonl\n' + json.dumps(ROW) + '\n```', '```jsonl\n' + json.dumps(ROW),
    '{"entities":[' + json.dumps(ROW) + ']}',
    '{"type":"Person","mention":"甲","mention":"乙","evidence":{"quote":"甲"}}'])
def test_normalization_never_repairs_or_discards_bad_content(text):
    _, attempts, _ = generate([text])
    assert attempts[-1]['terminal']


def test_provider_truncation_cannot_be_hidden_by_complete_fence_or_marker():
    response = StructuredLLMResponse(text='```jsonl\n' + json.dumps(ROW) + '\n{}\n```',
        api='chat.completions', response_mode='plain', status='incomplete', finish_reason='length')
    result, attempts, _ = generate([response])
    assert result == {'entities': [ROW]} and attempts[-1]['terminal']
    assert attempts[-1]['type'] == 'incomplete'


def test_repeated_recovered_entity_does_not_become_a_schema_failure():
    result, attempts, _ = generate([json.dumps(ROW) + '\n{"type":"Invented"}', json.dumps(ROW) + '\n{}'])
    assert result == {'entities': [ROW]}
    assert len(attempts) == 1 and not attempts[0]['terminal']


def test_parent_saved_key_is_ignored_without_overwriting_verified_facts():
    other = {**ROW, 'mention': '乙', 'evidence': {'quote': '乙'}}
    result, attempts, calls = generate([json.dumps(ROW) + '\n' + json.dumps(other)],
        saved_entity_keys={('Person', '甲')})
    assert result == {'entities': [other]} and not attempts
    assert calls[-1]['ignored_saved_records'] == 1


def test_rejection_feedback_identifies_the_exact_field():
    bad = {**ROW, 'properties': {'invented': 'value'}}
    _, attempts, _ = generate([json.dumps(bad)])
    assert attempts[-1]['terminal']
    assert "unexpected property 'invented'" in attempts[-1]['message']
    assert attempts[-1]['rejection_reasons'][0]['record'] == 0


def test_foreign_auxiliary_candidate_is_rejected_without_regenerating_valid_peers():
    first, second = ExtractionChunk('a', '甲负责巡检。'), ExtractionChunk('b', '乙负责运输。')
    seen = []
    stub = StubLLM(verification_handler=lambda item, c: 'yes')
    def llm(messages):
        user = json.loads(messages[1]['content'])
        if user['task']['operation'] != 'entity':
            return stub(messages)
        seen.append(user)
        rows = [span_item('Person', name, text) for name, text in [('甲', first.text), ('乙', second.text)]]
        return '\n'.join(json.dumps(row) for row in rows)
    result = extract_split(ONTOLOGY, [first, second], llm=llm, skill_refs=[],
        max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert {row['name'] for row in result['raw']['entities']} == {'甲', '乙'}
    assert len(seen) == 2
    rejects = [a for a in result['attempts'] if a['type'] == 'candidate_rejected']
    assert len(rejects) == 2 and not any(a['terminal'] for a in rejects)


def test_all_source_less_candidates_cannot_become_successful_empty_extraction():
    result = extract_split(ONTOLOGY, [ExtractionChunk('c', '原文无人员')],
        llm=StubLLM(entity_handler=lambda p, c: [span_item('Person', '甲', '虚构原文')]),
        skill_refs=[], max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'failed' and not result['raw']['entities']


def test_persistent_malformed_multi_type_output_splits_and_revalidates_each_scope():
    ontology = '@startuml\nclass Person\nclass Place\n@enduml'
    seen = []
    stub = StubLLM(verification_handler=lambda item, c: 'yes')
    def llm(messages):
        user = json.loads(messages[1]['content'])
        if user['task']['operation'] != 'entity':
            return stub(messages)
        types = user['output_contract']['success_schema']['properties']['type']['enum']
        seen.append(types)
        if len(types) > 1:
            return '{"type":"Person","mention":"甲","type":"Place","mention":"乙地"}'
        return json.dumps(span_item(types[0], '甲' if types[0] == 'Person' else '乙地', '甲到乙地。'))
    result = extract_split(ontology, [ExtractionChunk('c', '甲到乙地。')], llm=llm,
        skill_refs=[], max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert seen == [['Person', 'Place'], ['Person'], ['Place']]
    assert not any(a['terminal'] for a in result['attempts'])
    assert result['attempts'][0]['recovery'] == 'split_request'


def test_type_evidence_rules_reach_generation_and_independent_review():
    from ..prompt_router import build_chat_messages, resolve_document_route
    from ..semantic_evidence import build_verification_messages
    chunk = ExtractionChunk('c', '甲负责巡检，电话12345。')
    plan = compile_plan(ONTOLOGY)
    generated = build_chat_messages(task='entity', plan=plan, route=resolve_document_route(plan, [chunk]), chunk=chunk)
    reviewed = build_verification_messages(chunk=chunk, items=[{'item_id': 'a', 'type': 'Person', 'name': '甲'}])
    for system in [generated[0]['content'], reviewed[0]['content']]:
        assert '不能将所有被提及内容都解释为业务对象' in system
        assert '在原文中只充当日期、编号、联系方式' in system
        assert '本体明确将这种值建模为实体则按其定义判断' in system
        assert '本体明确包括人员或签阅主体时' in system


def test_entity_review_sees_candidate_boundary_as_unverified_data():
    from ..property_grounding import admit_entity_names
    from ..prompt_router import resolve_document_route
    plan = compile_plan(ONTOLOGY)
    chunk = ExtractionChunk('c', '甲负责巡检。')
    row = {'type': 'Person', 'name': '负责巡检的人员', 'properties': {'duty': '巡检'},
           'evidence': {'quote': chunk.text}}
    stub = StubLLM(verification_handler=lambda item, c: 'yes')
    observed = []
    def llm(messages):
        observed.append(json.loads(messages[1]['content']))
        assert '未经证明，不能作为原文证据' in messages[0]['content']
        return stub(messages)
    assert admit_entity_names([row], [chunk], llm, plan=plan,
        route=resolve_document_route(plan, [chunk]), attempts=[], warnings=[]) == [row]
    assert observed[0]['task']['items'][0]['candidate_properties'] == {'duty': '巡检'}
    assert observed[0]['documents'] == chunk.text
    # Entity approval must never create property proof receipts.
    assert not row.get('property_evidence')


def test_entity_review_receives_complete_type_catalog_without_extra_tasks():
    from ..property_grounding import admit_entity_names
    from ..prompt_router import resolve_document_route
    plan = compile_plan('@startuml\nclass Actor\nclass ManagedObject\n@enduml')
    chunk = ExtractionChunk('c', '甲部门执行检查。')
    row = {'type': 'ManagedObject', 'name': '甲部门', 'properties': {}, 'evidence': {'quote': chunk.text}}
    seen = []
    def llm(messages):
        user = json.loads(messages[1]['content'])
        seen.append(user)
        assert 'type `Actor`' in user['ontology'] and 'type `ManagedObject`' in user['ontology']
        assert len(user['task']['items']) == 1
        assert user['task']['items'][0]['type'] == 'ManagedObject'
        return '{"item_id":"entity","supported":"no","reason":"它是执行方而非被治理对象"}'
    # Force independent review of literal names with an explicitly selected policy.
    from ..skill_planner import SkillAssemblyPlanner
    planner = SkillAssemblyPlanner()
    route = resolve_document_route(plan, [chunk])
    planner.prepare(route.to_dict(), skill_refs=['official_document@14'])
    attempts = []
    assert admit_entity_names([row], [chunk], llm, plan=plan, route=route, assembly_planner=planner,
        attempts=attempts, warnings=[], max_attempts=1) == []
    assert len(seen) == 1
    assert attempts[0]['type'] == 'candidate_rejected' and not attempts[0]['terminal']


def test_table_review_preserves_whole_supporting_row_and_original_coordinates():
    from ..property_grounding import _verification_context, locate_in_sources
    from ..source_windows import table_windows
    first = {'subject': '甲设备', 'background': '背景' * 400, 'cause': '潮湿', 'result': '红光带'}
    second = {'subject': '乙设备', 'background': '另行' * 400, 'cause': 'CPU故障', 'result': '不能联动'}
    text = '\n'.join(json.dumps(row, ensure_ascii=False) for row in [first, second])
    source = ExtractionChunk('original', text)
    windows = table_windows(source, 10000)
    assert len(windows) == 1
    spans = [locate_in_sources('潮湿', windows)]
    entry = ({'type': 'Rule', 'name': '潮湿导致红光带'}, 'cause', '潮湿', spans, {})
    reviewed = _verification_context([entry], windows)
    visible = '\n'.join(part.text for part in reviewed)
    assert '甲设备' in visible and '潮湿' in visible and '红光带' in visible
    assert '乙设备' not in visible and 'CPU故障' not in visible
    assert all(source.text[int(part.metadata.get('_v31_window_start', 0)):
        int(part.metadata.get('_v31_window_start', 0)) + len(part.text)] == part.text for part in reviewed)


@pytest.mark.parametrize('wire_fits', [True, False])
def test_minimum_scope_uses_actual_budget_without_relaxing_hard_limit(monkeypatch, wire_fits):
    from .. import candidate_generation
    seen_reserves = []
    def fits(messages, llm=None, *, reserve=2048):
        seen_reserves.append(reserve)
        return wire_fits and reserve == 0
    monkeypatch.setattr(candidate_generation, 'fits', fits)
    result = extract_split(ONTOLOGY, [ExtractionChunk('c', '甲负责巡检。')], skill_refs=[],
        llm=StubLLM(entity_handler=lambda p, c: [span_item('Person', '甲', c['text'])]),
        max_attempts=1, max_repair_rounds=0)
    assert result['status'] == ('succeeded' if wire_fits else 'failed')
    assert all(reserve == 0 for reserve in seen_reserves)
    if not wire_fits:
        assert result['error_code'] == 'LLM_INPUT_BUDGET_EXCEEDED'


@pytest.mark.parametrize('has_other', [True, False])
def test_single_type_early_stop_gets_one_bounded_scan_of_remaining_types(has_other, monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_TYPE_COMPLETION_AUDIT', 'true')
    ontology = '@startuml\nclass Person\nclass Place\nclass Device\n@enduml'
    text = '甲位于乙地。' + '背景说明。' * 120
    seen = []
    stub = StubLLM(verification_handler=lambda i, c: 'yes')
    def llm(messages):
        user = json.loads(messages[1]['content'])
        if user['task']['operation'] != 'entity':
            return stub(messages)
        types = user['output_contract']['success_schema']['properties']['type']['enum']
        seen.append(tuple(types))
        if len(types) > 1:
            return json.dumps(span_item('Person', '甲', '甲位于乙地。'))
        if types == ['Place'] and has_other:
            return json.dumps(span_item('Place', '乙地', '甲位于乙地。'))
        return '{}'
    result = extract_split(ontology, [ExtractionChunk('c', text)], llm=llm, skill_refs=[],
        max_attempts=1, max_repair_rounds=0, max_workers=2)
    assert result['status'] == 'succeeded'
    assert len(seen) == 3 and set(seen) == {('Device', 'Person', 'Place'), ('Device',), ('Place',)}
    assert {e['type'] for e in result['raw']['entities']} == ({'Person', 'Place'} if has_other else {'Person'})
    assert not any(a['terminal'] for a in result['attempts'])


def test_remaining_type_failure_keeps_its_own_terminal_diagnostics(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_TYPE_COMPLETION_AUDIT', 'true')
    ontology = '@startuml\nclass Person\nclass Place\nclass Device\n@enduml'
    stub = StubLLM(verification_handler=lambda i, c: 'yes')
    def llm(messages):
        user = json.loads(messages[1]['content'])
        if user['task']['operation'] != 'entity':
            return stub(messages)
        types = user['output_contract']['success_schema']['properties']['type']['enum']
        if len(types) > 1:
            return json.dumps(span_item('Person', '甲', '甲到岗。'))
        if types == ['Device']:
            raise TimeoutError('provider failed')
        return '{}'
    result = extract_split(ontology, [ExtractionChunk('c', '甲到岗。' + '背景。' * 180)],
        llm=llm, skill_refs=[], max_attempts=1, max_repair_rounds=0, max_workers=2, best_effort=True)
    assert result['status'] == 'partial'
    assert [e['name'] for e in result['raw']['entities']] == ['甲']
    assert any(a['type'] == 'timeout' and a['terminal'] for a in result['attempts'])


def test_default_does_not_expand_a_terminology_source_into_other_type_tasks(monkeypatch):
    monkeypatch.delenv('KG_EXTRACT_V31_TYPE_COMPLETION_AUDIT', raising=False)
    ontology = '@startuml\nclass Terminology\nclass Problem\nclass Solution\n@enduml'
    text = '故障现象：发生故障时记录的异常状态。' + '背景说明。' * 120
    stub = StubLLM(verification_handler=lambda i, c: 'yes')
    calls = []
    def llm(messages):
        user = json.loads(messages[1]['content'])
        if user['task']['operation'] != 'entity':
            return stub(messages)
        calls.append(user)
        return json.dumps(span_item('Terminology', '故障现象', '故障现象：发生故障时记录的异常状态。'))
    result = extract_split(ontology, [ExtractionChunk('c', text)], llm=llm, skill_refs=[],
        max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'succeeded' and len(calls) == 1
    assert [e['type'] for e in result['raw']['entities']] == ['Terminology']


@pytest.mark.parametrize('slow,status,delay,should_split', [
    (90, 502, 0, True), (90, 504, 0, True), (1, 502, 0, False),
    (90, 401, 0, False), (90, 429, 10, False), (90, 502, 10, False),
])
def test_slow_gateway_failure_splits_but_quota_and_permanent_errors_do_not(monkeypatch, slow, status, delay, should_split):
    from types import SimpleNamespace
    from .. import candidate_generation
    clock = {'value': 0}
    monkeypatch.setattr(candidate_generation, 'time', SimpleNamespace(monotonic=lambda: clock['value']))
    class Error(RuntimeError):
        status_code = status
        response = SimpleNamespace(headers={'Retry-After': str(delay)})
    def llm(messages):
        clock['value'] = slow
        raise Error('private provider details')
    attempts = []
    generate_structured(llm,
        [{'role': 'system', 'content': 'JSONL'}, {'role': 'user', 'content': '{"task":{"operation":"entity"}}'}],
        compile_plan(ONTOLOGY).output_schemas['entity_candidates'], use_scheduler=False,
        attempts=attempts, pass_name='entity', chunk_id='c', max_attempts=1,
        retry_base_seconds=0, split_on_overflow=True)
    assert attempts[-1]['adaptive_split'] == should_split
    assert attempts[-1]['terminal']
    assert 'private' not in attempts[-1]['message']


@pytest.mark.parametrize('bad_child', [False, True])
def test_malformed_bulk_verdicts_get_real_singleton_review_with_isolated_errors(bad_child):
    from ..property_grounding import ground_properties
    plan = compile_plan('@startuml\nclass Person {\n + duty: String\n + job: String\n}\n@enduml')
    chunk = ExtractionChunk('c', '甲是检查员，负责巡检。')
    rows = [{'type': 'Person', 'name': '甲', 'properties': {'duty': '巡检', 'job': '检查员'},
             'evidence': {'quote': chunk.text}}]
    seen = []
    def llm(messages):
        items = json.loads(messages[1]['content'])['task']['items']
        seen.append([item['item_id'] for item in items])
        if len(items) > 1:
            return '{"item_id":"0:duty","supported":"yes","reason":"原文支持","item_id":"0:job","supported":"no","reason":"重复字段"}'
        item = items[0]
        if bad_child and item['property'] == 'job':
            return '{"item_id":"0:job","supported":"no","supported":"yes","reason":"仍有重复字段"}'
        return json.dumps({'item_id': item['item_id'], 'supported': 'yes', 'reason': '原文支持'})
    attempts = []
    result = ground_properties(rows, [chunk], llm, plan=plan, attempts=attempts,
        warnings=[], max_attempts=2, retry_base_seconds=0, max_workers=2)
    assert seen[:2] == [['0:duty', '0:job'], ['0:duty', '0:job']]
    assert result[0]['properties'] == ({'duty': '巡检'} if bad_child else {'duty': '巡检', 'job': '检查员'})
    assert all(not a['terminal'] for a in attempts[:2])
    assert bool([a for a in attempts[2:] if a['terminal']]) == bad_child
    assert attempts[1]['recovery'] == 'split_request'  # Only the terminal parent attempt is converted.


def test_known_ids_in_truncated_bulk_response_must_be_reverified_without_cache():
    from ..property_grounding import ground_properties
    plan = compile_plan('@startuml\nclass Person {\n + duty: String\n + job: String\n}\n@enduml')
    chunk = ExtractionChunk('c', '甲是检查员，负责巡检。')
    rows = [{'type': 'Person', 'name': '甲', 'properties': {'duty': '巡检', 'job': '检查员'},
             'evidence': {'quote': chunk.text}}]
    seen = []
    def llm(messages):
        items = json.loads(messages[1]['content'])['task']['items']
        seen.append([item['item_id'] for item in items])
        text = '\n'.join(json.dumps({'item_id': item['item_id'], 'supported': 'yes', 'reason': 'source'}) for item in items)
        if len(items) > 1:
            return StructuredLLMResponse(text=text, api='chat.completions', response_mode='plain',
                                         status='incomplete', finish_reason='length')
        return text
    attempts = []
    result = ground_properties(rows, [chunk], llm, plan=plan, attempts=attempts,
        warnings=[], max_attempts=1, retry_base_seconds=0, max_workers=2)
    assert len(seen) == 3 and sorted(seen[1:]) == [['0:duty'], ['0:job']]
    assert result[0]['properties'] == {'duty': '巡检', 'job': '检查员'}
    assert not any(a['terminal'] for a in attempts)
