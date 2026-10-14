"""JSONL output is plain text transport with strict local validation."""
import json

import pytest

from ..candidate_generation import decode_jsonl_entities, generate_structured
from ..ontology_compiler import compile_plan
from ..llm_client import StructuredLLMResponse
from .test_llm_compatibility import _Client, LLMCompatibilityTests

ROW = {'type': 'Person', 'mention': '张三', 'evidence': {'quote': '张三\n负责检查'}}
LINE = json.dumps(ROW, ensure_ascii=False)


def test_jsonl_preserves_multiple_rows_and_escaped_newlines():
    assert decode_jsonl_entities(LINE + '\n\n' + LINE) == ({'entities': [ROW, ROW]}, None)
    assert decode_jsonl_entities('{}') == ({'entities': []}, None)


@pytest.mark.parametrize('text', ['', '[]', '{"entities": []}', '```jsonl\n' + LINE + '\n```',
                                  LINE + '\n{"type":', LINE + '\n{}', '说明\n' + LINE])
def test_invalid_jsonl_never_silently_returns_partial_results(text):
    parsed, error = decode_jsonl_entities(text)
    assert parsed is None and error


@pytest.mark.parametrize('mode', ['json_schema', 'json_object', 'plain'])
def test_even_legacy_context_cannot_enable_json_mode(mode):
    client = _Client()
    llm = LLMCompatibilityTests._llm(client, base_url='https://api.siliconflow.cn/v1')
    llm.generate_structured({}, [{'role': 'user', 'content': '返回 JSONL'}], {'response_mode': mode})
    assert len(client.chat.completions.calls) == 1
    assert 'response_format' not in client.chat.completions.calls[0]
    assert client.responses.calls == []


def test_retry_is_plain_and_rejects_truncated_response():
    schema = compile_plan('@startuml\nclass Person\n@enduml').output_schemas['entity_candidates']
    contexts = []
    class LLM:
        def generate_structured(self, schema, messages, run_context):
            contexts.append(run_context)
            if len(contexts) == 1:
                return StructuredLLMResponse(text=LINE, api='chat.completions', response_mode='plain',
                                             status='incomplete', finish_reason='length')
            return json.dumps({**ROW, 'mention': '李四', 'evidence': {'quote': '李四'}})
    attempts = []
    result = generate_structured(LLM(), [{'role': 'system', 'content': 'JSONL'},
                                 {'role': 'user', 'content': '{"task": {"operation": "entity"}}'}],
                                 schema, use_scheduler=False, attempts=attempts, pass_name='entity',
                                 chunk_id='c1', max_attempts=2, retry_base_seconds=0)
    assert result == {'entities': [ROW, {**ROW, 'mention': '李四', 'evidence': {'quote': '李四'}}]}
    assert attempts[0]['type'] == 'incomplete'
    assert [c['response_mode'] for c in contexts] == ['plain', 'plain']


def run_rows(responses, *, schema=None, validator=None):
    schema = schema or compile_plan('@startuml\nclass Person\n@enduml').output_schemas['entity_candidates']
    attempts, calls = [], []
    pending = iter(responses)
    result = generate_structured(lambda messages: next(pending),
        [{'role': 'system', 'content': 'JSONL'}, {'role': 'user', 'content': '{"task": {"operation": "entity"}}'}],
        schema, use_scheduler=False, attempts=attempts, pass_name='entity', chunk_id='c1',
        max_attempts=len(responses), retry_base_seconds=0, call_metrics=calls, validator=validator)
    return result, attempts, calls


@pytest.mark.parametrize('flagged', [False, True])
def test_truncated_tail_preserves_valid_records_and_reports_partial(flagged):
    text = LINE + '\n{"type":"Person","mention":"李'
    response = (StructuredLLMResponse(text=text, api='chat.completions', response_mode='plain',
                status='incomplete', finish_reason='length') if flagged else text)
    result, attempts, calls = run_rows([response])
    assert result == {'entities': [ROW]}
    assert attempts[-1]['terminal'] is True
    assert attempts[-1]['recovery'] == 'jsonl_valid_records'
    assert attempts[-1]['recovered_items'] == 1
    assert attempts[-1]['rejected_records'] == 1
    assert calls[-1]['status'] == 'partial'


def test_bad_middle_line_and_invalid_type_do_not_discard_valid_neighbours():
    second = {**ROW, 'mention': '李四', 'evidence': {'quote': '李四'}}
    bad = {**ROW, 'type': 'Invented'}
    result, attempts, _ = run_rows([LINE + '\n损坏行\n' + json.dumps(bad) + '\n' + json.dumps(second)])
    assert result == {'entities': [ROW, second]}
    assert attempts[-1]['rejected_records'] == 2
    assert attempts[-1]['terminal']


def test_recovered_candidate_still_requires_ontology_and_source_validation():
    from ..chunk_context import ExtractionChunk
    from ..output_contracts import validate_candidate_properties
    plan = compile_plan('@startuml\nclass Person {\n + 职责 (duty): String\n}\n@enduml')
    chunk = ExtractionChunk('c1', '张三\n负责检查')
    invented = {**ROW, 'mention': '火星人', 'evidence': {'quote': '火星人'}}
    result, attempts, _ = run_rows([json.dumps(invented) + '\n' + LINE + '\n{"type":'],
        schema=plan.output_schemas['entity_candidates'],
        validator=lambda payload: validate_candidate_properties(payload, plan, chunk=chunk))
    assert result == {'entities': [ROW]}
    assert attempts[-1]['rejected_records'] == 2


@pytest.mark.parametrize('later', ['{}', TimeoutError('provider timed out')])
def test_later_empty_or_failed_retry_cannot_erase_valid_prefix(later):
    sequence = iter([LINE + '\n{"type":', later])
    def llm(messages):
        response = next(sequence)
        if isinstance(response, Exception):
            raise response
        return response
    attempts = []
    result = generate_structured(llm,
        [{'role': 'system', 'content': 'JSONL'}, {'role': 'user', 'content': '{"task":{"operation":"entity"}}'}],
        compile_plan('@startuml\nclass Person\n@enduml').output_schemas['entity_candidates'],
        use_scheduler=False, attempts=attempts, pass_name='entity', chunk_id='c1',
        max_attempts=2, retry_base_seconds=0)
    assert result == {'entities': [ROW]}
    assert attempts[-1]['terminal']  # An unfinished discovery cannot be completed by an empty marker.


def test_complete_retry_keeps_prior_valid_entity_without_name_normalization():
    first = {**ROW, 'mention': 'A B', 'evidence': {'quote': 'A B'}}
    second = {**ROW, 'mention': 'AB', 'evidence': {'quote': 'AB'}}
    result, attempts, _ = run_rows([json.dumps(first) + '\n{"type":', json.dumps(second)])
    assert result == {'entities': [first, second]}
    assert not any(a['terminal'] for a in attempts)


def test_retry_input_budget_exhaustion_preserves_rows_and_recovery_diagnostics(monkeypatch):
    from .. import candidate_generation
    budget_checks = iter([True, False])
    monkeypatch.setattr(candidate_generation, 'fits', lambda *args, **kwargs: next(budget_checks))
    result, attempts, _ = run_rows([LINE + '\n{"type":', '{}'])
    assert result == {'entities': [ROW]}
    assert attempts[-1]['type'] == 'input_budget'
    assert attempts[-1]['terminal'] is True
    assert attempts[-1]['recovery'] == 'jsonl_valid_records'
    assert attempts[-1]['recovered_items'] == 1


def test_verification_keeps_only_unique_in_scope_complete_verdicts():
    from ..output_contracts import verification_schema, validate_verification_items
    items = [{'item_id': 'p0'}, {'item_id': 'p1'}, {'item_id': 'p2'}]
    rows = [{'item_id': 'p0', 'supported': 'yes', 'reason': '证据'},
            {'item_id': 'p1', 'supported': 'yes', 'reason': '证据'},
            {'item_id': 'p1', 'supported': 'no', 'reason': '矛盾'},
            {'item_id': 'invented', 'supported': 'yes', 'reason': '伪造'},
            {'item_id': [], 'supported': 'yes', 'reason': '非法 ID'}]
    text = '\n'.join(json.dumps(row) for row in rows) + '\n{"item_id":"p2"'
    result, attempts, _ = run_rows([text], schema=verification_schema(items),
        validator=lambda p: validate_verification_items(p, items))
    assert result == {'verdicts': [rows[0]]}
    assert attempts[-1]['missing_item_ids'] == ['p1', 'p2']


def test_duplicate_json_keys_never_produce_an_accepted_verdict():
    from ..output_contracts import verification_schema
    result, attempts, _ = run_rows([
        '{"item_id":"p0","supported":"no","supported":"yes","reason":"重复键"}'
    ], schema=verification_schema([{'item_id': 'p0'}]))
    assert result is None
    assert attempts[-1]['recovered_items'] == 0


def test_partial_verifier_retains_confirmed_property_and_removes_missing_one():
    from .. import extract_harness
    from ..chunk_context import ExtractionChunk
    from .stub_llm import StubLLM
    ontology = '@startuml\nclass Person {\n + 职责 (duty): String\n + 岗位 (job): String\n}\n@enduml'
    text = '张三是检查员，负责检查。'
    def entity(prompt, chunk):
        return [{**ROW, 'properties': {'duty': '检查', 'job': '检查员'},
                 'evidence': {'quote': text, 'property_quotes': {'duty': [text], 'job': [text]}}}]
    stub = StubLLM(entity_handler=entity)
    def llm(messages):
        user = json.loads(messages[1]['content'])
        if user['task']['operation'] == 'evidence_verification':
            assert user['output_contract']['format'] == 'jsonl'
            return '{"item_id":"0:duty","supported":"yes","reason":"原文支持"}\n{"item_id":"0:job"'
        return stub(messages)
    result = extract_harness(ontology, [ExtractionChunk('c1', text)], llm=llm,
        best_effort=True, use_scheduler=False, max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'partial'
    assert result['raw']['entities'][0]['properties'] == {'duty': '检查'}
    assert 'duty' in result['raw']['entities'][0]['property_evidence']
    assert result['verification']['valid']


def test_truncated_entity_response_retains_facts_but_not_complete_status():
    from .. import extract_harness
    from ..chunk_context import ExtractionChunk
    row = {**ROW, 'properties': {}}
    result = extract_harness('@startuml\nclass Person\n@enduml',
        [ExtractionChunk('c1', '张三\n负责检查')], llm=lambda m: json.dumps(row) + '\n{"type":',
        best_effort=True, use_scheduler=False, max_attempts=1, max_repair_rounds=0, skill_refs=[])
    assert result['status'] == 'partial'
    assert [e['name'] for e in result['raw']['entities']] == ['张三']
    assert result['run_trace']['coverage_ledger'][0]['entity_scan_complete'] is False
    assert result['run_trace']['attempts'][0]['recovery'] == 'jsonl_valid_records'
    assert result['run_trace']['attempts'][0]['rejected_records'] == 1


def test_property_completion_keeps_complete_entity_when_later_line_is_truncated():
    from ..output_contracts import property_entity_schema
    plan = compile_plan('@startuml\nclass Person {\n + 职责 (duty): String\n}\n@enduml')
    row = {'type': 'Person', 'name': '张三', 'properties': {'duty': '检查'},
           'evidence': {'quote': '张三负责检查'}}
    schema = property_entity_schema([row], plan)
    result, attempts, _ = run_rows([json.dumps(row) + '\n{"type":'], schema=schema,
        validator=lambda p: [] if [(e['type'], e['name']) for e in p['entities']] == [('Person', '张三')] else ['invalid keys'])
    assert result == {'entities': [row]}
    assert attempts[-1]['terminal']


def test_literal_unicode_line_separator_inside_json_string_is_not_a_jsonl_boundary():
    row = {**ROW, 'mention': '张\u2028三'}
    result, attempts, _ = run_rows([json.dumps(row, ensure_ascii=False)])
    assert result == {'entities': [row]}
    assert not attempts
