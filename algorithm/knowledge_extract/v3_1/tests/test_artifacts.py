"""Compact artifacts keep facts, receipts, partial failures and actual messages."""
from copy import deepcopy
import json

import pytest

from .. import export_artifacts, extract_harness
from ..artifacts import build_artifacts
from ..chunk_context import ExtractionChunk
from ..result_contract import to_public_result
from ..run_state import ExtractionResponseError
from .stub_llm import StubLLM, span_item


def aggregate():
    span = {'span_id': 'c1:0:4', 'source_block_ids': ['c1'], 'quote': '张三\n检查',
            'start_char': 0, 'end_char': 4, 'evidence_type': 'explicit'}
    other = {**span, 'span_id': 'c2:0:4', 'source_block_ids': ['c2']}
    return {'status': 'succeeded', 'error_code': None,
        'raw': {'entities': [{'type': 'Person', 'name': '张三', 'properties': {'duty': '检查'},
            'evidence': span, 'evidences': [span, other],
            'property_evidence': {'duty': {'value': '检查', 'support': 'llm', 'sources': [span, other]}}}],
            'relations': []},
        'verification': {'valid': True, 'coverage_complete': True, 'coverage_gaps': []},
        'run_trace': {'run_id': 'run-test', 'calls': [], 'coverage_ledger': []}}


def test_evidence_deduplicates_without_merging_equal_quotes_from_different_sources():
    result = aggregate()
    before = deepcopy(result)
    artifacts = build_artifacts(result)
    sources = [e for e in artifacts['evidence'] if e['kind'] == 'source']
    bindings = [e for e in artifacts['evidence'] if e['kind'] == 'entity_evidence']
    assert len(sources) == 2 and len(bindings) == 1
    row = artifacts['entities'][0]
    assert list(row) == ['type', 'name', 'properties']
    binding = bindings[0]
    assert (binding['type'], binding['name']) == (row['type'], row['name'])
    assert binding['property_evidence']['duty']['evidence_ids'] == binding['evidence_ids']
    assert set(binding['evidence_ids']) == {e['id'] for e in sources}
    assert {tuple(e['source_block_ids']) for e in sources} == {('c1',), ('c2',)}
    assert row['properties'] == {'duty': '检查'}
    assert binding['property_evidence']['duty']['support'] == 'llm'
    assert result == before
    assert set(artifacts) == {'entities', 'evidence', 'summary', 'messages'}


def test_partial_pipeline_exports_facts_and_terminal_failure_without_changing_strict_api(tmp_path):
    text = '张三检查。'
    line = json.dumps({**span_item('Person', '张三', text), 'properties': {}}, ensure_ascii=False)
    result = extract_harness('@startuml\nclass Person\n@enduml', [ExtractionChunk('c1', text)],
        llm=lambda messages: line + '\n{"type":', max_attempts=1, max_repair_rounds=0,
        use_scheduler=False, best_effort=True, skill_refs=[])
    with pytest.raises(ExtractionResponseError):
        to_public_result(result)
    files = export_artifacts(result, tmp_path / 'partial')
    summary = json.loads(open(files['summary.json']).read())
    assert summary['status'] == 'partial'
    assert summary['error_code'] == 'LLM_RESPONSE_INCOMPLETE'
    assert summary['attempts'][0]['terminal'] is True
    assert summary['attempts'][0]['recovery'] == 'jsonl_valid_records'
    assert summary['coverage_ledger'][0]['entity_scan_complete'] is False
    assert json.loads(open(files['entities.jsonl']).read())['name'] == '张三'
    call = json.loads(open(files['messages.jsonl']).read())
    assert call['messages'][-1] == {'role': 'assistant', 'content': line + '\n{"type":'}
    assert summary['message_trace']['complete'] is True


def test_failed_receipt_value_and_quality_are_not_relabelled_as_valid():
    result = aggregate()
    result.update(status='failed', error_code='LLM_SCHEMA_INVALID')
    result['verification'] = {'valid': False, 'coverage_complete': False, 'issues': [{'code': 'INVALID'}]}
    result['raw']['entities'][0]['properties']['duty'] = '记录'
    artifacts = build_artifacts(result)
    assert artifacts['summary']['status'] == 'failed'
    assert artifacts['summary']['verification'] == result['verification']
    binding = next(e for e in artifacts['evidence'] if e['kind'] == 'entity_evidence')
    assert binding['property_evidence']['duty']['value'] == '检查'


def test_shared_source_preserves_exact_entity_keys_and_property_bindings():
    result = aggregate()
    original = result['raw']['entities'][0]
    result['raw']['entities'] = [{**deepcopy(original), 'type': kind, 'name': name}
        for kind, name in [('Person', 'A B'), ('Person', 'AB'), ('Organization', 'AB')]]
    artifacts = build_artifacts(result)
    sources = [e for e in artifacts['evidence'] if e['kind'] == 'source']
    bindings = [e for e in artifacts['evidence'] if e['kind'] == 'entity_evidence']
    assert len(sources) == 2
    assert {(e['type'], e['name']) for e in bindings} == {('Person', 'A B'), ('Person', 'AB'), ('Organization', 'AB')}
    assert all(list(e) == ['type', 'name', 'properties'] for e in artifacts['entities'])
    assert all(e['property_evidence']['duty']['support'] == 'llm' for e in bindings)


def test_failed_receipt_without_sources_is_not_lost_in_evidence_file():
    result = aggregate()
    result['status'] = 'failed'
    entity = result['raw']['entities'][0]
    entity.pop('evidence')
    entity['evidences'] = []
    entity['property_evidence']['duty']['sources'] = []
    artifacts = build_artifacts(result)
    assert artifacts['evidence'] == [{'kind': 'entity_evidence', 'type': 'Person', 'name': '张三',
        'evidence_ids': [], 'property_evidence': {'duty': {'support': 'llm', 'evidence_ids': []}}}]
    assert artifacts['summary']['status'] == 'failed'
    assert artifacts['summary']['counts']['evidence'] == 0


def test_transport_capture_keeps_assistant_but_never_auth_headers():
    calls = [{'stage': 'entity', 'request': {'messages': [{'role': 'user', 'content': '原文'}],
        'headers': {'Authorization': 'private-auth'}, 'api_key': 'private-key'},
        'response': {'choices': [{'message': {'role': 'assistant', 'content': '{}'}, 'finish_reason': 'stop'}],
                     'headers': {'X-AGENT-TOKEN': 'private-token'}, 'usage': {'total_tokens': 10}}}]
    artifacts = build_artifacts(aggregate(), calls=calls)
    assert artifacts['messages'][0]['messages'] == [{'role': 'user', 'content': '原文'},
                                                  {'role': 'assistant', 'content': '{}'}]
    assert 'private-' not in json.dumps(artifacts)
    assert artifacts['summary']['message_trace']['complete'] is True


def test_historical_trace_without_answer_and_omitted_calls_are_explicit():
    result = aggregate()
    result['run_trace']['calls'] = [{'messages': [{'role': 'user', 'content': '原文'}]}, {'messages_omitted': True}]
    artifacts = build_artifacts(result)
    assert artifacts['summary']['message_trace'] == {
        'calls': 2, 'recorded_calls': 2, 'complete_calls': 0, 'complete': False}
    assert not any(c['response_available'] for c in artifacts['messages'])
    assert artifacts['messages'][1]['messages'] == []


def test_trace_retention_bounds_requests_and_answers_together(monkeypatch):
    from ..candidate_generation import _record_call
    monkeypatch.setenv('KG_EXTRACT_V31_TRACE_CALLS', '1')
    calls = []
    for i in range(2):
        _record_call(calls, attempt=1, pass_name='entity', chunk_id=str(i), status='succeeded',
            elapsed_seconds=0, messages=[{'role': 'user', 'content': '原文'}], response='{}')
    assert calls[0]['response_text'] == '{}'
    assert 'response_text' not in calls[1] and 'messages' not in calls[1]
    assert calls[1]['response_omitted'] is True
    assert calls[1]['output_characters'] == 2


def test_jsonl_files_have_independent_records_and_fresh_directory(tmp_path):
    destination = tmp_path / 'export'
    files = export_artifacts(aggregate(), destination)
    assert set(files) == {'entities.jsonl', 'evidence.jsonl', 'summary.json', 'messages.jsonl'}
    lines = (destination / 'evidence.jsonl').read_text().split('\n')
    assert len(lines) == 4 and lines[-1] == ''
    assert all(json.loads(line)['quote'] == '张三\n检查' for line in lines[:2])
    assert json.loads(lines[2])['kind'] == 'entity_evidence'
    entity_line = (destination / 'entities.jsonl').read_text()
    assert list(json.loads(entity_line)) == ['type', 'name', 'properties']
    assert entity_line.startswith('{"type":"Person","name":"张三","properties":')
    before = {p.name: p.read_bytes() for p in destination.iterdir()}
    with pytest.raises(FileExistsError):
        export_artifacts(aggregate(), destination)
    assert before == {p.name: p.read_bytes() for p in destination.iterdir()}
    link = tmp_path / 'link'
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(FileExistsError):
        export_artifacts(aggregate(), link)


@pytest.mark.parametrize('change', ['status', 'relations', 'nan'])
def test_invalid_export_never_creates_a_completion_marker(tmp_path, change):
    result = aggregate()
    if change == 'status':
        result.pop('status')
    elif change == 'relations':
        result['raw']['relations'] = [{'rel_type': 'unexpected'}]
    else:
        result['raw']['entities'][0]['properties']['duty'] = float('nan')
    destination = tmp_path / change
    with pytest.raises(ValueError):
        export_artifacts(result, destination)
    assert not destination.exists()
