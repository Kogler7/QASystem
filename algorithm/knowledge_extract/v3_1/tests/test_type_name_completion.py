"""Cross-chunk identity routing leaves all property synthesis to the model."""
import json

import pytest

from ..chunk_context import ChunkContextManager, ExtractionChunk
from ..ontology_compiler import compile_plan
from ..prompt_router import resolve_document_route
from ..property_enrichment import enrich_entity_properties
from .stub_llm import StubLLM

ONTOLOGY = '''@startuml
class Person {
 + 职责 (duty): String
}
class Work {
 + 说明 (description): String
}
@enduml'''


def complete(handler, *, batch_size=24):
    chunks = [ExtractionChunk('a', '巡检：人员负责检查。'), ExtractionChunk('b', '巡检：人员负责记录。')]
    plan = compile_plan(ONTOLOGY)
    llm = StubLLM(property_handler=handler, verification_handler=lambda item, chunk: "yes" if item["value"] == "检查并记录" else "no")
    attempts = []
    rows, stats = enrich_entity_properties([
        ('a', [{'type': 'Person', 'name': '巡检', 'properties': {'duty': '检查'}, 'evidence': {'quote': chunks[0].text}},
               {'type': 'Work', 'name': '巡检', 'properties': {}}]),
        ('b', [{'type': 'Person', 'name': '巡检', 'properties': {'duty': '记录'}, 'evidence': {'quote': chunks[1].text}}]),
    ], ChunkContextManager(chunks), plan, llm, route=resolve_document_route(plan, chunks),
       attempts=attempts, warnings=[], batch_size=batch_size, max_attempts=1)
    return rows, attempts, llm


def test_key_group_never_split_and_model_owns_consolidated_properties():
    def handler(prompt, chunk):
        payload = json.loads(prompt)
        known = payload['task']['known_entities']
        assert len(known) == 1
        entity = known[0]
        assert 'id' not in entity
        if entity['type'] == 'Person':
            assert [d['text'] for d in payload['documents']] == ['巡检：人员负责检查。', '巡检：人员负责记录。']
            assert [o['properties'] for o in entity['occurrences']] == [{'duty': '检查'}, {'duty': '记录'}]
            properties = {'duty': '检查并记录'}
        else:
            properties = {'description': '巡检'}
        return {'entities': [{'type': entity['type'], 'name': entity['name'],
                              'properties': properties, 'evidence': {'quote': '巡检：人员负责检查。', 'property_quotes': {'duty': ['巡检：人员负责检查。', '巡检：人员负责记录。']} if entity['type'] == 'Person' else {}}}]}
    rows, attempts, llm = complete(handler)
    assert not attempts
    assert len([t for t, _ in llm.calls if t == "property"]) == 2
    assert [(r['type'], r['name']) for r in rows] == [('Person', '巡检'), ('Work', '巡检')]
    assert rows[0]['properties'] == {'duty': '检查并记录'}
    assert rows[1]['properties'] == {'description': '巡检'}
    assert all('id' not in r for r in rows)


@pytest.mark.parametrize('bad', ['duplicate', 'omit', 'wrong_property'])
def test_rejects_invalid_model_consolidation(bad):
    def handler(prompt, chunk):
        entities = []
        for e in json.loads(prompt)['task']['known_entities']:
            entities.append({'type': e['type'], 'name': e['name'], 'properties': {}, 'evidence': {'quote': '巡检'}})
        if bad == 'duplicate':
            entities.append(dict(entities[0]))
        elif bad == 'omit':
            entities.pop()
        else:
            entities[0]['properties'] = {'unknown': '其他类型属性'}
        return {'entities': entities}
    rows, attempts, _ = complete(handler, batch_size=24)
    assert len(rows) == 2
    assert rows[0]["properties"] == {}  # conflicting values cannot be chosen by Python
    assert rows[1]["properties"] == {}
    assert any(a.get('terminal') for a in attempts)


def test_type_specific_schema_and_typed_arrays():
    from ..output_contracts import property_entity_schema
    from ..structured_output import validate_json_schema
    plan = compile_plan('''@startuml
class A {
 + 值 (value): Integer
 + 数组 (values): Integer[]
}
class B {
 + 值 (value): Boolean
}
@enduml''')
    for type_id, value in [('A', 3), ('B', True)]:
        schema = property_entity_schema([{'type': type_id, 'name': '同名'}], plan)
        properties = {'value': value}
        if type_id == 'A':
            properties['values'] = [1, 2]
        row = {'type': type_id, 'name': '同名', 'properties': properties, 'evidence': {'quote': '同名'}}
        assert not validate_json_schema({'entities': [row]}, schema)
        row['properties']['value'] = 'wrong'
        assert validate_json_schema({'entities': [row]}, schema)
    with pytest.raises(ValueError, match='one entity type'):
        property_entity_schema([{'type': 'A', 'name': '同名'}, {'type': 'B', 'name': '同名'}], plan)


def test_contract_rejects_old_updates_and_wrong_stage():
    from ..output_contracts import build_output_contract, property_entity_schema
    plan = compile_plan(ONTOLOGY)
    with pytest.raises(ValueError, match='does not match'):
        build_output_contract({'type': 'object', 'properties': {'updates': {'type': 'array'}}},
                              entity_task=False, task='property')
    with pytest.raises(ValueError, match='does not match'):
        build_output_contract(plan.output_schemas['entity_candidates'], entity_task=False, task='property')
    schema = property_entity_schema([{'type': 'Person', 'name': '甲'}], plan)
    contract = build_output_contract(schema, entity_task=False, task='property')
    assert '不能返回空结果' in contract['constraints']
    assert '无结果时使用' not in contract['constraints']


@pytest.mark.parametrize('text,mention,quote,ambiguous', [
    ('甲负责检查。', '甲', '甲负责检查', False),
    ('甲负责检查。乙负责检查。', '检查安排', '乙负责检查', False),
    ('甲负责检查。甲负责检查。', '甲', '甲负责检查', True),
])
def test_quote_grounding_without_model_offsets(text, mention, quote, ambiguous):
    from ..candidate_generation import _normalize_span_entities, bind_entity_evidence
    from ..ontology_schema import OntologySchema
    chunk = ExtractionChunk('c', text)
    warnings = []
    rows = _normalize_span_entities({'entities': [{'type': 'Person', 'mention': mention,
        'evidence': {'quote': quote}}]}, chunk=chunk, schema=OntologySchema.parse(ONTOLOGY),
        target_types=None, warnings=warnings)
    assert set(rows[0]['evidence']) == {'quote'}
    bound = bind_entity_evidence(rows, ChunkContextManager([chunk]))[0]['evidence']
    assert text[bound['start_char']:bound['end_char']] == quote
    assert ('ENTITY_EVIDENCE_AMBIGUOUS' in {w['code'] for w in warnings}) == ambiguous
    assert 'ENTITY_SPAN_OFFSET_CORRECTED' not in {w['code'] for w in warnings}


def test_unlocated_quote_does_not_manufacture_offsets():
    from ..candidate_generation import _normalize_span_entities, bind_entity_evidence
    from ..ontology_schema import OntologySchema
    chunk = ExtractionChunk('c', '甲负责检查。')
    rows = _normalize_span_entities({'entities': [{'type': 'Person', 'mention': '不存在',
        'evidence': {'quote': '没有此原文'}}]}, chunk=chunk, schema=OntologySchema.parse(ONTOLOGY),
        target_types=None, warnings=[])
    evidence = bind_entity_evidence(rows, ChunkContextManager([chunk]))[0]['evidence']
    assert evidence == {'quote': '没有此原文'}
