"""Request selection failures, dynamic catalogs and frozen whole-request policies."""
from copy import deepcopy
import json
import time

import pytest

from .. import extract_harness, extract_aggregate
from ..chunk_context import ExtractionChunk
from ..skill_registry import SkillConfigurationError, SkillRegistry
from ..skill_planner import SkillAssemblyPlanner
from .stub_llm import StubLLM, span_item

ONTOLOGY = '@startuml\nclass "新领域对象" as RenamedObject {\n + 说明 (body): String\n}\n@enduml'


def skill(root, name='new_domain', version='1'):
    path = root / name / 'SKILL.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'''---
name: {name}
version: "{version}"
description: 新领域对象本体；根据对象说明抽取原文事实，不处理其他领域。
stages: [entity, property, repair]
---
## rules:all
保持新领域对象的限定。
## rules:entity
保留独立对象。
## rules:property
说明按原文补全。
## rules:repair
仅修复列出问题。
''', encoding='utf-8')
    return path


def decision(refs, status=None):
    return {'status': status or ('selected' if refs else 'no_match'), 'skill_refs': refs,
            'reason': '根据本体定义与技能描述选择'}


def registered_ref(name):
    return next(entry['skill_ref'] for entry in SkillRegistry().selection_catalog() if entry['name'] == name)


def run(llm, **kwargs):
    return extract_harness(ONTOLOGY, [ExtractionChunk('a', '甲对象。'), ExtractionChunk('b', '乙对象。')],
        llm=llm, use_scheduler=False, max_attempts=1, max_repair_rounds=0,
        retry_base_seconds=0, **kwargs)


@pytest.mark.parametrize('entrypoint', [extract_harness, extract_aggregate])
def test_new_domain_works_without_route_hints_and_is_shared_by_all_entities(tmp_path, entrypoint):
    skill(tmp_path)
    def select(user):
        assert user['task']['allowed_types'] == ['RenamedObject']
        assert user['ontology'] == ONTOLOGY
        assert user['task']['skills'][0]['description'].startswith('新领域对象本体')
        assert '保持新领域' not in json.dumps(user, ensure_ascii=False)
        return decision(['new_domain@1'])
    llm = StubLLM(selection_handler=select,
        entity_handler=lambda p, c: [span_item('RenamedObject', c['text'][:-1])],
        verification_handler=lambda i, c: 'yes')
    chunks = [ExtractionChunk('a', '甲对象。'), ExtractionChunk('b', '乙对象。')]
    before = deepcopy(chunks)
    result = entrypoint(ONTOLOGY, chunks, llm=llm, use_scheduler=False, skills_dir=str(tmp_path))
    assert chunks == before
    assert result['status'] == 'succeeded'
    assert len(result['raw']['entities']) == 2
    assert result['profile']['document_route']['kind'] == 'generic'
    assert sum(t == 'skill_selection' for t, _ in llm.calls) == 1
    for stage, prompt in llm.calls:
        if stage != 'skill_selection':
            assert '保持新领域对象的限定' in json.loads(prompt)['domain_policy']
    audit = result['profile']['prompt_assembly']['plans'][0]
    assert audit['selection']['skill_refs'] == ['new_domain@1']
    assert audit['selection']['source'] == 'semantic'
    assert len(audit['selection']['catalog_sha256']) == 64
    assert len(audit['read_skills'][0]['sha256']) == 64
    assert result['raw']['relations'] == []


@pytest.mark.parametrize('name,ontology,type_id', [
    ('rule', '@startuml\nclass RuleType {\n + antecedent: String\n + consequent: String\n}\n@enduml', 'RuleType'),
    ('trouble', '@startuml\nclass FaultType\nclass FaultComponent\nclass FaultHandlingMethod\n@enduml', 'FaultType'),
])
def test_generic_route_can_select_rule_or_trouble_for_the_same_source(name, ontology, type_id):
    llm = StubLLM(selection_handler=lambda user: decision([
        next(s['skill_ref'] for s in user['task']['skills'] if s['name'] == name)]),
        entity_handler=lambda p, c: [span_item(type_id, 'RTU程序错误')],
        verification_handler=lambda i, c: 'yes')
    result = extract_harness(ontology, [ExtractionChunk('a', 'RTU程序错误，重启后恢复正常。')],
        llm=llm, use_scheduler=False, max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert result['profile']['document_route']['kind'] == 'generic'
    assert result['profile']['prompt_assembly']['plans'][0]['read_skills'][0]['skill_id'] == name
    assert all(json.loads(p)['domain_policy'] for t, p in llm.calls if t != 'skill_selection')


@pytest.mark.parametrize('payload', [decision(['missing@1']), decision(['rule@wrong']),
    decision(['rule@10', 'rule@10']), decision([], 'selected'), decision(['rule@10'], 'no_match'),
    decision([], 'ambiguous'), {'status': 'no_match', 'skill_refs': []}])
def test_invalid_or_ambiguous_selection_fails_without_extracting(payload):
    llm = StubLLM(selection_handler=lambda user: payload)
    result = run(llm)
    assert result['status'] == 'failed'
    assert {t for t, _ in llm.calls} == {'skill_selection'}
    assert result['graph']['nodes'] == []
    assert any(a['pass'] == 'skill_selection' and a['terminal'] for a in result['attempts'])
    assert all(not item['entity_scan_complete'] for item in result['run_trace']['coverage_ledger'])


def test_no_match_is_a_successful_decision_and_empty_policy():
    llm = StubLLM(selection_handler=lambda user: decision([]),
        entity_handler=lambda p, c: [span_item('RenamedObject', c['text'][:-1])])
    result = run(llm)
    assert result['status'] == 'succeeded'
    assert result['profile']['prompt_assembly']['plans'][0]['selection']['status'] == 'no_match'
    assert all(json.loads(p)['domain_policy'] == '' for t, p in llm.calls)


def test_binding_pins_versions_and_does_not_call_selector(tmp_path):
    skill(tmp_path)
    llm = StubLLM(entity_handler=lambda p, c: [span_item('RenamedObject', c['text'][:-1])],
                  verification_handler=lambda i, c: 'yes')
    result = run(llm, skills_dir=str(tmp_path), skill_refs=['new_domain@1'])
    assert result['status'] == 'succeeded'
    assert not any(t == 'skill_selection' for t, _ in llm.calls)
    assert result['profile']['prompt_assembly']['plans'][0]['selection']['source'] == 'configuration'
    with pytest.raises(SkillConfigurationError):
        run(llm, skills_dir=str(tmp_path), skill_refs=['new_domain@2'])


def test_file_edits_during_selection_do_not_change_rules(tmp_path):
    path = skill(tmp_path)
    def select(user):
        path.write_text(path.read_text().replace('保持新领域对象的限定', '运行中新规则'))
        return decision(['new_domain@1'])
    llm = StubLLM(selection_handler=select,
        entity_handler=lambda p, c: [span_item('RenamedObject', c['text'][:-1])],
        verification_handler=lambda i, c: 'yes')
    result = run(llm, skills_dir=str(tmp_path))
    assert result['status'] == 'succeeded'
    assert all('运行中新规则' not in p for t, p in llm.calls)
    assert all('保持新领域对象的限定' in p for t, p in llm.calls if t != 'skill_selection')


def test_request_deadline_and_input_budget_fail_before_calls(monkeypatch):
    llm = StubLLM()
    llm.deadline = time.monotonic() - 1
    assert run(llm)['error_code'] == 'LLM_TIMEOUT'
    assert llm.calls == []
    del llm.deadline
    monkeypatch.setenv('KG_EXTRACT_V31_MAX_INPUT_BYTES', '100')
    result = run(llm)
    assert result['status'] == 'failed'
    assert any(a['type'] == 'input_budget' for a in result['attempts'])
    assert llm.calls == []


def test_sampling_includes_the_last_chunk_and_records_coverage():
    chunks = [ExtractionChunk(str(i), f'正文{i}。') for i in range(20)]
    llm = StubLLM(selection_handler=lambda user: decision([]))
    result = extract_harness(ONTOLOGY, chunks, llm=llm, use_scheduler=False, max_repair_rounds=0)
    audit = result['profile']['prompt_assembly']['plans'][0]['selection']['sampling']
    assert audit['total_chunks'] == 20
    assert {s['index'] for s in audit['samples']} >= {0, 19}
    assert len(audit['samples']) == 8


def test_invalid_selection_retry_keeps_inputs_and_uses_object_feedback():
    seen = []
    def select(user):
        seen.append(user)
        return decision(['missing@1']) if len(seen) == 1 else decision([])
    llm = StubLLM(selection_handler=select)
    result = extract_harness(ONTOLOGY, [ExtractionChunk('a', '原文。')], llm=llm,
                            use_scheduler=False, max_attempts=2, retry_base_seconds=0, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert len(seen) == 2
    retried = deepcopy(seen[1])
    assert '完整 JSON 对象' in retried.pop('retry_feedback')
    assert retried == seen[0]
    assert not result['attempts'][0]['terminal']


def test_prepared_selection_cannot_be_changed():
    planner = SkillAssemblyPlanner()
    route = {'kind': 'generic'}
    planner.prepare(route, skill_refs=[registered_ref('rule')])
    with pytest.raises(SkillConfigurationError, match='selection'):
        planner.prepare(route, skill_refs=[registered_ref('trouble')])


@pytest.mark.parametrize('raw', [
    '{"status":"selected","status":"no_match","skill_refs":[],"reason":"duplicate"}',
    '{"status":"no_match","skill_refs":[],"reason":NaN}',
])
def test_duplicate_status_and_nonfinite_json_cannot_be_accepted(raw):
    result = run(lambda messages: raw)
    assert result['status'] == 'failed'
    assert result['attempts'][0]['type'] == 'json'
    assert result['graph']['nodes'] == []


def test_selector_uses_existing_scheduler(monkeypatch):
    from contextlib import contextmanager
    from .. import candidate_generation
    slots = []
    class Scheduler:
        @contextmanager
        def request_slot(self, deadline=None):
            slots.append(deadline)
            yield
    monkeypatch.setattr(candidate_generation, 'get_llm_scheduler', lambda: Scheduler())
    llm = StubLLM(selection_handler=lambda user: decision([]))
    result = extract_harness(ONTOLOGY, [ExtractionChunk('a', '原文。')], llm=llm,
                            use_scheduler=True, max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert len(slots) == len(result['run_trace']['calls']) == 2


def test_late_selector_result_is_not_accepted():
    llm = StubLLM()
    def select(user):
        llm.deadline = time.monotonic() - 1
        return decision(['rule@10'])
    llm.selection_handler = select
    result = run(llm)
    assert result['error_code'] == 'LLM_TIMEOUT'
    assert [stage for stage, _ in llm.calls] == ['skill_selection']


def test_two_compatible_skills_keep_all_rule_blocks_for_the_whole_request(tmp_path):
    skill(tmp_path, 'one')
    skill(tmp_path, 'two')
    llm = StubLLM(selection_handler=lambda user: decision(['two@1', 'one@1']),
        entity_handler=lambda p, c: [span_item('RenamedObject', c['text'][:-1])],
        verification_handler=lambda i, c: 'yes')
    result = run(llm, skills_dir=str(tmp_path))
    assert result['status'] == 'succeeded'
    policy = json.loads(next(p for t, p in llm.calls if t == 'entity'))['domain_policy']
    assert policy.count('保持新领域对象的限定') == 2
    assert [s['skill_id'] for s in result['profile']['prompt_assembly']['plans'][0]['read_skills']] == ['one', 'two']


def test_provider_cannot_replace_the_local_selection_schema():
    class MutatingProvider:
        def generate_structured(self, schema, messages, context):
            schema['properties']['skill_refs']['items']['enum'].append('forged@1')
            messages[1]['content'] = '{}'
            return decision(['forged@1'])
    result = run(MutatingProvider())
    assert result['status'] == 'failed'
    assert result['graph']['nodes'] == []
    assert result['attempts'][0]['type'] == 'schema'
