"""Isolated replay research; never changes Backend settings. Raw artifacts stay in tmp."""
from __future__ import annotations
import argparse,collections,copy,hashlib,json,re,time,concurrent.futures
from pathlib import Path

def records(text):
    text=re.sub(r'^```(?:jsonl|json)?\s*|\s*```$','',text.strip())
    values=[]; decoder=json.JSONDecoder();pos=0
    wrapper=re.match(r'^\{\s*"(?:entities|relations|updates)"\s*:\s*\[',text)
    if wrapper:pos=wrapper.end()
    while pos<len(text):
        while pos<len(text) and (text[pos].isspace() or text[pos]==','):pos+=1
        try:value,end=decoder.raw_decode(text,pos)
        except ValueError:
            end=text.find('\n',pos)
            if end<0:break
            pos=end+1;continue
        pos=end
        if isinstance(value,dict):
            arrays=[v for k,v in value.items() if k in {'entities','relations','updates'} and isinstance(v,list)]
            values.extend(arrays[0] if len(value)==1 and arrays else [value])
    return [x for x in values if isinstance(x,dict)]

def metrics(text):
    rows=records(text);keys=[json.dumps(x,ensure_ascii=False,sort_keys=True) for x in rows]
    counts=collections.Counter(keys);seen=set();first=None
    for i,k in enumerate(keys,1):
        if k in seen and first is None:first=i
        seen.add(k)
    # A loop inside a single unfinished JSON string is invisible to line dedup.
    run=max((len(m.group(0)) for m in re.finditer(r'(.{3,80}?)\1{3,}',text)),default=0)
    semantic=[]
    for row in rows:
        if 'mention' in row:semantic.append((row.get('type'),row['mention']))
        elif 'from_id' in row:semantic.append((row.get('from_id'),row.get('to_id'),row.get('rel_type')))
    identity_counts=collections.Counter(semantic)
    return {'records':len(rows),'unique_records':len(counts),'duplicate_fraction':1-len(counts)/len(rows) if rows else 0,'first_duplicate_record':first,'max_exact_multiplicity':max(counts.values(),default=0),'repeated_substring_chars':run,'identity_unique':len(set(semantic)),'identity_duplicate_fraction':1-len(set(semantic))/len(semantic) if semantic else None,'identity_max_multiplicity':max(identity_counts.values(),default=0),'characters':len(text)}

def variant(case,method):
    call=case['call']; messages=copy.deepcopy(call['messages']);temp=0;extra={}
    from algorithm.knowledge_extract.jsonl_protocol import messages_for_jsonl
    if call['context'].get('response_mode')=='jsonl':messages=messages_for_jsonl(messages,call['schema'])
    if method=='budget':messages[0]['content']+='\n本次最多输出 40 条记录。逐条检查唯一键，输出完毕立即结束。不得为凑数重复。'
    elif method=='grounded':messages[0]['content']+='\n本次精度优先：实体 mention 必须逐字出现在原文，不允许忠实归纳或泛化概念。没有明确业务职责的普通名词不抽取。不得将同一事实拆成细碎候选。'
    elif method=='temperature_03':temp=.3
    elif method=='frequency_05':extra['frequency_penalty']=.5
    elif method=='short_chunk':
        for msg in messages:
            if msg['role']!='user':continue
            m=re.search(r'(## current chunk\n)(.*?)(?=\n\n## |\Z)',msg['content'],re.S)
            if not m:continue
            try:chunk=json.loads(m.group(2))
            except ValueError:continue
            text=chunk['text']; cutoff=text.find('# 二、工作要求')
            chunk['text']=text[:cutoff] if cutoff>0 else text[:max(1,len(text)//2)]
            msg['content']=msg['content'][:m.start(2)]+json.dumps(chunk,ensure_ascii=False)+msg['content'][m.end(2):]
    elif method=='string_bound':messages[0]['content']+='\n每个属性值必须简短：分类只选一个原文支持的类别，不枚举可能类型；语义属性最多 80 字，原文内容字段可完整引用。'
    elif method=='stream_guard':pass
    elif method!='baseline':raise ValueError(method)
    return messages,temp,extra

def input_text(messages):
    for message in messages:
        match=re.search(r'## current chunk\n(.*?)(?=\n\n## |\Z)',message.get('content',''),re.S)
        if match:
            try:return json.loads(match.group(1)).get('text','')
            except ValueError:pass
    return ''

def quality_metrics(text,messages,anchors):
    source=input_text(messages);rows=records(text)
    entities=[r for r in rows if 'mention' in r]
    literal=sum(bool(r.get('mention')) and r['mention'] in source for r in entities)
    supported=[a for a in anchors if a['mention_contains'] in source]
    hits=sum(any(r.get('type')==a['type'] and a['mention_contains'] in r.get('mention','') for r in rows) for a in supported)
    return {'input_text_chars':len(source),'literal_mention_fraction':literal/len(entities) if entities else None,'anchor_hits':hits,'anchor_count':len(supported),'anchor_scope_excluded':len(anchors)-len(supported),'type_counts':dict(collections.Counter(r.get('type') for r in entities))}

def replay(dataset,out,plan,workers=2,max_tokens=8192,timeout=180,service_id=None):
    from openai import OpenAI
    from external_services.model_routes import resolve_service
    from external_services.llm.service import model_call
    from qasystem_config.configuration import getenv
    profile=resolve_service('extract',service_id=service_id)
    if not profile:
        raise ValueError('Replay requires an ops registered extraction service or --service-id')
    credential=getenv(profile['api_key_env'],'') if profile.get('api_key_env') else ''
    if profile['kind']=='api' and not credential:
        raise ValueError('Registered replay service credential is not configured')
    client=OpenAI(base_url=profile['base_url'],api_key=credential or 'not-needed',timeout=timeout,max_retries=0)
    out.mkdir(parents=True,exist_ok=True)
    def one(spec):
        cid,method,repeat=spec;case=json.loads((dataset/(cid+'.json')).read_text(encoding='utf8'));messages,temp,extra=variant(case,method)
        safe={'case':cid,'method':method,'repeat':repeat,'model':profile['model'],'temperature':temp,'max_tokens':max_tokens,'timeout_seconds':timeout,'thinking':{'type':'disabled'},**extra,'messages':messages}
        target=out/(cid+'--'+method+'--'+str(repeat)+'.json')
        if target.exists():return json.loads(target.read_text(encoding='utf8'))['summary']
        start=time.monotonic();text='';finish=None;usage=None;stopped=False;error=None;metadata={}
        try:
            params=dict(model=profile['model'],messages=messages,temperature=temp,max_tokens=max_tokens,extra_body={'thinking':{'type':'disabled'}},**extra)
            with model_call(profile,params,timeout=timeout) as remaining:
                params['timeout']=remaining
                if method=='stream_guard':
                    stream=client.chat.completions.create(**params,stream=True)
                    checked=0
                    for item in stream:
                        if not item.choices:continue
                        text+=item.choices[0].delta.content or '';finish=item.choices[0].finish_reason or finish
                        if len(text)-checked<256:continue
                        checked=len(text);m=metrics(text)
                        if m['max_exact_multiplicity']>=3 or m['repeated_substring_chars']>=400:
                            stream.close();stopped=True;break
                else:
                    response=client.chat.completions.create(**params);text=response.choices[0].message.content or '';finish=response.choices[0].finish_reason;usage=response.usage.model_dump() if response.usage else None
                    metadata={key:getattr(response,key,None) for key in ['id','model','created','system_fingerprint']}
        except Exception as exc:error={'type':type(exc).__name__,'message':(str(exc).replace(credential,'[REDACTED]') if credential else type(exc).__name__)[:500]}
        from algorithm.knowledge_extract.jsonl_protocol import decode_jsonl
        from algorithm.knowledge_extract.v3.structured_output import validate_json_schema
        decoded,parse_errors=decode_jsonl(text,case['call']['schema']);schema_errors=validate_json_schema(decoded,case['call']['schema'])
        summary={'case':cid,'method':method,'repeat':repeat,'seconds':time.monotonic()-start,'finish_reason':finish,'guard_stopped':stopped,'usage':usage,'error':error,'parse_errors':parse_errors,'schema_error_count':len(schema_errors),**quality_metrics(text,messages,case.get('reference_anchors',[])),**metrics(text)}
        target.write_text(json.dumps({'request':safe,'response_text':text,'response_metadata':metadata,'summary':summary},ensure_ascii=False,indent=2),encoding='utf8',newline='\n');print(json.dumps(summary,ensure_ascii=False),flush=True);return summary
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:results=list(pool.map(one,plan))
    (out/'summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8',newline='\n');return results

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--dataset',type=Path,required=True);parser.add_argument('--out',type=Path,required=True);parser.add_argument('--plan',type=Path,required=True);parser.add_argument('--service-id');parser.add_argument('--max-tokens',type=int,default=8192);parser.add_argument('--timeout',type=float,default=180);args=parser.parse_args()
    replay(args.dataset,args.out,json.loads(args.plan.read_text(encoding='utf8')),max_tokens=args.max_tokens,timeout=args.timeout,service_id=args.service_id)
