"""Original names survive candidate selection and cross-chunk completion."""
import json

import pytest

from ..chunk_context import ExtractionChunk
from ..ontology_compiler import compile_plan
from ..prompt_router import build_chat_messages, resolve_document_route
from ..split_extractor import extract_split
from .stub_llm import StubLLM, span_item

ONTOLOGY = '''@startuml
class Organization {
 + 职责 (duty): String
}
@enduml'''
NAME = '中车  智能装备有限公司（CRRC）'


@pytest.mark.parametrize('stage', ['entity', 'repair'])
def test_original_name_rules_reach_system_and_user(stage):
    plan = compile_plan(ONTOLOGY)
    chunk = ExtractionChunk('c', f'{NAME}（简称“智能装备”）负责检查。')
    messages = build_chat_messages(task=stage, plan=plan, route=resolve_document_route(plan, [chunk]),
                                  chunk=chunk, output_schema=plan.output_schemas["entity_candidates"], repair_issues=[{'code': 'MISSING_ENTITY'}] if stage=='repair' else [])
    user = json.loads(messages[1]['content'])
    for rules in [messages[0]['content'], user['output_contract']['constraints']]:
        assert '逐字保留完整名称及内部空格、大小写、标点' in rules
        assert '优先选择原文中实际出现的完整全称' in rules
        assert '不得凭常识扩写简称' in rules
        assert '仅当原文没有明确名称时' in rules
    assert set(user['output_contract']['success_schema']['properties']) == {'type', 'mention', 'properties', 'evidence'}


def test_full_source_name_unchanged_after_cross_chunk_completion():
    def properties(prompt, chunk):
        user=json.loads(prompt)
        known=user['task']['known_entities']
        assert len(known)==1 and known[0]['name']==NAME
        assert len(known[0]['occurrences'])==2
        assert user['output_contract']['success_schema']['properties']['name']['enum']==[NAME]
        return {'entities':[{'type':'Organization','name':NAME,'properties':{'duty':'检查并记录'},'evidence':{'quote':chunk['text'], 'property_quotes':{'duty':[d['text'] for d in user['documents']]}}}]}
    result=extract_split(ONTOLOGY, [ExtractionChunk('a',f'{NAME}（简称“智能装备”）负责检查。'),
                                   ExtractionChunk('b',f'{NAME}负责记录。')],
        llm=StubLLM(entity_handler=lambda p,c:[span_item('Organization',NAME, c['text'])],property_handler=properties,
                    verification_handler=lambda item,chunk: 'yes' if item['name']==NAME and item['value']=='检查并记录' else 'no'))
    assert result['status']=='succeeded'
    assert len(result['raw']['entities'])==1
    assert result['raw']['entities'][0]['name']==NAME
    assert result['raw']['entities'][0]['properties']=={'duty':'检查并记录'}
    assert result['intermediate_model']['objects'][0]['display_name']==NAME


def test_completion_cannot_replace_full_source_name_with_short_name():
    llm=StubLLM(entity_handler=lambda p,c:[span_item('Organization',NAME)],
        property_handler=lambda p,c:{'entities':[{'type':'Organization','name':'智能装备','properties':{},'evidence':{'quote':NAME}}]})
    result=extract_split(ONTOLOGY,[ExtractionChunk('a',f'{NAME}（简称“智能装备”）负责检查。')],llm=llm,max_attempts=1)
    assert result['error_code']=='LLM_SCHEMA_INVALID'
    assert [e['name'] for e in result['raw']['entities']]==[NAME]  # verified discovery survives failed completion
