"""Prompt wiring and common-only contracts; these are not LLM quality scores."""
import json

import pytest

from .. import extract_harness
from ..chunk_context import ExtractionChunk
from ..ontology_compiler import compile_plan
from ..output_contracts import property_entity_schema
from ..prompt_router import build_chat_messages, resolve_document_route
from ..prompt_templates import FACT_EVIDENCE_RULES
from ..semantic_evidence import (VERIFICATION_DECISION_RULES, ENTITY_VERIFICATION_SYSTEM_PROMPT,
                                 build_verification_messages)
from ..skill_planner import SkillAssemblyPlanner
from ..skill_registry import SkillRegistry
from .stub_llm import StubLLM, span_item


@pytest.mark.parametrize('skill_id', ['official_document', 'rule', 'trouble'])
@pytest.mark.parametrize('stage', ['entity', 'property', 'repair_entity', 'repair_property',
                                  'verification', 'entity_verification'])
def test_shared_semantics_are_identical_and_operations_stay_in_their_stage(skill_id, stage):
    # Renamed type/field identifiers do not change the frozen skill or source.
    ontology = '@startuml\nclass Subject {\n + 说明 (statement): String\n}\n@enduml'
    plan = compile_plan(ontology)
    chunk = ExtractionChunk('internal-source', '对象甲：阶段一未通过，阶段二检查正常。')
    route = resolve_document_route(plan, [chunk])
    registry = SkillRegistry()
    ref = next(item['skill_ref'] for item in registry.selection_catalog() if item['name'] == skill_id)
    planner = SkillAssemblyPlanner(registry)
    planner.prepare(route.to_dict(), skill_refs=[ref])
    known = [{'type': 'Subject', 'name': '对象甲', 'properties': {}}]
    if stage in {'verification', 'entity_verification'}:
        item = {'item_id': 'check', 'type': 'Subject', 'name': '对象甲',
                'candidate_properties': {'statement': '阶段二检查正常'}, 'evidence_quotes': [chunk.text]}
        if stage == 'verification':
            item.update(property='statement', value='阶段二检查正常',
                        property_description='指定对象及阶段的原文说明')
        messages = build_verification_messages(chunk=chunk, route=route, assembly_planner=planner,
                                               plan=plan, items=[item])
        assert messages[0]['content'].startswith(ENTITY_VERIFICATION_SYSTEM_PROMPT) == (stage == 'entity_verification')
        applicable = {'all', 'semantics'}
    else:
        is_property = stage in {'property', 'repair_property'}
        task = 'repair' if stage.startswith('repair') else stage
        messages = build_chat_messages(task=task, plan=plan, route=route, chunk=chunk,
            assembly_planner=planner, known_entities=known if is_property else None,
            output_schema=property_entity_schema(known, plan) if is_property else plan.output_schemas['entity_candidates'],
            repair_issues=[{'code': 'MISSING', 'message': '检查指定目标。'}] if task == 'repair' else None)
        applicable = {'all', 'semantics', 'property' if is_property else 'entity'}
        if task == 'repair':
            applicable.add('repair')
    user = json.loads(messages[1]['content'])
    if skill_id == 'official_document':
        assert ref == 'official_document@13'
        assert '仅有签名或“已阅”不自动证明承担具体工作职责' in user['domain_policy']
        assert '表单中的明确批示、承办主体、事项和约束' in user['domain_policy']
        assert '角色名单能支持主体身份，但不能凭名单补写职责' in user['domain_policy']
        assert '不能把签阅流水中的姓名名单当成主送名单' in user['domain_policy']
        assert '不能用表头替代文件标题' in user['domain_policy']
    if skill_id == 'rule':
        assert ref == 'rule@10'
        assert '核验时也必须检查连接关系' in user['domain_policy']
        assert '不能把甲的恢复转给乙' in user['domain_policy']
        assert '不另补“甲导致丙”' in user['domain_policy']
    if skill_id == 'trouble':
        assert ref == 'trouble@10'
        assert '检查动作、试验项目或结果文字不自动成为固有专名' in user['domain_policy']
        assert '项目文字仍可忠实填入本体的名称属性' in user['domain_policy']
        assert '显式对象实例标签和真正固有名称逐字保留' in user['domain_policy']
        assert '不能把每个非空单元格都作为部件' in user['domain_policy']
        assert '不能一律拆型号中的连字符' in user['domain_policy']
    blocks = {}
    for section_stage in ('entity', 'property', 'repair'):
        entry = registry.read_skill(skill_id, section_stage)
        profile = registry.render([{'skill_id': skill_id, 'sections': entry['available_sections']}], section_stage)[skill_id]
        blocks.update({block['section']: block['text'] for block in profile['rule_blocks']})
    assert set(blocks) == {'all', 'semantics', 'entity', 'property', 'repair'}
    for section, body in blocks.items():
        assert user['domain_policy'].count(body) == (1 if section in applicable else 0)
    assert messages[0]['content'].count(FACT_EVIDENCE_RULES) == 1
    assert user['documents'] == chunk.text
    assert 'internal-source' not in messages[1]['content']
    assert user['output_contract']['format'] == 'jsonl'
    if stage in {'verification', 'entity_verification'}:
        assert VERIFICATION_DECISION_RULES in messages[0]['content']
        assert VERIFICATION_DECISION_RULES in user['output_contract']['constraints']
        assert set(user['output_contract']['success_schema']['properties']) == {'item_id', 'supported', 'reason'}
    else:
        fields = user['output_contract']['success_schema']['properties']
        assert fields['type']['enum'] == ['Subject']
        assert set(fields['properties']['properties']) == {'statement'}


@pytest.mark.parametrize('binding', [None, []])
@pytest.mark.parametrize('type_id', ['Stakeholder', 'RenamedActor'])
def test_sample_uses_common_prompts_and_preserves_instance_and_business_name(binding, type_id):
    """A controlled no_match decision must reach extraction without a new skill."""
    ontology = f'@startuml\nclass {type_id} {{\n + name: String\n}}\n@enduml'
    text = f'object 维护角色_B <<{type_id}>> {{\n name = "维护工程师"\n}}'
    candidate = {**span_item(type_id, '维护角色_B', text), 'properties': {'name': '维护工程师'}}
    seen = []
    stub = StubLLM(selection_handler=lambda user: {
        'status': 'no_match', 'skill_refs': [], 'reason': '本体只表达角色实例'},
        entity_handler=lambda prompt, source: [candidate], verification_handler=lambda item, source: 'yes')
    def llm(messages):
        seen.append(messages)
        return stub(messages)
    result = extract_harness(ontology, [ExtractionChunk('sample-source', text)], llm=llm,
        skill_refs=binding, use_scheduler=False, max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert len(result['raw']['entities']) == 1
    entity = result['raw']['entities'][0]
    assert (entity['type'], entity['name'], entity['properties']['name']) == (type_id, '维护角色_B', '维护工程师')
    assert result['raw']['relations'] == []
    assert result['run_trace']['prompt_version'] == 'knowledge-extract-v3.1-tokenized-recovery-v37'
    assert {item['name'] for item in SkillRegistry().selection_catalog()} == {'official_document', 'rule', 'trouble'}
    for messages in seen:
        user = json.loads(messages[1]['content'])
        assert user['domain_policy'] == ''
        if user['task']['operation'] == 'skill_selection':
            assert binding is None
            assert user['task']['allowed_types'] == [type_id]
            assert 'task.allowed_types' in messages[0]['content']
        else:
            assert FACT_EVIDENCE_RULES in messages[0]['content']
    assert sum(stage == 'skill_selection' for stage, _ in stub.calls) == (1 if binding is None else 0)


def test_official_literal_names_still_require_business_scope_admission():
    ontology = '@startuml\nclass RenamedActor\n@enduml'
    source = '甲：已阅。乙：请检查系统风险。丙：业务联系人。'
    seen = []
    def verdict(item, chunk):
        seen.append(item['name'])
        return 'no' if item['name'] == '甲' else 'yes'
    llm = StubLLM(entity_handler=lambda p, c: [span_item('RenamedActor', n, source)
        for n in ('甲', '乙', '丙')], verification_handler=verdict)
    result = extract_harness(ontology, [ExtractionChunk('c', source)], llm=llm,
        skill_refs=['official_document@13'], use_scheduler=False,
        max_attempts=1, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert set(seen) == {'甲', '乙', '丙'}
    assert {e['name'] for e in result['raw']['entities']} == {'乙', '丙'}
    assert any(a['type'] == 'candidate_rejected' and not a['terminal']
               for a in result['attempts'])
