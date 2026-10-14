"""Entity prompt scope and evidence wiring; fixtures do not measure LLM accuracy."""
from copy import deepcopy
import json

import pytest

from ..chunk_context import ExtractionChunk
from ..ontology_compiler import compile_plan
from ..property_grounding import admit_entity_names
from ..prompt_templates import FACT_EVIDENCE_RULES, MESSAGE_BOUNDARY_RULES
from ..semantic_evidence import (ENTITY_VERIFICATION_SYSTEM_PROMPT, VERIFICATION_SYSTEM_PROMPT,
                                 build_verification_messages)


ENTITY = {'item_id': 'e', 'type': 'Subject', 'name': '对象甲'}
PROPERTY = {**ENTITY, 'item_id': 'p', 'property': 'state', 'value': '正常'}


@pytest.mark.parametrize('items,entity_only', [
    ([ENTITY], True),
    ([ENTITY, {**ENTITY, 'item_id': 'e2'}], True),
    ([PROPERTY], False),
    ([ENTITY, PROPERTY], False),
    ([PROPERTY, ENTITY], False),
    ([], False),
    ([{**ENTITY, 'property': ''}], False),
    ([{**ENTITY, 'property': None}], False),
])
def test_only_nonempty_property_free_batch_uses_entity_system(items, entity_only):
    messages = build_verification_messages(chunk=ExtractionChunk('c', '对象甲正常。'), items=items)
    expected = ENTITY_VERIFICATION_SYSTEM_PROMPT if entity_only else VERIFICATION_SYSTEM_PROMPT
    assert messages[0]['content'].startswith(expected + '\n' + MESSAGE_BOUNDARY_RULES)
    assert messages[0]['content'].count(FACT_EVIDENCE_RULES) == 1
    assert json.loads(messages[1]['content'])['task']['items'] == items


def test_pure_entity_retains_every_type_and_each_item_source_authority():
    types = ['OfficialDocument', 'DocumentUnit', 'OperationalAgent', 'OperationalActivity',
             'OperationalInformation', 'OperationalObject', 'OperationalConstraint', 'OperationalExchange']
    plan = compile_plan('@startuml\n' + '\n'.join('class ' + type_id for type_id in types) + '\n@enduml')
    first = ExtractionChunk('private-one', '泵甲温度过高并且振动异常时停机。', document_id='document-one')
    second = ExtractionChunk('private-two', '泵乙温度正常，维修后允许启动。', document_id='document-two')
    items = [
        {'item_id': 'e1', 'type': 'OperationalConstraint', 'name': '泵甲联合异常停机要求',
         'candidate_properties': {'premise': '泵甲温度过高并且振动异常', 'result': '泵甲停机',
                                  'unverified_management_field': '尚无证据'}, 'evidence_quotes': [first.text]},
        {'item_id': 'e2', 'type': 'OperationalConstraint', 'name': '泵乙维修后启动要求',
         'candidate_properties': {'premise': '泵乙维修后', 'result': '允许启动'}, 'evidence_quotes': [second.text]},
    ]
    before = deepcopy(items)
    messages = build_verification_messages(chunk=first, items=items, plan=plan,
        source_chunks=[first, second], item_sources={'e1': [first], 'e2': [second]})
    user = json.loads(messages[1]['content'])
    assert user['task']['items'] == before and items == before
    assert all(f'type `{type_id}`' in user['ontology'] for type_id in types)
    assert user['documents'] == (f'【核验项 e1 的原文】\n{first.text}\n【核验原文结束】\n\n'
                                 f'【核验项 e2 的原文】\n{second.text}\n【核验原文结束】')
    assert '其他项的资料不能补足其对象、条件、值或身份' in messages[0]['content']
    assert 'private-' not in messages[1]['content'] and 'document-one' not in messages[1]['content']
    assert user['output_contract']['success_schema']['properties']['item_id']['enum'] == ['e1', 'e2']


def test_entity_scope_excludes_field_signoff_and_replacement_instructions():
    entity_system = build_verification_messages(chunk=ExtractionChunk('c', '对象甲'), items=[ENTITY])[0]['content']
    property_system = build_verification_messages(chunk=ExtractionChunk('c', '对象甲'), items=[PROPERTY])[0]['content']
    for field_operation in ('每项仍只核验指定 property', '如有 previous_value 或 conflicting_values',
                            '字段类型无法表达综合结果时不拼造值', '按本体区分过程、结果、原因和标识等字段'):
        assert field_operation not in entity_system
        assert field_operation in property_system
    for boundary in ('不逐字段裁定属性值', '实体 yes 不证明任何属性', '实体准入与属性完整性分别判断',
                     '非身份属性缺失', '管理字段尚未获证不构成拒绝该公文的理由',
                     '不能以属性另验为由忽略规则前提或混合主体', '部分成员或各片段分别出现，不能证明组合后的整体成立'):
        assert boundary in entity_system


@pytest.mark.parametrize('type_id,name,properties,source,verdict', [
    ('BusinessObject', '信息部门', {'role': '建设对象'}, '信息部门作为通知接收方，未作为建设对象。', 'no'),
    ('Rule', '泵乙异常停机', {'premise': '泵乙温度过高', 'result': '泵乙停机'},
     '泵甲温度过高时停机；泵乙温度正常。', 'no'),
    ('Rule', '高温停机', {'premise': '温度过高', 'result': '停机'},
     '仅在维修阶段且振动异常、温度过高时停机。', 'uncertain'),
])
def test_pure_entity_admission_still_requires_affirmative_full_claim_verdict(type_id, name, properties, source, verdict):
    plan = compile_plan('@startuml\nclass BusinessObject\nclass Rule\n@enduml')
    row = {'type': type_id, 'name': name, 'properties': properties, 'evidence': {'quote': source}}
    before = deepcopy(properties)
    observed = []
    def llm(messages):
        user = json.loads(messages[1]['content'])
        item = user['task']['items'][0]
        observed.append(item)
        assert messages[0]['content'].startswith(ENTITY_VERIFICATION_SYSTEM_PROMPT)
        assert '三者均获支持才能返回 yes' in messages[0]['content']
        assert '主体、前后件、对象、阶段、否定和适用条件' in messages[0]['content']
        assert item['candidate_properties'] == before
        assert source in user['documents']
        return json.dumps({'item_id': item['item_id'], 'supported': verdict, 'reason': '实例类型或必要条件未获完整支持'})
    admitted = admit_entity_names([row], [ExtractionChunk('c', source)], llm,
                                 attempts=[], warnings=[], plan=plan, max_attempts=1)
    assert observed and admitted == [] and 'entity_evidence' not in row
    assert row['properties'] == before
