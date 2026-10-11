"""Public extraction entrypoint uses a model-authored plan before extraction."""
import json

import pytest

from .. import extract_harness, extract_aggregate
from ..chunk_context import ExtractionChunk
from ..skill_registry import SkillConfigurationError
from .stub_llm import StubLLM, span_item

ONTOLOGY = '@startuml\nclass "事项" as Item {\n}\n@enduml'


class PlanningLLM:
    model = 'test'

    def __init__(self):
        self.calls = []
        self.stub = StubLLM(entity_handler=lambda p, c: [span_item('Item', '巡检')])

    def __call__(self, messages):
        first = json.loads(messages[1]['content'])
        self.calls.append(messages)
        if 'assembly_target' not in first:
            return self.stub(messages)
        last = json.loads(messages[-1]['content'])
        if last['requested_action'] == 'read_skills':
            return json.dumps({'action': 'read_skills', 'skill_ids': ['custom']})
        return json.dumps({'action': 'assemble', 'operation': last['operation'], 'chunk_id': last['chunk_id'],
                           'skills': [{'skill_id': 'custom', 'sections': ['all']}], 'reason': '用户维护的领域策略'})


def custom_skills(tmp_path):
    folder = tmp_path / 'custom'
    folder.mkdir()
    (folder / 'SKILL.md').write_text('---\nname: custom\ndescription: 设备业务\nversion: "1"\nstages: [entity, property, relation, repair]\nroute_hints: [rule]\n---\n\n## rules:all\n按原文抽取设备业务事项，保留完整动宾短语。\n')


@pytest.mark.parametrize('entrypoint', [extract_harness, extract_aggregate])
def test_public_entrypoint_plans_with_same_injected_runtime(tmp_path, entrypoint):
    custom_skills(tmp_path)
    llm = PlanningLLM()
    result = entrypoint(ontology=ONTOLOGY, chunks=[ExtractionChunk('c1', '前件：设备异常；后件：开展巡检。')], llm=llm, use_scheduler=False, skills_dir=str(tmp_path))
    assert result['profile']['prompt_assembly']['mode'] == 'model_skill_plan'
    record = result['profile']['prompt_assembly']['plans'][0]
    assert record['plan']['skills'] == [{'skill_id': 'custom', 'sections': ['all']}]
    assert len(record['read_skills'][0]['sha256']) == 64
    extraction = [json.loads(m[1]['content']) for m in llm.calls if 'task' in json.loads(m[1]['content'])]
    assert extraction
    user = extraction[0]
    assert user['documents'][0]['current_chunk']['text'] == '前件：设备异常；后件：开展巡检。'
    assert user['domain_policy']['profiles']['custom']['extraction_rules'] == '按原文抽取设备业务事项，保留完整动宾短语。'
    fields = user['output_contract']['success_schema']['properties']['entities']['items']['properties']
    assert set(fields) == {'type', 'mention', 'start_char', 'end_char', 'evidence'}


def test_invalid_model_plan_fails_explicitly_without_empty_extraction(tmp_path):
    custom_skills(tmp_path)
    def invalid_llm(messages):
        return json.dumps({'action': 'read_skills', 'skill_ids': ['not-registered']})
    with pytest.raises(SkillConfigurationError):
        extract_harness(ontology=ONTOLOGY, chunks=[ExtractionChunk('c1', '前件：设备异常；后件：巡检。')],
                        llm=invalid_llm, use_scheduler=False, max_attempts=1, skills_dir=str(tmp_path))


def test_explicit_planner_and_directory_cannot_conflict(tmp_path):
    from unittest.mock import Mock
    llm = Mock(model='test')
    with pytest.raises(ValueError, match='skills_dir cannot be combined'):
        extract_harness(ontology=ONTOLOGY, chunks=[], llm=llm,
                        assembly_planner=Mock(), skills_dir=str(tmp_path))
    assert llm.mock_calls == []


@pytest.mark.parametrize('first_text', ['普通正文：开展巡检。', 'object 人员 <<Stakeholder>> { 巡检 }'])
def test_public_entrypoint_keeps_empty_policy_for_all_chunks(first_text):
    llm = StubLLM(entity_handler=lambda p, c: [span_item('Item', '巡检')])
    result = extract_harness(
        ontology=ONTOLOGY,
        chunks=[ExtractionChunk('first', first_text),
                ExtractionChunk('later', '关于开展巡检的通知')],
        llm=llm, use_scheduler=False,
    )
    assembly = result['profile']['prompt_assembly']
    assert assembly['calls'] == []
    assert len(assembly['plans']) >= 2
    assert all(record['plan']['skills'] == [] for record in assembly['plans'])


def test_public_entrypoint_missing_skill_directory_still_extracts(tmp_path):
    llm = StubLLM(entity_handler=lambda p, c: [span_item('Item', '巡检')])
    result = extract_harness(
        ontology=ONTOLOGY, chunks=[ExtractionChunk('c1', '关于开展巡检的通知')],
        llm=llm, use_scheduler=False, skills_dir=str(tmp_path / 'absent'),
    )
    assert result['profile']['prompt_assembly']['calls'] == []
    assert result['profile']['prompt_assembly']['plans'][0]['plan']['skills'] == []
