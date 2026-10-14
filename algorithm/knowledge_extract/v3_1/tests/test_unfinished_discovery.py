"""A gateway stop flag cannot prove a syntactically unfinished scan complete."""
import json

import pytest

from ..candidate_generation import _unclosed_json_record, generate_structured
from ..chunk_context import ExtractionChunk
from ..llm_client import StructuredLLMResponse
from ..ontology_compiler import compile_plan
from ..split_extractor import extract_split
from .stub_llm import StubLLM, span_item, source_documents


@pytest.mark.parametrize('line,expected', [
    ('{"type":"Person","mention":"未完',True), ('{"type":',True),
    ('{"type":"Person","evidence":{"quote":"x"}',True),
    ('{"value":"braces } [ inside a string"}',False),
    ('{"x":"\\\"quoted"}',False), ('{"a":1,"a":2}',False),
    ('{"a":1 "b":2}',False), ('explanation {',False), ('{"a":]}',False)])
def test_detects_only_unclosed_json_containers_or_strings(line,expected):
    assert _unclosed_json_record(line) is expected


def test_stopped_unfinished_tail_retains_prefix_and_requires_source_recovery():
    row=span_item('Person','甲')
    text=json.dumps(row)+'\n{"type":"Person","mention":"乙'
    response=StructuredLLMResponse(text=text,finish_reason='stop',status='completed',api='chat.completions',response_mode='plain')
    attempts=[]
    parsed=generate_structured(lambda m:response,
        [{'role':'system','content':'JSONL'},{'role':'user','content':'{"task":{"operation":"entity"}}'}],
        compile_plan('@startuml\nclass Person\n@enduml').output_schemas['entity_candidates'],
        attempts=attempts,use_scheduler=False,pass_name='entity',chunk_id='c',max_attempts=3,
        retry_base_seconds=0,split_on_overflow=True)
    assert parsed=={'entities':[row]}
    assert len(attempts)==1 and attempts[0]['type']=='incomplete'
    assert attempts[0]['unfinished_entity_tail'] and attempts[0]['terminal']


def test_recovery_scans_smaller_original_sources_without_priming_missing_types():
    text='甲负责检查。\n'+'背景说明。'*80+'\n乙负责记录。'
    seen=[]
    fallback=StubLLM(verification_handler=lambda i,c:'yes')
    def llm(messages):
        user=json.loads(messages[1]['content'])
        if user['task']['operation']!='entity':return fallback(messages)
        primary=next(d['text'] for d in source_documents(user) if not d.get('context_only'))
        types=user['output_contract']['success_schema']['properties']['type']['enum']
        seen.append((primary,types))
        if primary==text:
            return json.dumps(span_item('Person','甲'))+'\n{"type":"Person","mention":"乙'
        names=[name for name in ('甲','乙') if name in primary]
        return '\n'.join(json.dumps(span_item('Person',name)) for name in names) or '{}'
    result=extract_split('@startuml\nclass Person\nclass Place\n@enduml',
        [ExtractionChunk('c',text)],llm=llm,skill_refs=[],max_attempts=1,retry_base_seconds=0,
        max_repair_rounds=0,best_effort=True)
    assert result['status']=='succeeded'
    assert {r['name'] for r in result['raw']['entities']}=={'甲','乙'}
    assert len(seen)>1 and all(types==['Person','Place'] for _,types in seen)
    assert all(len(primary)<len(text) for primary,_ in seen[1:])
    assert not any(a['terminal'] for a in result['attempts'])
    assert result['attempts'][0]['recovery']=='split_request'


def test_repetitive_truncated_discovery_splits_types_before_source():
    text='甲负责检查。\n'+'背景说明。'*80+'\n乙负责记录。'
    seen=[]
    fallback=StubLLM(verification_handler=lambda i,c:'yes')
    def llm(messages):
        user=json.loads(messages[1]['content'])
        if user['task']['operation']!='entity':return fallback(messages)
        types=user['output_contract']['success_schema']['properties']['type']['enum']
        primary=next(d['text'] for d in source_documents(user) if not d.get('context_only'))
        seen.append((primary,types))
        if len(types)>1:
            return '\n'.join(json.dumps(span_item('Person','甲')) for _ in range(16))+'\n{"type":"Person","mention":"乙'
        if types==['Person']:return json.dumps(span_item('Person','乙'))
        return '{}'
    result=extract_split('@startuml\nclass Person\nclass Place\n@enduml',
        [ExtractionChunk('c',text)],llm=llm,skill_refs=[],max_attempts=1,retry_base_seconds=0,
        max_repair_rounds=0,best_effort=True)
    assert result['status']=='succeeded'
    assert {r['name'] for r in result['raw']['entities']}=={'甲','乙'}
    assert seen==[(text,['Person','Place']),(text,['Person']),(text,['Place'])]
    failure=result['attempts'][0]
    assert failure['duplicate_entity_records']==15
    assert failure['repetitive_entity_output'] and failure['unfinished_entity_tail']
    assert failure['recovery']=='split_request' and not failure['terminal']
