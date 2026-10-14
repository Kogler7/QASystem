"""Extensions cannot change admission rules, source locality, or deadlines."""
from copy import deepcopy
import json

import pytest

from .. import candidate_generation
from ..candidate_generation import generate_structured
from ..chunk_context import ChunkContextManager, ExtractionChunk
from ..split_extractor import extract_split
from .stub_llm import StubLLM, span_item


def _request(llm, *, scheduled=False):
    messages = [{'role': 'system', 'content': 'fixed rules'},
                {'role': 'user', 'content': json.dumps({'task': {'operation': 'test'},
                                                       'documents': [{'text': 'original'}]})}]
    schema = {'type': 'object', 'properties': {'value': {'type': 'string', 'enum': ['allowed']}},
              'required': ['value'], 'additionalProperties': False}
    original = deepcopy((messages, schema))
    attempts, calls = [], []
    result = generate_structured(llm, messages, schema, use_scheduler=scheduled,
        attempts=attempts, pass_name='entity', chunk_id='c1', max_attempts=2,
        retry_base_seconds=0, call_metrics=calls)
    return result, attempts, calls, (messages, schema), original


@pytest.mark.parametrize('scheduled', [False, True])
def test_adapter_cannot_rewrite_schema_or_retry_source(scheduled):
    class Adapter:
        def __init__(self):
            self.received = []

        def generate_structured(self, schema, messages, run_context):
            self.received.append(deepcopy((schema, messages)))
            schema['properties']['value']['enum'].append('invented')
            messages[0]['content'] = 'changed rules'
            messages[1]['content'] = json.dumps({'task': {'operation': 'changed'},
                                                'documents': [{'text': 'invented'}]})
            return json.dumps({'value': 'invented' if len(self.received) == 1 else 'allowed'})

    adapter = Adapter()
    result, attempts, _, inputs, original = _request(adapter, scheduled=scheduled)
    assert result == {'value': 'allowed'}
    assert inputs == original
    assert len(adapter.received) == 2
    assert attempts[0]['type'] == 'schema'
    assert adapter.received[1][0] == original[1]
    assert adapter.received[1][1][0] == original[0][0]
    retry = json.loads(adapter.received[1][1][1]['content'])
    assert retry.pop('retry_feedback')
    assert retry == json.loads(original[0][1]['content'])


def test_plain_adapter_cannot_change_caller_messages():
    def adapter(messages):
        messages.clear()
        return '{"value":"allowed"}'
    result, _, _, inputs, original = _request(adapter)
    assert result == {'value': 'allowed'}
    assert inputs == original


@pytest.mark.parametrize('scheduled', [False, True])
def test_response_returned_after_document_deadline_is_not_accepted(monkeypatch, scheduled):
    expired = False
    calls = 0
    def adapter(messages):
        nonlocal expired, calls
        calls += 1
        expired = True
        return '{"value":"allowed"}'
    monkeypatch.setattr(candidate_generation, 'document_expired', lambda llm: expired)
    result, attempts, _, _, _ = _request(adapter, scheduled=scheduled)
    assert result is None
    assert calls == 1
    assert attempts[-1]['type'] == 'timeout'
    assert attempts[-1]['terminal'] is True
    assert attempts[-1]['retryable'] is False


@pytest.mark.parametrize('boundary', ['document', 'section'])
@pytest.mark.parametrize('direction', ['previous', 'next'])
def test_context_cannot_jump_over_a_source_boundary(boundary, direction):
    chunks = [ExtractionChunk(str(i), str(i), document_id='d1', section_id='s1') for i in range(3)]
    if boundary == 'document':
        chunks[1].document_id = 'd2'
    else:
        chunks[1].section_id = 's2'
    manager = ChunkContextManager(chunks)
    anchor = '0' if direction == 'next' else '2'
    context = manager.local_context(anchor, previous=2, next=2)
    assert [c.chunk_id for c in context] == [anchor]


def test_explicit_neighbor_cycles_do_not_duplicate_evidence():
    manager = ChunkContextManager([
        ExtractionChunk('a', 'A', previous_chunk_id='b', next_chunk_id='b'),
        ExtractionChunk('b', 'B', previous_chunk_id='a', next_chunk_id='a')])
    ids = [c.chunk_id for c in manager.local_context('a', previous=3, next=3)]
    assert len(ids) == len(set(ids)) == 2
    assert set(ids) == {'a', 'b'}


def test_section_expansion_still_respects_document_boundary():
    manager = ChunkContextManager([
        ExtractionChunk('a', 'A', document_id='d', section_id='s1'),
        ExtractionChunk('b', 'B', document_id='d', section_id='s2'),
        ExtractionChunk('c', 'C', document_id='other', section_id='s2')])
    assert [c.chunk_id for c in manager.local_context('a', next=3, same_section=False)] == ['a', 'b']


@pytest.mark.parametrize('chunk_id', [' a ', '', None, 1])
def test_invalid_source_identity_fails_at_input_boundary(chunk_id):
    with pytest.raises(ValueError, match='chunk_id'):
        ChunkContextManager([ExtractionChunk(chunk_id, 'text')])


ONTOLOGY = '@startuml\nclass Person {\n + 职责 (duty): String\n}\n\' @required Person:duty\n@enduml'


@pytest.mark.parametrize('custom', [False, True])
def test_required_property_repair_cannot_expand_to_unrelated_entities(custom):
    text = '甲负责巡检，乙负责运输。'
    rows = [{**span_item('Person', name, text), 'properties': {'duty': duty}}
            for name, duty in [('甲', '巡检'), ('乙', '运输')]]
    def repair_model(prompt, chunk):
        assert 'known_entities' in json.loads(prompt)['task']
        return {'entities': [{**{key: value for key, value in row.items() if key != 'mention'},
                              'name': row['mention']} for row in rows]}
    llm = StubLLM(entity_handler=lambda p, c: [{**span_item('Person', '甲', text), 'properties': {}}],
                  repair_handler=repair_model,
                  verification_handler=lambda item, chunk: 'yes')
    repairer = (lambda payload: {'entities': [
        {**{k: v for k, v in row.items() if k != 'mention'}, 'name': row['mention']} for row in rows]}) if custom else None
    result = extract_split(ONTOLOGY, [ExtractionChunk('c1', text)], llm=llm,
                           repairer=repairer, retry_base_seconds=0, max_attempts=1)
    assert result['status'] == ('succeeded' if custom else 'failed')
    assert [(e['name'], e['properties']) for e in result['raw']['entities']] == [('甲', {'duty': '巡检'})]
    if custom:
        assert any(w['code'] == 'REPAIR_OUT_OF_SCOPE' for w in result['warnings'])
    else:
        assert any(a['pass'] == 'repair_property' and a['terminal'] for a in result['attempts'])


@pytest.mark.parametrize('proposal', [None, {'entities': [1]}, {'entities': [{'type': 'Person'}]},
    {'entities': [{'type': 'Person', 'name': '甲', 'properties': {'unknown': '值'}, 'evidence': {'quote': '甲'}}]}])
def test_invalid_custom_repair_is_a_typed_failure(proposal):
    result = extract_split(ONTOLOGY, [ExtractionChunk('c1', '甲负责巡检。', clause_number='1')],
        llm=StubLLM(), repairer=lambda payload: proposal, retry_base_seconds=0, max_attempts=1)
    assert result['status'] == 'failed'
    assert result['error_code'] == 'LLM_SCHEMA_INVALID'
    assert any(a['pass'] == 'repair' and a['terminal'] for a in result['attempts'])


def test_custom_repair_cannot_mutate_sources_or_forge_evidence_receipts(monkeypatch):
    chunk = ExtractionChunk('c1', '甲负责巡检。', clause_number='1', metadata={'nested': {'value': 'original'}})
    observed = []
    from .. import property_grounding
    original_ground = property_grounding.ground_properties
    def ground(rows, sources, *args, **kwargs):
        observed.extend(source.metadata['nested']['value'] for source in sources)
        return original_ground(rows, sources, *args, **kwargs)
    monkeypatch.setattr(property_grounding, 'ground_properties', ground)
    def repair(payload):
        payload['chunks'][0]['metadata']['nested']['value'] = 'changed'
        return {'entities': [{'type': 'Person', 'name': '甲', 'properties': {'duty': '巡检'},
            'evidence': {'quote': chunk.text, 'source_block_ids': ['forged'], 'start_char': 999},
            'evidences': [{'quote': 'fake', 'source_block_ids': ['forged']}],
            'property_evidence': {'duty': {'support': 'llm', 'value': '巡检', 'sources': []}}}]}
    def verify(item, source):
        assert 'metadata' not in source
        assert source['text'] == chunk.text
        return 'yes'
    result = extract_split(ONTOLOGY, [chunk], llm=StubLLM(verification_handler=verify),
                           repairer=repair, retry_base_seconds=0, max_attempts=1)
    assert result['status'] == 'succeeded'
    assert observed and set(observed) == {'original'}
    entity = result['raw']['entities'][0]
    assert all(e['source_block_ids'] == ['c1'] for e in entity['evidences'])
    assert entity['property_evidence']['duty']['sources'][0]['source_block_ids'] == ['c1']


@pytest.mark.parametrize('error,code', [(RuntimeError, 'LLM_REQUEST_FAILED'), (TimeoutError, 'LLM_TIMEOUT')])
def test_callback_failure_is_typed_without_exposing_exception(error, code, caplog):
    def repair(payload):
        raise error('Authorization: secret-example-do-not-log')
    result = extract_split(ONTOLOGY, [ExtractionChunk('c1', '甲', clause_number='1')],
        llm=StubLLM(), repairer=repair, max_attempts=1, retry_base_seconds=0)
    assert result['status'] == 'failed'
    assert result['error_code'] == code
    assert 'secret-example-do-not-log' not in json.dumps(result, ensure_ascii=False) + caplog.text
