"""Public entrypoints compile skills once without planning requests."""
import json

def policy_text(policy):
    return "\n\n".join(block["text"] for block in policy["rule_blocks"])


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
        assert set(first) == {'task', 'output_contract', 'domain_policy', 'ontology', 'documents', 'stage_context'}
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
    assert user['documents'][0]['text'] == '关于开展巡检的通知\n〔2026〕1号\n开展巡检。'
    assert policy_text(user['domain_policy']) == '按原文抽取设备业务事项，保留完整动宾短语。'
    fields = user['output_contract']['success_schema']['properties']
    assert set(fields) == {'type', 'mention', 'properties', 'evidence'}


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
    assert {u['documents'][0]['text'] for u in users} == {'开展巡检。', '关于开展巡检的通知\n〔2026〕1号'}
    assert all('route' not in u['domain_policy'] for u in users)
    assert result['profile']['prompt_assembly']['plans'][0]['route']['kind'] == 'official_document'
    assert all(policy_text(u['domain_policy']) for u in users)


def test_jsonl_retry_keeps_confirmed_skill_and_source_name():
    """Exercise the public pipeline without a stub that rewrites model output."""
    name = '关于开展 CRRC  设备巡检的通知（试行）'
    text = f'{name}文号为〔2026〕1号。开展巡检。'
    ontology = '''@startuml
class OfficialDocument {
 + 文件标题 (title): String
 + 文号 (documentNumber): String
}
@enduml'''

    class JsonlLLM:
        model = 'test'

        def __init__(self):
            self.calls = []

        def generate_structured(self, schema, messages, run_context):
            if run_context['pass_name'] == 'property_verification':
                from .stub_llm import StubLLM
                return StubLLM(verification_handler=lambda item, chunk: 'yes')(messages)
            user = json.loads(messages[1]['content'])
            self.calls.append((messages, user, run_context))
            assert run_context['response_mode'] == 'plain'
            assert user['output_contract']['format'] == 'jsonl'
            assert set(user['domain_policy']) == {'rule_blocks'}
            assert policy_text(user['domain_policy']).startswith('公文领域抽取规则：')
            assert '若文本是公文' not in policy_text(user['domain_policy'])
            assert '无需重新判断领域' in messages[0]['content']
            assert user['documents'] == [{'text': text, 'chunk_id': 'c1'}]
            if run_context['pass_name'] == 'entity':
                assert '逐字保留完整名称及内部空格、大小写、标点' in messages[0]['content']
                row = {'type': 'OfficialDocument', 'mention': name,
                       'evidence': {'quote': name}}
                if run_context['attempt'] == 1:
                    return json.dumps({'entities': [row]}, ensure_ascii=False)
                return json.dumps(row, ensure_ascii=False)
            assert run_context['pass_name'] == 'property'
            assert user['stage_context']['known_entities'][0]['name'] == name
            return json.dumps({'type': 'OfficialDocument', 'name': name,
                               'properties': {'title': name, 'documentNumber': '〔2026〕1号'},
                               'evidence': {'quote': text}}, ensure_ascii=False)

    llm = JsonlLLM()
    result = extract_harness(ontology=ontology, chunks=[ExtractionChunk('c1', text)],
                             llm=llm, use_scheduler=False, retry_base_seconds=0)
    assert result['status'] == 'succeeded'
    assert [(ctx['pass_name'], ctx['attempt']) for _, _, ctx in llm.calls] == [
        ('entity', 1), ('entity', 2), ('property', 1),
    ]
    first_messages, first_user, _ = llm.calls[0]
    retried_messages, retried_user, _ = llm.calls[1]
    assert retried_messages[0] == first_messages[0]
    assert retried_user['domain_policy'] == first_user['domain_policy']
    assert 'JSONL' in retried_user['retry_feedback']
    entity = result['raw']['entities'][0]
    assert entity['name'] == name
    assert entity['properties'] == {'title': name, 'documentNumber': '〔2026〕1号'}
    assembly = result['profile']['prompt_assembly']
    assert assembly['calls'] == []
    assert len(assembly['plans']) == 1
    assert assembly['plans'][0]['route']['kind'] == 'official_document'
    assert assembly['plans'][0]['read_skills'][0]['version'] == '5'
