"""Freeze genuine Qwen property loops and partition input tasks without source loss."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import re
from pathlib import Path

from algorithm.knowledge_extract.jsonl_protocol import messages_for_jsonl, record_schema, jsonl_instruction
from eval.tools.repetition_causal import section, replace_section, expansion_trajectory
from eval.tools.repetition_study import metrics, records


def safe_error(exc, credential):
    """Apply existing judge/trace secret + Bearer redaction, hide URL parameters."""
    text=str(exc)
    if credential:
        text=text.replace(credential,'[REDACTED]')
    text=re.sub(r'(?i)Bearer\s+[^\s"\']+', 'Bearer [REDACTED]', text)
    text=re.sub(r'https?://[^\s"\']+', '[REDACTED_URL]', text)
    text=re.sub(r'(?i)(authorization|api[_-]?key|access[_-]?token|password)\s*[=:]\s*[^\s,;}]+', r'\1=[REDACTED]', text)
    return text[:400]


def focused_call(call, entity_id, property_name=None):
    result=copy.deepcopy(call)
    item=result['schema']['properties']['updates']['items']['properties']
    assert entity_id in item['entity_id']['enum']
    item['entity_id']['enum']=[entity_id]
    if property_name is not None:
        assert property_name in item['properties']['properties']
        item['properties']['properties']={property_name:item['properties']['properties'][property_name]}
    user=next(m for m in result['messages'] if m['role']=='user')
    lines=section(user['content'],'known entities').group(1).splitlines()
    row=next(line for line in lines if '`'+entity_id+'`' in line)
    if property_name is not None:
        parts=row.split('|')
        parts[-2]=f' {property_name}({property_name}:string) '
        row='|'.join(parts)
    user['content']=replace_section(user['content'],'known entities','\n'.join(lines[:2]+[row]))
    user['content']=replace_section(user['content'],'record schema',json.dumps(record_schema(result['schema']),ensure_ascii=False,sort_keys=True))
    user['content']=replace_section(user['content'],'output protocol',jsonl_instruction(result['schema']))
    return result


def freeze(trace,out):
    calls={n:json.loads((trace/(n+'-call.json')).read_text(encoding='utf8')) for n in ['000034','000040']}
    cases=[]
    def add(name,call,derivation):
        result={'id':name,'call':copy.deepcopy(call),'reference_anchors':[],
                'quality_labels':{'label_status':'agent-reviewed / provisional; no independent human gold'},
                'derivation':derivation}
        result['call']['messages']=messages_for_jsonl(call['messages'],call['schema'])
        result['call']['context']['response_mode']='frozen-wire'
        cases.append(result)
    for number,call in calls.items():
        add('property-'+number+'-baseline',call,{'source_trace':'74c55be4a06f4b0783a53e97d1efa5b8',
                                               'source_call':number,'source_sha256':hashlib.sha256((trace/(number+'-call.json')).read_bytes()).hexdigest(),
                                               'actual_wire':'trace messages plus existing messages_for_jsonl injection'})
    source=calls['000040'];item=source['schema']['properties']['updates']['items']['properties']
    ids=item['entity_id']['enum'];properties=list(item['properties']['properties'])
    for eid in ids:
        add('property-000040-'+eid,focused_call(source,eid),{'source_call':'000040','scope':'one entity','entity_id':eid,'other_entities':'partitioned into other frozen tasks, not discarded'})
    for prop in properties:
        add('property-000040-entity-25-'+prop,focused_call(source,'entity-25',prop),
            {'source_call':'000040','scope':'one entity and one property','entity_id':'entity-25','property':prop,
             'other_properties':'partitioned into other frozen tasks, not discarded'})
    out.mkdir(parents=True,exist_ok=True);entries=[]
    for case in cases:
        data=json.dumps(case,ensure_ascii=False,indent=2)+'\n'
        (out/(case['id']+'.json')).write_text(data,encoding='utf8',newline='\n')
        entries.append({'id':case['id'],'sha256':hashlib.sha256(data.encode()).hexdigest()})
    plan=[['property-000040-baseline','baseline',1],['property-000034-baseline','baseline',1],
          ['property-000040-entity-25','baseline',1],['property-000040-entity-25-职责','baseline',1],
          ['property-000040-entity-25-职责','baseline',2],['property-000040-entity-25','baseline',2]]
    manifest={'cases':entries,'provider':'matrix-qwen35','model':'Qwen/Qwen3.5-4B','temperature':0,'max_tokens':8192,
              'thinking':{'type':'disabled'},'timeout_seconds':180,'max_retries':0,'workers':1,
              'entity_partition':ids,'entity_25_property_partition':properties,
              'coverage_warning':'Six calls diagnose the looping entity/field only. Other frozen partitions are not executed; no whole-batch coverage claim.',
              'main_matrix_overlap':'must wait for main Qwen extraction matrix to finish before replay'}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    (out/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    return cases,plan


def audit(call,text):
    from jsonschema import Draft202012Validator
    from algorithm.knowledge_extract.v3.evidence import locate_quote
    from algorithm.knowledge_extract.v3.chunk_context import ExtractionChunk
    item=call['schema']['properties']['updates']['items'];validator=Draft202012Validator(item)
    user=next(m for m in call['messages'] if m['role']=='user')
    source=json.loads(section(user['content'],'current chunk').group(1))['text']
    rows=records(text);valid=[];bound=[]
    for row in rows:
        valid.append(not list(validator.iter_errors(row)))
        quote=(row.get('evidence') or {}).get('quote','')
        bound.append(bool(quote and locate_quote([ExtractionChunk('body',source)],quote)))
    return {**metrics(text),'trajectory':expansion_trajectory(text),'schema_valid_records':sum(valid),
            'quote_bound_records':sum(bound),'schema_and_quote_valid_records':sum(v and b for v,b in zip(valid,bound)),
            'quote_binding_note':'literal existing v3 locate_quote; binding is not independent semantic truth'}


def replay(dataset,out,profile_name='matrix-qwen35'):
    # Use the existing model endpoint, never print or persist credentials.
    import os,time
    from openai import OpenAI
    from backend.app.services.extraction_config import resolve_config, public_config
    from algorithm.knowledge_extract.request_runtime import ScheduledClient
    config=resolve_config({'version':'v3','provider':profile_name,'request_timeout':180,'document_timeout':900})
    assert config['model']=='Qwen/Qwen3.5-4B'
    assert config['max_output_tokens']==8192
    sdk=OpenAI(base_url=config['base_url'],api_key=os.environ[config['api_key_env']],timeout=180,max_retries=0)
    out.mkdir(parents=True,exist_ok=True)
    for name,method,repeat in json.loads((dataset/'plan.json').read_text(encoding='utf8')):
        target=out/(name+'--'+method+'--'+str(repeat)+'.json')
        if target.exists():continue
        client=ScheduledClient(sdk,config)
        case=json.loads((dataset/(name+'.json')).read_text(encoding='utf8'));request={'model':config['model'],'messages':case['call']['messages'],'temperature':0,'max_tokens':8192,'thinking':{'type':'disabled'},'timeout_seconds':180,'resolved_config':public_config(config)}
        started=time.time();text='';error=None;finish=None;usage=None
        try:
            response=client.chat.completions.create(model=request['model'],messages=request['messages'],temperature=0,max_tokens=8192,extra_body={'thinking':{'type':'disabled'}})
            text=response.choices[0].message.content or '';finish=response.choices[0].finish_reason;usage=response.usage.model_dump() if response.usage else None
        except Exception as exc:
            error={'type':type(exc).__name__,'message':safe_error(exc,os.environ[config['api_key_env']])}
        summary={'case':name,'repeat':repeat,'started_unix':started,'seconds':time.time()-started,'finish_reason':finish,'usage':usage,'error':error}
        data={'request':request,'response_text':text,'summary':summary}
        target.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8',newline='\n')
        print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--trace',type=Path);parser.add_argument('--dataset',type=Path,default=Path('test_data/repetition/property-loop-v1'));parser.add_argument('--replay-out',type=Path);parser.add_argument('--audit-out',type=Path)
    args=parser.parse_args()
    if args.audit_out:
        result=[]
        for number in ['000034','000040']:
            call=json.loads((args.trace/(number+'-call.json')).read_text(encoding='utf8'))
            response=json.loads((args.trace/(number+'-call-response.json')).read_text(encoding='utf8'))
            result.append({'source_call':number,'finish_reason':response['finish_reason'],'usage':response['usage'],
                           'seconds':response['elapsed_seconds'],**audit(call,response['text'])})
        args.audit_out.parent.mkdir(parents=True,exist_ok=True)
        args.audit_out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8',newline='\n')
        print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.replay_out:replay(args.dataset,args.replay_out)
    else:
        cases,plan=freeze(args.trace,args.dataset);print(json.dumps({'cases':len(cases),'planned_calls':len(plan)}))
