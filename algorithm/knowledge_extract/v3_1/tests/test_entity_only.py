"""V3.1 removes every relationship-producing path while retaining entities."""
import json

import pytest

from .. import extract_harness, extract
from ..chunk_context import ExtractionChunk, ChunkContextManager
from ..ontology_schema import OntologySchema
from ..ontology_compiler import compile_plan
from ..prompt_router import build_chat_messages, resolve_document_route
from ..skill_planner import SkillAssemblyPlanner
from .stub_llm import StubLLM, span_item

ONTOLOGY = '''@startuml
class Person {
  + 职责 (duty): String
}
class Work
Person "1" --> "1..*" Work : performs
@enduml'''


class EntityPlanner(SkillAssemblyPlanner):
    def __init__(self):
        super().__init__()
        self.stages = []

    def assemble(self, user, *, stage):
        assert stage in {'entity', 'property', 'repair'}
        assert 'relation_pairs' not in user['task']
        assert 'relation_signatures' not in user['ontology']
        self.stages.append(stage)
        return super().assemble(user, stage=stage)


def test_many_entities_with_required_relations_finish_after_properties():
    # 30 x 30 legal endpoint combinations previously required relation batching.
    names = [f'人员{i:02}' for i in range(30)] + [f'任务{i:02}' for i in range(30)]
    text = '\n\n'.join(f'{n}负责巡检。' if n.startswith('人员') else f'{n}需要执行。' for n in names)
    planner = EntityPlanner()
    def properties(prompt, chunk):
        known = json.loads(prompt)["task"]["known_entities"]
        return {'entities': [{'type': e['type'], 'name': e['name'], 'properties': {'duty': '巡检'} if e['type']=='Person' else {},
                             'evidence': {'quote': e['name'] + '负责巡检。' if e['type']=='Person' else e['name']}} for e in known]}
    llm = StubLLM(entity_handler=lambda p, c: [span_item('Person' if n.startswith('人员') else 'Work', n) for n in names],
                  property_handler=properties)
    result = extract_harness(ONTOLOGY, [ExtractionChunk('c0', text)], llm=llm,
                             assembly_planner=planner, use_scheduler=False)
    assert result['status'] == 'succeeded'
    assert len(result['raw']['entities']) == 60
    assert all(e['properties']['duty'] == '巡检' for e in result['raw']['entities'] if e['type'] == 'Person')
    assert result['raw']['relations'] == result['graph']['relationships'] == result['triples'] == []
    assert result['intermediate_model']['relations'] == []
    assert result['mapping']['relations'] == []
    assert result['profile']['pipeline'] == 'entity_property'
    assert {task for task, _ in llm.calls} == {'entity', 'property'}
    assert set(planner.stages) == {'entity', 'property'}
    assert result['profile']['repair_rounds'] == []
    assert not any(g['gap_type'] in {'relation_coverage', 'composite_membership'} for g in result['coverage_gaps'])


def test_public_result_has_empty_relations():
    llm = StubLLM(entity_handler=lambda p, c: [span_item('Work', '巡检')])
    result = extract([ExtractionChunk('c0', '巡检')], ONTOLOGY, llm=llm,
                     assembly_planner=EntityPlanner(), use_scheduler=False)
    assert result['entities']
    assert result['relations'] == []


def test_property_response_cannot_add_relationships():
    llm = StubLLM(entity_handler=lambda p, c: [span_item('Person', '巡检')],
                  property_handler=lambda p, c: {'entities': [], 'relations': [{}]})
    result = extract_harness(ONTOLOGY, [ExtractionChunk('c0', '巡检')], llm=llm,
                             use_scheduler=False, max_attempts=1)
    assert result['error_code'] == 'LLM_SCHEMA_INVALID'
    assert result['raw']['relations'] == []


def test_relation_prompt_rejected_before_planning():
    plan = compile_plan(ONTOLOGY)
    chunk = ExtractionChunk('c0', '甲负责巡检')
    planner = EntityPlanner()
    with pytest.raises(ValueError, match='Unsupported'):
        build_chat_messages(task='relation', plan=plan, route=resolve_document_route(plan, [chunk]),
                            chunk=chunk, assembly_planner=planner)
    assert planner.stages == []
    assert 'relation_candidates' not in plan.output_schemas


def test_relation_skill_stage_is_rejected_without_model_calls():
    def unexpected(*args):
        pytest.fail('relation planning must not call a model')
    with pytest.raises(ValueError, match='Unsupported'):
        SkillAssemblyPlanner().assemble({}, stage='relation')


def test_entity_repair_still_runs_and_discards_proposed_edges():
    seen = []
    def repair(payload):
        seen.append(payload)
        return {'entities': [{'type': 'Work', 'name': '巡检', 'properties': {},
                              'evidence': {'quote': '巡检'}}],
                'relations': [{'from_name': '巡检', 'to_name': '巡检', 'rel_type': 'performs'}]}
    result = extract_harness(ONTOLOGY, [ExtractionChunk('c0', '巡检', clause_number='1')],
                             llm=StubLLM(), assembly_planner=EntityPlanner(),
                             repairer=repair, use_scheduler=False, retry_base_seconds=0)
    assert seen
    assert any(e['name'] == '巡检' for e in result['raw']['entities'])
    assert result['raw']['relations'] == []
    assert result['verification']['valid']
    assert result['status'] == 'succeeded'
