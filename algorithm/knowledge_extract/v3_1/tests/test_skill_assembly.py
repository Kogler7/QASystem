"""Public entrypoints compile skills once without planning requests."""
import json

import pytest

from .. import extract_harness, extract_aggregate
from ..chunk_context import ExtractionChunk
from ..skill_registry import SkillConfigurationError
from .stub_llm import StubLLM, span_item

ONTOLOGY = '@startuml\nclass "事项" as Item {\n}\n@enduml'


class ExtractionLLM:
    model = 'test'

    def __init__(self):
        self.calls = []
        self.stub = StubLLM(entity_handler=lambda p, c: [span_item('Item', '巡检')])

    def __call__(self, messages):
        first = json.loads(messages[1]['content'])
        self.calls.append(messages)
        assert 'assembly_target' not in first, 'Skill planning must not call the LLM'
        assert set(first) == {'task', 'output_contract', 'domain_policy', 'ontology', 'documents'}
        return self.stub(messages)


def custom_skills(tmp_path):
    folder = tmp_path / 'custom'
    folder.mkdir()
    (folder / 'SKILL.md').write_text('---\nname: custom\ndescription: 设备业务\nversion: "1"\nstages: [entity, property, repair]\nroute_hints: [official_document]\n---\n\n## rules:all\n按原文抽取设备业务事项，保留完整动宾短语。\n')


@pytest.mark.parametrize('entrypoint', [extract_harness, extract_aggregate])
def test_public_entrypoint_compiles_without_planning_calls(tmp_path, entrypoint):
    custom_skills(tmp_path)
    llm = ExtractionLLM()
    result = entrypoint(ontology=ONTOLOGY, chunks=[ExtractionChunk('c1', '关于开展巡检的通知\n〔2026〕1号\n开展巡检。')], llm=llm, use_scheduler=False, skills_dir=str(tmp_path))
    assert result['profile']['prompt_assembly']['mode'] == 'deterministic_skill_compile'
    record = result['profile']['prompt_assembly']['plans'][0]
    assert record['stages']['entity'] == [{'skill_id': 'custom', 'sections': ['all']}]
    assert len(record['read_skills'][0]['sha256']) == 64
    extraction = [json.loads(m[1]['content']) for m in llm.calls if 'task' in json.loads(m[1]['content'])]
    assert extraction
    user = extraction[0]
    assert user['documents'][0]['current_chunk']['text'] == '关于开展巡检的通知\n〔2026〕1号\n开展巡检。'
    assert user['domain_policy']['profiles']['custom']['extraction_rules'] == '按原文抽取设备业务事项，保留完整动宾短语。'
    fields = user['output_contract']['success_schema']['properties']['entities']['items']['properties']
    assert set(fields) == {'type', 'mention', 'evidence'}


def test_invalid_skill_fails_before_model_call(tmp_path):
    custom_skills(tmp_path)
    (tmp_path / 'custom' / 'SKILL.md').write_text('broken frontmatter')
    llm = ExtractionLLM()
    with pytest.raises(SkillConfigurationError):
        extract_harness(ontology=ONTOLOGY, chunks=[ExtractionChunk('c1', '巡检')],
                        llm=llm, use_scheduler=False, skills_dir=str(tmp_path))
    assert llm.calls == []


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
    assert len(assembly['plans']) == 1
    assert all(record['stages']['entity'] == [] for record in assembly['plans'])


def test_public_entrypoint_missing_skill_directory_still_extracts(tmp_path):
    llm = StubLLM(entity_handler=lambda p, c: [span_item('Item', '巡检')])
    result = extract_harness(
        ontology=ONTOLOGY, chunks=[ExtractionChunk('c1', '关于开展巡检的通知\n〔2026〕1号')],
        llm=llm, use_scheduler=False, skills_dir=str(tmp_path / 'absent'),
    )
    assert result['profile']['prompt_assembly']['calls'] == []
    assert result['profile']['prompt_assembly']['plans'][0]['stages']['entity'] == []


def test_later_chunk_selects_shared_request_skill(tmp_path):
    custom_skills(tmp_path)
    llm = ExtractionLLM()
    result = extract_harness(
        ontology=ONTOLOGY,
        chunks=[ExtractionChunk('first', '开展巡检。'),
                ExtractionChunk('later', '关于开展巡检的通知\n〔2026〕1号')],
        llm=llm, use_scheduler=False, skills_dir=str(tmp_path),
    )
    assert result['profile']['prompt_assembly']['calls'] == []
    assert len(result['profile']['prompt_assembly']['plans']) == 1
    users = [json.loads(m[1]['content']) for m in llm.calls if 'task' in json.loads(m[1]['content'])]
    assert {u['documents'][0]['current_chunk']['chunk_id'] for u in users} == {'first', 'later'}
    assert all('route' not in u['domain_policy'] for u in users)
    assert result['profile']['prompt_assembly']['plans'][0]['route']['kind'] == 'official_document'
    assert all(set(u['domain_policy']['profiles']) == {'custom'} for u in users)
