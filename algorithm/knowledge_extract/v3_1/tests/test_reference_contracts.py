"""Regression contracts adapted from the reference runtimes, without their SDKs."""
from copy import deepcopy
from contextlib import contextmanager
import json
import time

import pytest

from .. import candidate_generation as generation
from ..chunk_context import ChunkContextManager, ExtractionChunk, ExtractionRequest
from ..ontology_compiler import compile_plan
from ..ontology_schema import OntologySchema
from ..property_grounding import ground_properties
from ..split_extractor import extract_split, _repair_context_chunks
from ..structured_output import validate_json_schema
from ..verification import ExtractionVerifier
from .stub_llm import StubLLM, span_item


ONTOLOGY = '@startuml\nclass Person {\n + 出生地 (birthplace): String\n}\n@enduml'


def test_literal_cooccurrence_does_not_prove_property_meaning():
    text = '张三出生于北京并在上海工作。'
    llm = StubLLM(entity_handler=lambda p, c: [{**span_item('Person', '张三', text),
        'properties': {'birthplace': '上海'}}], verification_handler=lambda item, chunk: 'no',
        entity_verification_handler=lambda item, chunk: 'yes')
    result = extract_split(ONTOLOGY, [ExtractionChunk('c', text)], llm=llm)
    assert result['raw']['entities'][0]['properties'] == {}
    assert any(task == 'evidence_verification' for task, _ in llm.calls)


def test_model_cannot_publish_entity_without_any_source():
    def llm(messages):
        return json.dumps({'type': 'Person', 'mention': '虚构人物', 'properties': {},
                           'evidence': {'quote': '虚构来源'}}, ensure_ascii=False)
    result = extract_split(ONTOLOGY, [ExtractionChunk('c', '原文没有任何人物。')],
                           llm=llm, max_attempts=1, max_repair_rounds=0)
    assert result['raw']['entities'] == []
    assert result['status'] == 'failed'
    assert result['error_code'] == 'LLM_SCHEMA_INVALID'


def test_property_verification_uses_bounded_source_excerpts(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_MAX_INPUT_BYTES', '8000')
    quote = '张三的出生地点为北京市。'
    text = '无关背景。' * 10000 + quote
    rows = [{'type': 'Person', 'name': '张三', 'properties': {'birthplace': '北京'},
             'evidence': {'quote': quote}}]
    llm = StubLLM(verification_handler=lambda item, chunk: 'yes')
    attempts = []
    result = ground_properties(rows, [ExtractionChunk('c', text)], llm,
        attempts=attempts, warnings=[], plan=compile_plan(ONTOLOGY), max_attempts=1)
    assert result[0]['properties'] == {'birthplace': '北京'}
    assert not attempts
    assert len(llm.calls) == 1
    assert len(llm.calls[0][1]) < 8000
    span = result[0]['property_evidence']['birthplace']['sources'][0]
    assert text[span['start_char']:span['end_char']] == quote


def test_equal_values_keep_all_property_receipts():
    chunks = [ExtractionChunk(cid, '张三出生于北京。') for cid in ('a', 'b')]
    llm = StubLLM(entity_handler=lambda p, c: [{**span_item('Person', '张三', c['text']),
        'properties': {'birthplace': '北京'}}], verification_handler=lambda item, chunk: 'yes')
    result = extract_split(ONTOLOGY, chunks, llm=llm)
    spans = result['raw']['entities'][0]['property_evidence']['birthplace']['sources']
    assert {span['source_block_ids'][0] for span in spans} == {'a', 'b'}


def test_final_verifier_rejects_relocated_property_receipt():
    chunk = ExtractionChunk('c', '张三出生于北京。')
    rows = [{'type': 'Person', 'name': '张三', 'properties': {'birthplace': '北京'},
             'evidence': {'quote': chunk.text}}]
    plan = compile_plan(ONTOLOGY)
    rows = ground_properties(rows, [chunk], StubLLM(verification_handler=lambda i, c: 'yes'),
                             attempts=[], warnings=[], plan=plan)
    rows[0]['property_evidence']['birthplace']['sources'][0]['start_char'] = 1
    report = ExtractionVerifier().verify(plan, OntologySchema.parse(ONTOLOGY),
        ChunkContextManager([chunk]), ExtractionRequest([chunk]), rows, [])
    assert not report.valid
    assert any(issue.code == 'PROPERTY_EVIDENCE_INVALID' for issue in report.issues)


@pytest.mark.parametrize('target', ['primary', 'all_sources'])
def test_final_verifier_checks_primary_and_all_entity_coordinates(target):
    chunk = ExtractionChunk('c', '张三出生于北京。')
    plan = compile_plan(ONTOLOGY)
    rows = ground_properties([{'type': 'Person', 'name': '张三', 'properties': {},
        'evidence': {'quote': chunk.text}}], [chunk], StubLLM(), attempts=[], warnings=[])
    rows[0]['evidence'] = deepcopy(rows[0]['evidence'])
    span = rows[0]['evidence'] if target == 'primary' else rows[0]['evidences'][0]
    span['start_char'] = 1
    report = ExtractionVerifier().verify(plan, OntologySchema.parse(ONTOLOGY),
        ChunkContextManager([chunk]), ExtractionRequest([chunk]), rows, [])
    assert not report.valid
    assert any(issue.code == 'EVIDENCE_INVALID' for issue in report.issues)


def test_repair_context_does_not_silently_drop_fourth_source():
    chunks = [ExtractionChunk(f'c{i}', f'原文{i}') for i in range(6)]
    issues = [{'path': c.chunk_id, 'context': {'chunk_ids': [c.chunk_id]}} for c in chunks]
    selected = _repair_context_chunks(issues, ChunkContextManager(chunks), entities=[], relations=[])
    assert [c.chunk_id for c in selected] == [c.chunk_id for c in chunks]


def test_unfinished_repair_is_not_reported_as_validation_complete():
    llm = StubLLM(repair_handler=lambda p, c: {'entities': []})
    result = extract_split(ONTOLOGY, [ExtractionChunk('c', '这是结构章节。', section_id='s')], llm=llm)
    assert result['profile']['repair_stop_reason'] == 'no_progress'
    assert result['run_trace']['stop_reason'] == 'no_progress'
    assert result['verification']['coverage_complete'] is False


def test_deadline_is_rechecked_after_scheduler_wait(monkeypatch):
    llm = StubLLM()
    llm.deadline = float('inf')

    class Scheduler:
        @contextmanager
        def request_slot(self, *, deadline=None):
            llm.deadline = 0
            yield

    monkeypatch.setattr(generation, 'get_llm_scheduler', lambda: Scheduler())
    attempts = []
    result = generation.generate_structured(llm, [{'role': 'system', 'content': 'x'},
        {'role': 'user', 'content': '{}'}], {'type': 'object'}, use_scheduler=True,
        attempts=attempts, pass_name='entity', chunk_id='c', max_attempts=1, retry_base_seconds=0)
    assert result is None
    assert not llm.calls
    assert attempts[-1]['type'] == 'timeout'
    assert attempts[-1]['retryable'] is False


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_json_numbers_cannot_enter_facts(value):
    assert validate_json_schema(value, {'type': 'number'})


def test_target_type_generator_is_materialized_once():
    ontology = '@startuml\nclass Person\nclass Place\n@enduml'
    llm = StubLLM(entity_handler=lambda p, c: [span_item('Person', '张三')])
    result = extract_split(ontology, [ExtractionChunk('c', '张三')], llm=llm,
                           target_types=(item for item in ['Person']))
    assert result['status'] == 'succeeded'
    assert [e['type'] for e in result['raw']['entities']] == ['Person']


def test_input_chunks_are_snapshotted_before_model_calls():
    chunk = ExtractionChunk('c', '张三出生于北京。', metadata={'source': {'revision': 1}})
    original = deepcopy(chunk)

    def entities(prompt, current):
        chunk.text = '调用方修改了原文'
        chunk.metadata['source']['revision'] = 2
        return [{**span_item('Person', '张三', current['text']), 'properties': {}}]

    result = extract_split(ONTOLOGY, [chunk], llm=StubLLM(entity_handler=entities))
    span = result['raw']['entities'][0]['evidence']
    assert span['quote'] == original.text
    assert result['verification']['valid'] is True
    assert not any(i['code'] == 'EVIDENCE_INVALID' for i in result['verification']['issues'])


def test_provider_exception_does_not_leak_credentials(caplog):
    secret = 'test-only-sensitive-provider-token'

    def llm(messages):
        raise RuntimeError(f'Authorization: Bearer {secret}; request body contains private input')

    result = extract_split(ONTOLOGY, [ExtractionChunk('c', '张三')], llm=llm, max_attempts=1)
    assert result['error_code'] == 'LLM_REQUEST_FAILED'
    assert secret not in json.dumps(result, ensure_ascii=False)
    assert secret not in caplog.text
    assert 'private input' not in caplog.text


def test_scheduler_deadline_bounds_semaphore_and_rate_waits():
    from ..scheduler import LLMRateLimits, LLMRequestScheduler
    scheduler = LLMRequestScheduler(LLMRateLimits(max_concurrency=1, qpm=1))
    with scheduler.request_slot():
        with pytest.raises(TimeoutError):
            with scheduler.request_slot(deadline=time.monotonic() + 0.01):
                pytest.fail('a second caller must not own the busy slot')
    with pytest.raises(TimeoutError):
        with scheduler.request_slot(deadline=time.monotonic() + 0.01):
            pytest.fail('rate limited call must honor its deadline')
    # Deadline failure must release its acquired semaphore.
    assert scheduler._semaphore.acquire(blocking=False)
    scheduler._semaphore.release()


def test_unresolved_property_conflict_is_explicit_partial_result():
    chunks = [ExtractionChunk('a', '张三出生于北京。'), ExtractionChunk('b', '张三出生于上海。')]
    llm = StubLLM(entity_handler=lambda p, c: [{**span_item('Person', '张三', c['text']),
        'properties': {'birthplace': '北京' if '北京' in c['text'] else '上海'}}],
        verification_handler=lambda item, chunk: 'yes',
        property_handler=lambda p, c: {'entities': [{'type': 'Person', 'name': '张三',
            'properties': {}, 'evidence': {'quote': c['text']}}]})
    result = extract_split(ONTOLOGY, chunks, llm=llm, best_effort=True)
    assert result['status'] == 'partial'
    assert result['raw']['entities'][0]['properties'] == {}
    assert any(w['code'] == 'PROPERTY_CONFLICT_UNRESOLVED' for w in result['warnings'])


def test_repair_visits_every_referenced_chunk():
    chunks = [ExtractionChunk(str(i), f'人员{i}已登记。', section_id=f's{i}') for i in range(5)]
    llm = StubLLM(repair_handler=lambda p, c: {'entities': [
        {**span_item('Person', c['text'].split('已')[0], c['text']), 'properties': {}}]})
    result = extract_split(ONTOLOGY, chunks, llm=llm)
    assert len(result['raw']['entities']) == 5
    assert result['profile']['repair_stop_reason'] == 'validation_complete'


def test_changed_value_does_not_inherit_receipts_for_old_value():
    before = ExtractionChunk('before', '张三原在北京工作。')
    after = ExtractionChunk('after', '张三已调至上海工作。')
    llm = StubLLM(verification_handler=lambda i, c: 'yes')
    prior = ground_properties([{'type': 'Person', 'name': '张三',
        'properties': {'location': '北京'}, 'evidence': {'quote': before.text}}],
        [before], llm, attempts=[], warnings=[])[0]
    current = ground_properties([{'type': 'Person', 'name': '张三',
        'properties': {'location': '上海'}, 'evidence': {'quote': after.text}}],
        [after], llm, attempts=[], warnings=[], prior=prior)[0]
    assert [s['source_block_ids'] for s in current['property_evidence']['location']['sources']] == [['after']]


def test_hard_verification_failure_has_truthful_stop_reason(monkeypatch):
    from dataclasses import replace
    from ..verification import VerificationIssue
    original = ExtractionVerifier.verify

    def invalid(self, *args, **kwargs):
        report = original(self, *args, **kwargs)
        return replace(report, issues=report.issues + (
            VerificationIssue('EVIDENCE_INVALID', 'invalid evidence'),))

    monkeypatch.setattr(ExtractionVerifier, 'verify', invalid)
    result = extract_split(ONTOLOGY, [ExtractionChunk('c', '张三')],
        llm=StubLLM(entity_handler=lambda p, c: [span_item('Person', '张三')]),
        max_repair_rounds=0, best_effort=True)
    assert result['status'] == 'partial'
    assert result['verification']['valid'] is False
    assert result['run_trace']['stop_reason'] == 'best_effort_partial'
    assert result['run_trace']['repair_stop_reason'] == 'verification_failed'
    assert result['profile']['repair_stop_reason'] == 'verification_failed'


@pytest.mark.parametrize('value,schema', [
    ('abcd', {'type': 'string', 'maxLength': 3}),
    ([1, 2], {'type': 'array', 'maxItems': 1}),
])
def test_structured_output_respects_declared_size_bounds(value, schema):
    assert validate_json_schema(value, schema)
