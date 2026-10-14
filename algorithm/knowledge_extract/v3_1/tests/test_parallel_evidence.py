"""Concurrency, rejection and ontology regressions from the new frozen eval."""
from copy import deepcopy
import json
import threading

import pytest

from ..chunk_context import ExtractionChunk, ChunkContextManager
from ..ontology_compiler import compile_plan
from ..property_enrichment import enrich_entity_properties
from ..property_grounding import ground_properties, locate_in_sources, _verification_context
from ..prompt_router import resolve_extraction_route, resolve_document_route
from ..split_extractor import extract_split
from ..llm_client import StructuredLLMResponse
from .stub_llm import StubLLM, span_item


ONTOLOGY = '@startuml\nclass A {\n + duty: String\n}\nclass B {\n + duty: String\n}\n@enduml'


def completion_inputs(long=False):
    texts = ['甲负责检查。', '乙负责审批。']
    if long:
        texts = [text + '背景说明。' * 110 for text in texts]
    chunks = [ExtractionChunk(str(i), text, document_id=str(i)) for i, text in enumerate(texts)]
    rows = [(c.chunk_id, [{'type': typ, 'name': name, 'properties': {},
             'evidence': locate_in_sources(name, [c])}])
            for c, typ, name in zip(chunks, ['A', 'B'], ['甲', '乙'])]
    return chunks, rows


def complete(chunks, rows, llm, workers):
    attempts, warnings, metrics = [], [], []
    plan=compile_plan(ONTOLOGY)
    entities, stats = enrich_entity_properties(rows, ChunkContextManager(chunks), plan, llm,
        route=resolve_extraction_route(plan,chunks), attempts=attempts, warnings=warnings, call_metrics=metrics,
        max_workers=workers, max_attempts=1, retry_base_seconds=0,
        local_previous=0, local_next=0)
    return entities, stats, attempts, metrics


def test_independent_completion_jobs_overlap_and_equal_sequential_receipts():
    chunks, rows = completion_inputs()
    barrier = threading.Barrier(2, timeout=2)
    def response(prompt, chunk):
        known = json.loads(prompt)['task']['known_entities'][0]
        return {'entities': [{'type': known['type'], 'name': known['name'],
            'properties': {'duty': '检查' if known['name'] == '甲' else '审批'},
            'evidence': {'quote': chunk['text']}}]}
    def concurrent(prompt, chunk):
        barrier.wait()
        return response(prompt, chunk)
    parallel = complete(chunks, rows, StubLLM(property_handler=concurrent,
        verification_handler=lambda i, c: 'yes'), 2)
    sequential = complete(chunks, rows, StubLLM(property_handler=response,
        verification_handler=lambda i, c: 'yes'), 1)
    assert parallel[:3] == sequential[:3]
    assert [x['name'] for x in parallel[0]] == ['甲', '乙']
    assert not parallel[2]


def test_concurrent_failure_does_not_poison_other_keys_or_request_attempts():
    chunks, rows = completion_inputs()
    barrier = threading.Barrier(2, timeout=2)
    def llm(messages):
        user = json.loads(messages[-1]['content']);task = user['task']
        if task['operation'] == 'evidence_verification':
            return '\n'.join(json.dumps({'item_id': i['item_id'], 'supported': 'yes', 'reason': '支持'})
                             for i in task['items'])
        known = task['known_entities'][0]
        barrier.wait()
        if known['name'] == '甲':
            return StructuredLLMResponse(text='', api='chat.completions', response_mode='plain', status='incomplete', finish_reason='length')
        return json.dumps({'type': 'B', 'name': '乙', 'properties': {'duty': '审批'},
                           'evidence': {'quote': '乙负责审批。'}})
    entities, stats, attempts, metrics = complete(chunks, rows, llm, 2)
    assert entities[0]['properties'] == {}
    assert entities[1]['properties'] == {'duty': '审批'}
    assert entities[1]['property_evidence']['duty']['sources'][0]['source_block_ids'] == ['1']
    assert all(a['chunk'] == '0:property' for a in attempts if a.get('terminal'))
    assert stats['calls'] == 2


def test_concurrent_completion_keeps_each_key_window_sequence(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_WINDOW_CHARS', '256')
    chunks, rows = completion_inputs(long=True)
    lock = threading.Lock();barrier = threading.Barrier(2, timeout=2)
    active=set();seen=set();counts={}
    def response(prompt, chunk):
        known=json.loads(prompt)['task']['known_entities'][0];key=(known['type'],known['name'])
        with lock:
            assert key not in active
            active.add(key);first=key not in seen;seen.add(key);counts[key]=counts.get(key,0)+1
        if first:
            barrier.wait()
        with lock:
            active.remove(key)
        return {'entities': [{'type': known['type'], 'name': known['name'], 'properties': {},
                              'evidence': {'quote': known['name']}}]}
    entities, stats, attempts, _=complete(chunks, rows, StubLLM(property_handler=response),2)
    assert not attempts and not active
    assert all(n>1 for n in counts.values())
    assert len(entities)==2


def test_verification_batches_overlap_without_mixing_verdicts(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_VERIFICATION_BATCH_SIZE','2')
    chunk=ExtractionChunk('c','甲负责检查。乙负责审批。丙负责检查。丁负责审批。')
    rows=[{'type':'A','name':name,'properties':{'duty':'检查'},'evidence':{'quote':chunk.text}}
          for name in ['甲','乙','丙','丁']]
    barrier=threading.Barrier(2,timeout=2)
    def llm(messages):
        items=json.loads(messages[-1]['content'])['task']['items'];barrier.wait()
        return '\n'.join(json.dumps({'item_id':i['item_id'],'supported':'yes' if i['name'] in {'甲','丙'} else 'no','reason':'判断'}) for i in items)
    attempts=[];metrics=[]
    ground_properties(rows,[chunk],llm,plan=compile_plan(ONTOLOGY),attempts=attempts,warnings=[],
                      call_metrics=metrics,max_workers=2,max_attempts=1,retry_base_seconds=0)
    assert [r['properties'] for r in rows]==[{'duty':'检查'},{},{'duty':'检查'},{}]
    assert len(metrics)==2 and not attempts
    assert all(r['property_evidence']['duty']['sources'][0]['quote']==chunk.text for r in rows if r['properties'])


def test_truncated_verification_splits_only_missing_items(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_VERIFICATION_BATCH_SIZE','16')
    chunk=ExtractionChunk('c','甲负责检查。乙负责检查。')
    rows=[{'type':'A','name':name,'properties':{'duty':'检查'},'evidence':{'quote':chunk.text}} for name in ['甲','乙']]
    sent=[]
    def llm(messages):
        items=json.loads(messages[-1]['content'])['task']['items'];sent.append([i['name']for i in items])
        content=json.dumps({'item_id':items[0]['item_id'],'supported':'yes','reason':'支持'})
        if len(items)==2:
            return StructuredLLMResponse(text=content,api='chat.completions',response_mode='plain',status='incomplete',finish_reason='length')
        return content
    attempts=[]
    ground_properties(rows,[chunk],llm,plan=compile_plan(ONTOLOGY),attempts=attempts,warnings=[],max_attempts=1,retry_base_seconds=0)
    assert sent==[['甲','乙'],['乙']]
    assert all(r['properties']=={'duty':'检查'} for r in rows)
    assert not any(a.get('terminal')for a in attempts)


@pytest.mark.parametrize('verdict,expected',[('no','succeeded'),('uncertain','partial')])
def test_rejected_candidate_and_unresolved_verdict_have_different_status(verdict,expected):
    ontology='@startuml\nclass OfficialDocument\n@enduml'
    llm=StubLLM(entity_handler=lambda p,c:[span_item('OfficialDocument',name)for name in ['巡检通知','已阅']],
        verification_handler=lambda i,c:'yes' if i['name']=='巡检通知' else verdict)
    result=extract_split(ontology,[ExtractionChunk('c','巡检通知。已阅。')],llm=llm,best_effort=True,max_attempts=1,max_repair_rounds=0)
    assert result['status']==expected
    assert [e['name']for e in result['raw']['entities']]==['巡检通知']
    assert result['profile']['candidate_selection']['rejected_entities']==(1 if verdict=='no' else 0)


def test_source_excerpt_merge_keeps_exact_union_and_coordinates():
    chunk=ExtractionChunk('c','前言。甲负责检查。乙负责审批。尾部。'+'背景。'*200)
    rows=[]
    for name in ['甲','乙']:
        row={'type':'A','name':name};span=locate_in_sources(name,[chunk])
        rows.append((row,'duty','检查',[span],{}))
    parts=_verification_context(rows,[chunk])
    assert len(parts)==1
    part=parts[0];offset=part.metadata['_v31_window_start']
    assert part.text==chunk.text[offset:offset+len(part.text)]
    assert '甲负责检查' in part.text and '乙负责审批' in part.text


def test_class_note_and_rule_family_reach_generation_and_verification():
    ontology='''@startuml
class "规则" <<RuleType>> {
 + antecedent: String
 + consequent: String
}
note right of 规则
规则定义：独立前提引起独立结果；分类不是规则身份。
class ForgedType
end note
@enduml'''
    plan=compile_plan(ontology)
    assert set(plan.allowed_types)=={'RuleType'}
    assert '独立前提' in plan.class_specs['RuleType'].description
    chunks=[ExtractionChunk('c','继电器失效导致断电；复位后恢复供电。')]
    assert resolve_document_route(plan,chunks).kind=='generic'
    assert resolve_extraction_route(plan,chunks).kind=='rule'
    llm=StubLLM(entity_handler=lambda p,c:[span_item('RuleType','继电器失效导致断电')],verification_handler=lambda i,c:'yes')
    result=extract_split(ontology,chunks,llm=llm,max_attempts=1,max_repair_rounds=0)
    assert result['status']=='succeeded'
    for task,prompt in llm.calls:
        user=json.loads(prompt)
        assert '分类不是规则身份' in user['ontology']
        if task == 'skill_selection':
            continue
        assert user['domain_policy']
        if task == 'entity':
            assert '故障详细分类' in user['domain_policy']
        if task == 'evidence_verification':
            assert '仅为有证据支持的 missing property 输出更新' not in user['domain_policy']
    assert result['profile']['document_route']['kind']=='generic'
    assert result['profile']['prompt_assembly']['plans'][0]['selection']['skill_refs']==['rule@10']


def test_partial_v31_never_publishable_when_structure_is_valid():
    from backend.app.services.knowledge_extract_adapter import project_items
    result={'extract_version':'v3.1','status':'partial','verification':{'valid':True},
            'graph':{'nodes':[],'relationships':[]},'attempts':[]}
    assert project_items(result,texts=['原文'])[0]['quality']['graph_publishable'] is False


@pytest.mark.parametrize('configured,size', [(None, 4), ('2', 2)])
def test_completion_batches_entities_and_overlap(monkeypatch, configured, size):
    if configured is not None:
        from .. import property_enrichment
        monkeypatch.setattr(property_enrichment, 'PROPERTY_BATCH_SIZE', int(configured))
    names = list('甲乙丙丁戊己庚辛')[:size * 2]
    chunk = ExtractionChunk('c', ''.join(f'{name}负责检查。' for name in names))
    rows = [('c', [{'type': 'A', 'name': name, 'properties': {},
                    'evidence': locate_in_sources(name, [chunk])} for name in names])]
    barrier = threading.Barrier(2, timeout=3)
    batches = []
    def response(prompt, source):
        known = json.loads(prompt)['task']['known_entities']
        batches.append([row['name'] for row in known])
        barrier.wait()
        return {'entities': [{'type': row['type'], 'name': row['name'],
            'properties': {'duty': '检查'}, 'evidence': {'quote': f"{row['name']}负责检查。"}}
            for row in known]}
    entities, stats, attempts, _ = complete([chunk], rows,
        StubLLM(property_handler=response, verification_handler=lambda i, c: 'yes'), 2)
    assert sorted(map(len, batches)) == [size, size]
    assert sorted(name for batch in batches for name in batch) == sorted(names)
    assert [row['name'] for row in entities] == names
    assert all(row['properties'] == {'duty': '检查'} for row in entities)
    assert stats['calls'] == 2 and not attempts


@pytest.mark.parametrize('configured,sizes', [(None, [1, 8]), ('3', [3, 3, 3])])
def test_property_verification_batches_preserve_receipts(monkeypatch, configured, sizes):
    if configured is not None:
        from .. import property_grounding
        monkeypatch.setattr(property_grounding, 'PROPERTY_VERIFICATION_BATCH_SIZE', int(configured))
    monkeypatch.setenv('KG_EXTRACT_V31_VERIFICATION_BATCH_SIZE', '16')
    names = list('甲乙丙丁戊己庚辛壬')
    chunk = ExtractionChunk('c', ''.join(f'{name}负责检查。' for name in names))
    rows = [{'type': 'A', 'name': name, 'properties': {'duty': '检查'},
             'evidence': {'quote': f'{name}负责检查。'}} for name in names]
    batches = []
    def llm(messages):
        items = json.loads(messages[-1]['content'])['task']['items']
        batches.append([item['name'] for item in items])
        return '\n'.join(json.dumps({'item_id': item['item_id'], 'supported': 'yes', 'reason': '支持'}) for item in items)
    attempts = []
    ground_properties(rows, [chunk], llm, plan=compile_plan(ONTOLOGY), attempts=attempts,
        warnings=[], max_workers=2, max_attempts=1, retry_base_seconds=0)
    assert sorted(map(len, batches)) == sizes
    assert all(row['properties'] == {'duty': '检查'} for row in rows)
    assert all(row['property_evidence']['duty']['sources'][0]['quote'] == f"{row['name']}负责检查。" for row in rows)
    assert not attempts
