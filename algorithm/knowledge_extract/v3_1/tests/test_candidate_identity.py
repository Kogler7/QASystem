"""Regression cases for cross-record identity and per-field source authority."""
import json

import pytest

from ..chunk_context import ExtractionChunk
from ..chunk_context import ChunkContextManager
from ..property_enrichment import _support_sources
from ..split_extractor import extract_split
from ..property_grounding import _verification_context, ground_properties, locate_in_sources
from ..ontology_compiler import compile_plan
from ..semantic_evidence import build_verification_messages
from .stub_llm import StubLLM, span_item

ONTOLOGY = '@startuml\nclass Inspection {\n + procedure: String\n}\nnote right of Inspection\n类定义：针对明确对象实施的一次检查活动。\nend note\n@enduml'


def test_conflicting_candidates_are_qualified_before_merge_and_reproved():
    reviewed = []
    def review(item, chunk):
        reviewed.append(dict(item))
        if not item.get('property'):
            return 'yes' if item['name'] in {'检查', '泵甲检查', '泵乙检查'} else 'no'
        return 'yes'
    def identities(user):
        assert user['task']['operation'] == 'candidate_identity'
        assert '泵甲' in user['documents'] and '泵乙' in user['documents']
        return {item['item_id']: {'mention': '泵甲检查' if index == 0 else '泵乙检查'}
                for index, item in enumerate(user['task']['items'])}
    llm = StubLLM(entity_handler=lambda p, c: [{**span_item('Inspection', '检查', c['text']),
        'properties': {'procedure': '测电阻' if '泵甲' in c['text'] else '测压力'}}],
        identity_handler=identities, verification_handler=review)
    result = extract_split(ONTOLOGY, [ExtractionChunk('a', '泵甲：检查步骤为测电阻。'),
        ExtractionChunk('b', '泵乙：检查步骤为测压力。')], llm=llm, skill_refs=[],
        max_attempts=1, retry_base_seconds=0, local_previous=0, local_next=0)
    assert result['status'] == 'succeeded'
    assert {e['name'] for e in result['raw']['entities']} == {'泵甲检查', '泵乙检查'}
    assert {i['name'] for i in reviewed if i.get('property')} == {'检查', '泵甲检查', '泵乙检查'}
    for row in result['raw']['entities']:
        assert row['entity_evidence']['name'] == row['name']
        assert row['property_evidence']['procedure']['value'] == row['properties']['procedure']


@pytest.mark.parametrize('failure', ['unsupported', 'missing'])
def test_failed_qualification_preserves_conflict_and_original_candidates(failure):
    llm = StubLLM(entity_handler=lambda p, c: [{**span_item('Inspection', '检查', c['text']),
        'properties': {'procedure': c['text']}}],
        identity_handler=lambda u: ({} if failure == 'missing' else
            {i['item_id']: {'mention': '无来源对象检查'} for i in u['task']['items']}),
        verification_handler=lambda i, c: 'no' if i['name']=='无来源对象检查' else 'yes')
    result = extract_split(ONTOLOGY, [ExtractionChunk('a', '泵甲检查。'), ExtractionChunk('b', '泵乙检查。')],
        llm=llm, skill_refs=[], max_attempts=1, max_repair_rounds=0, retry_base_seconds=0,
        local_previous=0, local_next=0, best_effort=True)
    assert result['status'] == 'partial'
    assert [e['name'] for e in result['raw']['entities']] == ['检查']
    assert any(w['code']=='PROPERTY_CONFLICT_UNRESOLVED' for w in result['warnings'])


def test_property_context_does_not_import_first_generic_name_from_other_record():
    sources = [ExtractionChunk('a', '检查：泵甲压力异常。'), ExtractionChunk('b', '检查：泵乙压力正常。')]
    row = {'type': 'Inspection', 'name': '检查', 'evidence': locate_in_sources('检查：泵乙压力正常。', sources)}
    span = locate_in_sources('泵乙压力正常', sources)
    context = _verification_context([(row, 'procedure', '正常', [span], {'item_id': 'x'})], sources)
    assert [s.text for s in context] == ['检查：泵乙压力正常。']


def test_conflict_support_keeps_complete_original_row_and_distant_identity():
    record = {'action':'检查压力', 'padding':'x'*500, 'subject':'泵乙'}
    text = json.dumps({'rows':[{'subject':'无关泵甲'}, record]}, ensure_ascii=False)
    chunk = ExtractionChunk('c', text)
    span = locate_in_sources('检查压力', [chunk])
    parts = list(_support_sources({'property_evidence':{'procedure':{'sources':[span]}}}, ChunkContextManager([chunk])))
    visible = '\n'.join(p.text for p in parts)
    assert '泵乙' in visible and '无关泵甲' not in visible
    for part in parts:
        start = part.metadata['_v31_window_start']
        assert text[start:start+len(part.text)] == part.text


def test_verification_items_have_separate_sources_and_shared_rows_are_sent_once():
    a, b = ExtractionChunk('a', '泵甲压力异常。'), ExtractionChunk('b', '泵乙压力正常。')
    items = [{'item_id': key, 'type': 'Inspection', 'name': '检查', 'property': 'procedure', 'value': '正常'}
             for key in ('a', 'b', 'c')]
    messages = build_verification_messages(chunk=a, items=items,
        item_sources={'a': [a], 'b': [b], 'c': [b]})
    docs = json.loads(messages[1]['content'])['documents']
    assert docs.count('泵乙压力正常。') == 1
    assert '核验项 b, c 的原文' in docs
    assert '其他项的资料不能补足' in messages[0]['content']


@pytest.mark.parametrize('field,comment', [('key', '唯一标识；由结构化系统生成或维护'),
    ('externalKey', 'system-managed identifier')])
def test_system_managed_identifier_cannot_be_minted_even_if_model_would_approve(field, comment):
    plan = compile_plan(f'@startuml\nclass Inspection {{\n + {field}: String //{comment}\n}}\n@enduml')
    row = {'type': 'Inspection', 'name': '检查', 'properties': {field: '检查-2026-10-13'},
           'evidence': {'quote': '2026-10-13，检查泵甲压力正常。'}}
    llm = StubLLM(verification_handler=lambda i,c: 'yes')
    warnings = []
    ground_properties([row], [ExtractionChunk('a', row['evidence']['quote'])], llm,
        attempts=[], warnings=warnings, plan=plan)
    assert row['properties'] == {} and not llm.calls
    assert warnings[0]['code'] == 'PROPERTY_IDENTIFIER_UNGROUNDED'


def test_explicit_existing_system_identifier_still_requires_and_gets_semantic_review():
    plan = compile_plan('@startuml\nclass Inspection {\n + key: String //由系统生成或维护\n}\n@enduml')
    row = {'type': 'Inspection', 'name': '泵甲检查', 'properties': {'key': 'EX-42'},
        'evidence': {'quote': '泵甲检查的系统标识是 EX-42。'}}
    llm = StubLLM(verification_handler=lambda i,c: 'yes')
    ground_properties([row], [ExtractionChunk('a', row['evidence']['quote'])], llm,
        attempts=[], warnings=[], plan=plan)
    assert row['properties'] == {'key': 'EX-42'}
    assert row['property_evidence']['key']['support'] == 'llm'


def test_declared_identity_without_generation_comment_cannot_be_invented():
    plan = compile_plan('@startuml\nclass Rule {\n + key: String //唯一标识\n}\n\' @identity Rule: key\n@enduml')
    row = {'type': 'Rule', 'name': '停机规则', 'properties': {'key': 'RULE-2026-42'},
           'evidence': {'quote': '2026年，泵甲温度超过阈值时停机。'}}
    llm = StubLLM(verification_handler=lambda i, c: 'yes')
    ground_properties([row], [ExtractionChunk('a', row['evidence']['quote'])], llm,
        attempts=[], warnings=[], plan=plan)
    assert row['properties'] == {} and not llm.calls


def test_numeric_identity_present_in_source_retains_declared_type_and_needs_review():
    plan = compile_plan('@startuml\nclass Rule {\n + key: Integer\n}\n\' @identity Rule: key\n@enduml')
    row = {'type': 'Rule', 'name': '停机规则', 'properties': {'key': 42},
           'evidence': {'quote': '停机规则的标识为42。'}}
    ground_properties([row], [ExtractionChunk('a', row['evidence']['quote'])],
        StubLLM(verification_handler=lambda i, c: 'yes'), attempts=[], warnings=[], plan=plan)
    assert row['properties']['key'] == 42
    assert row['property_evidence']['key']['support'] == 'llm'


@pytest.mark.parametrize('value,accepted', [('发文〔2030〕4', False), ('发文〔2030〕45号', True)])
def test_reference_number_cannot_be_a_prefix_of_the_actual_number(value, accepted):
    plan = compile_plan('@startuml\nclass Memo {\n + reference: String //文书原始编号\n}\n@enduml')
    source = ExtractionChunk('a', '通知甲的文书原始编号为发文〔2030〕45号。')
    row = {'type': 'Memo', 'name': '通知甲', 'properties': {'reference': value},
           'evidence': {'quote': source.text}}
    llm = StubLLM(verification_handler=lambda i, c: 'yes')
    warnings = []
    ground_properties([row], [source], llm, attempts=[], warnings=warnings, plan=plan)
    assert bool(row['properties']) is accepted
    assert bool(llm.calls) is accepted
    if not accepted:
        assert warnings[0]['code'] == 'PROPERTY_IDENTIFIER_UNGROUNDED'


@pytest.mark.parametrize('text,accepted', [
    ('规则号142。', False), ('规则号42。', True), ('规则号１４２。', False), ('规则号４２。', True)])
def test_numeric_identity_requires_token_boundaries(text, accepted):
    plan = compile_plan('@startuml\nclass Rule {\n + key: Integer\n}\n\' @identity Rule: key\n@enduml')
    row = {'type': 'Rule', 'name': '规则', 'properties': {'key': 42}, 'evidence': {'quote': text}}
    ground_properties([row], [ExtractionChunk('a', text)], StubLLM(verification_handler=lambda i, c: 'yes'),
                      attempts=[], warnings=[], plan=plan)
    assert bool(row['properties']) is accepted


def test_code_description_can_still_be_a_supported_nonliteral_summary():
    plan = compile_plan('@startuml\nclass Memo {\n + description: String //错误代码说明\n}\n@enduml')
    source = ExtractionChunk('a', '报警Q7表示泵压力过低。')
    row = {'type': 'Memo', 'name': '报警Q7', 'properties': {'description': '泵的低压告警'},
           'evidence': {'quote': source.text}}
    ground_properties([row], [source], StubLLM(verification_handler=lambda i, c: 'yes'),
                      attempts=[], warnings=[], plan=plan)
    assert row['properties'] == {'description': '泵的低压告警'}


@pytest.mark.parametrize('first_value,expected', [('发文〔2030〕4', 'succeeded'), ('发文〔2030〕46号', 'partial')])
def test_prefix_is_rejected_but_two_full_reference_numbers_preserve_conflict(first_value, expected):
    ontology = '@startuml\nclass Memo {\n + reference: String //文书原始编号\n}\n@enduml'
    first_source_value = '发文〔2030〕45号' if first_value.endswith('4') else first_value
    llm = StubLLM(entity_handler=lambda p, c: [{**span_item('Memo', '通知甲', c['text']),
        'properties': {'reference': first_value if '材料甲' in c['text'] else '发文〔2030〕45号'}}],
        identity_handler=lambda u: {i['item_id']: {'mention': i['name']} for i in u['task']['items']},
        verification_handler=lambda i, c: 'yes')
    result = extract_split(ontology, [ExtractionChunk('a', f'材料甲，通知甲编号为{first_source_value}。'),
        ExtractionChunk('b', '材料乙，通知甲编号为发文〔2030〕45号。')], llm=llm, skill_refs=[],
        max_attempts=1, max_repair_rounds=0, retry_base_seconds=0,
        local_previous=0, local_next=0, best_effort=True)
    assert result['status'] == expected
    conflicts = [w for w in result['warnings'] if w['code'] == 'PROPERTY_CONFLICT_UNRESOLVED']
    assert bool(conflicts) is (expected == 'partial')
    if expected == 'succeeded':
        assert result['raw']['entities'][0]['properties']['reference'] == '发文〔2030〕45号'


def test_property_review_sees_direction_and_does_not_prove_fields_in_isolation():
    plan = compile_plan('@startuml\nclass Rule {\n + premise: String\n + result: String\n + link: String\n}\n@enduml')
    source = ExtractionChunk('a', '报警码Q7表示泵压力过低。')
    proposal = {'premise': '泵压力过低', 'result': '报警码Q7', 'link': '表示'}
    seen = []
    def verdict(item, context):
        assert item['candidate_properties'] == proposal
        assert item['property'] in proposal
        seen.append(item['property'])
        return 'no'  # The words occur, but the proposed diagnostic direction is reversed.
    row = {'type': 'Rule', 'name': '报警释义', 'properties': dict(proposal),
           'evidence': {'quote': source.text}}
    ground_properties([row], [source], StubLLM(verification_handler=verdict),
        attempts=[], warnings=[], plan=plan)
    assert set(seen) == set(proposal)
    assert row['properties'] == {} and row['property_evidence'] == {}


def test_single_entity_admission_does_not_force_single_property_batches(monkeypatch):
    from ..property_grounding import admit_entity_names
    monkeypatch.setenv('KG_EXTRACT_V31_ENTITY_VERIFICATION_BATCH_SIZE', '1')
    monkeypatch.setenv('KG_EXTRACT_V31_VERIFICATION_BATCH_SIZE', '4')
    source = ExtractionChunk('a', '泵甲压力正常，泵乙压力异常。')
    plan = compile_plan('@startuml\nclass Observation {\n + value: String\n}\n@enduml')
    calls = []
    stub = StubLLM(verification_handler=lambda i, c: 'yes')
    def llm(messages):
        calls.append(json.loads(messages[-1]['content'])['task']['items'])
        return stub(messages)
    rows = [{'type': 'Observation', 'name': name, 'properties': {'value': value},
             'evidence': {'quote': source.text}} for name, value in [('泵甲观测', '正常'), ('泵乙观测', '异常')]]
    rows = admit_entity_names(rows, [source], llm, attempts=[], warnings=[], plan=plan)
    assert [len(items) for items in calls] == [1, 1]
    ground_properties(rows, [source], llm, attempts=[], warnings=[], plan=plan)
    assert len(calls[-1]) == 2 and all('property' in i for i in calls[-1])
