"""Every name/type needs source-based review, independently of Skill selection."""
from copy import deepcopy
import json

import pytest

from .. import extract_harness
from ..chunk_context import ExtractionChunk, ChunkContextManager, ExtractionRequest
from ..ontology_compiler import compile_plan
from ..ontology_schema import OntologySchema
from ..verification import ExtractionVerifier


ONTOLOGY = '@startuml\nclass Person\n@enduml'


@pytest.mark.parametrize('skill_refs', [None, []])
@pytest.mark.parametrize('verdict', ['yes', 'no', 'uncertain', 'missing', 'request_failure'])
def test_literal_candidate_always_requires_entity_review(skill_refs, verdict):
    calls = []
    def llm(messages):
        user = json.loads(messages[-1]['content'])
        operation = user['task']['operation']
        calls.append(operation)
        if operation == 'skill_selection':
            return json.dumps({'status': 'no_match', 'skill_refs': [], 'reason': 'no domain'})
        if operation == 'entity':
            return json.dumps({'type': 'Person', 'mention': '2026年10月14日',
                               'properties': {}, 'evidence': {'quote': '日期：2026年10月14日。'}})
        assert operation == 'evidence_verification'
        item = user['task']['items'][0]
        assert item['type'] == 'Person'
        assert 'property' not in item
        assert '日期：2026年10月14日。' in user['documents']
        if verdict == 'request_failure':
            raise RuntimeError('test transport failure')
        if verdict == 'missing':
            return '{}'
        return json.dumps({'item_id': item['item_id'], 'supported': verdict, 'reason': 'fixture'})

    result = extract_harness(ONTOLOGY, [ExtractionChunk('c', '日期：2026年10月14日。')],
        llm=llm, skill_refs=skill_refs, max_attempts=1, max_repair_rounds=0,
        retry_base_seconds=0, use_scheduler=False, best_effort=True)
    assert 'evidence_verification' in calls
    if verdict == 'yes':
        assert result['status'] == 'succeeded'
        assert result['raw']['entities'][0]['entity_evidence']['support'] == 'llm'
    else:
        assert result['raw']['entities'] == []
        assert result['status'] == 'failed'


@pytest.mark.parametrize('corruption', ['missing', 'wrong_type', 'wrong_name', 'bad_span'])
def test_final_verifier_requires_receipt_for_literal_name(corruption):
    from .stub_llm import StubLLM, span_item
    chunk = ExtractionChunk('c', '张三到岗。')
    result = extract_harness(ONTOLOGY, [chunk], llm=StubLLM(
        entity_handler=lambda p, c: [span_item('Person', '张三')]),
        skill_refs=[], use_scheduler=False)
    rows = deepcopy(result['raw']['entities'])
    if corruption == 'missing':
        rows[0].pop('entity_evidence')
    elif corruption == 'bad_span':
        rows[0]['entity_evidence']['sources'][0]['start_char'] = 1
    else:
        rows[0]['entity_evidence']['type' if corruption == 'wrong_type' else 'name'] = '错误'
    report = ExtractionVerifier().verify(compile_plan(ONTOLOGY), OntologySchema.parse(ONTOLOGY),
        ChunkContextManager([chunk]), ExtractionRequest([chunk]), rows, [])
    assert not report.valid
    assert any(issue.code == 'ENTITY_SEMANTIC_UNSUPPORTED' for issue in report.issues)


def test_coverage_report_does_not_claim_semantic_recall():
    from .stub_llm import StubLLM, span_item
    result = extract_harness(ONTOLOGY, [ExtractionChunk('c', '张三、李四到岗。')],
        llm=StubLLM(entity_handler=lambda p, c: [span_item('Person', '张三')]),
        skill_refs=[], use_scheduler=False)
    report = result['verification']
    assert report['coverage_complete'] is True
    assert report['semantic_recall_verified'] is False
    assert report['coverage_scope'] == ['ontology_features', 'source_evidence', 'structured_chunk_binding']


def test_rejected_literal_does_not_discard_valid_peer():
    from .stub_llm import StubLLM, span_item
    text = '张三到岗，日期：2026年10月14日。'
    result = extract_harness(ONTOLOGY, [ExtractionChunk('c', text)], llm=StubLLM(
        entity_handler=lambda p, c: [span_item('Person', name, text)
                                     for name in ['张三', '2026年10月14日']],
        entity_verification_handler=lambda item, c: 'yes' if item['name'] == '张三' else 'no'),
        skill_refs=[], use_scheduler=False)
    assert result['status'] == 'succeeded'
    assert [row['name'] for row in result['raw']['entities']] == ['张三']
    assert any(a['type'] == 'candidate_rejected' for a in result['attempts'])


def test_repair_callback_cannot_admit_wrong_literal_type_or_forge_receipt():
    from .stub_llm import StubLLM
    text = '日期：2026年10月14日。'
    result = extract_harness(ONTOLOGY, [ExtractionChunk('c', text, section_id='s')],
        llm=StubLLM(entity_verification_handler=lambda item, c: 'no'),
        repairer=lambda request: {'entities': [{'type': 'Person', 'name': '2026年10月14日',
            'properties': {}, 'evidence': {'quote': text},
            'entity_evidence': {'support': 'llm', 'type': 'Person', 'name': '2026年10月14日'}}]},
        skill_refs=[], use_scheduler=False, best_effort=True, max_attempts=1)
    assert result['raw']['entities'] == []
    assert any(a['pass'] == 'entity_verification' for a in result['attempts'])
