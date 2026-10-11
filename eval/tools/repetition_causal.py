"""Freeze paired task-contract ablations without changing production algorithms."""
from __future__ import annotations
import argparse,copy,hashlib,json,re
from collections import Counter,defaultdict
from pathlib import Path
from algorithm.knowledge_extract.jsonl_protocol import messages_for_jsonl,record_schema

def section(content,heading):
    match=re.search(r'^## '+re.escape(heading)+r'\n(.*?)(?=\n\n## |\Z)',content,re.M|re.S)
    if not match:raise ValueError(heading)
    return match

def replace_section(content,heading,value):
    match=section(content,heading)
    return content[:match.start(1)]+value+content[match.end(1):]

def catalog(messages):
    result={}
    for message in messages:
        if message['role']!='user':continue
        body=section(message['content'],'known entities').group(1)
        for mid,kind,name in re.findall(r'^\| `(entity-\d+)` \| `([^`]+)` \| ([^|]+) \|',body,re.M):
            result[mid]={'type':kind,'name':name.strip()}
    return result

def relation_integrity(messages,rows):
    endpoints=catalog(messages);signatures=set()
    for message in messages:
        if message['role']!='user':continue
        body=section(message['content'],'ontology relation signatures').group(1)
        signatures.update(re.findall(r'`([^`]+)` -\[`([^`]+)`\]-> `([^`]+)`',body))
    checked=[]
    for row in rows:
        if 'from_id' not in row:continue
        source=endpoints.get(row['from_id']);target=endpoints.get(row['to_id'])
        present=source is not None and target is not None
        signature_ok=present and (source['type'],row['rel_type'],target['type']) in signatures
        checked.append({'from_id':row['from_id'],'source_type':source['type'] if source else None,'rel_type':row['rel_type'],'to_id':row['to_id'],'target_type':target['type'] if target else None,'endpoints_present':present,'signature_valid':bool(signature_ok)})
    return {'checked_edges':checked,'unknown_endpoint_edges':sum(not r['endpoints_present'] for r in checked),'signature_invalid_edges':sum(not r['signature_valid'] for r in checked)}

def expansion_trajectory(text):
    from eval.tools.repetition_study import records
    values=records(text);seen=set();novel=[];last_new=0;first_repeat=None
    for index,value in enumerate(values,1):
        key=json.dumps(value,sort_keys=True,ensure_ascii=False);is_new=key not in seen;novel.append(is_new)
        if is_new:last_new=index;seen.add(key)
        elif first_repeat is None:first_repeat=index
    # Main guard proposal: tolerate occasional duplicates; stop only sustained no progress.
    stop=None
    for index in range(40,len(values)+1):
        if not any(novel[index-20:index]):stop=index;break
    serialized=[len(json.dumps(v,ensure_ascii=False)) for v in values]
    duplicate_chars=sum(n for n,is_new in zip(serialized,novel) if not is_new)
    return {'complete_records':len(values),'unique_records':len(seen),'first_repeat_record':first_repeat,'last_new_record':last_new,'zero_novel_tail_records':len(values)-last_new,'exact_duplicate_reserialized_chars':duplicate_chars,'exact_duplicate_reserialized_char_fraction':duplicate_chars/sum(serialized) if serialized else None,'sustained_zero_novel_stop_record':stop,'guard_simulation_only':True,'sustained_guard_unique_retention':len({json.dumps(v,sort_keys=True,ensure_ascii=False) for v in values[:stop]})/len(seen) if stop and seen else None}

def analyze(dataset,results,out):
    from eval.tools.repetition_study import records,metrics,input_text,quality_metrics
    rows=[];grouped=defaultdict(list)
    for p in sorted(results.glob('*--*.json')):
        data=json.loads(p.read_text(encoding='utf8'));summary=dict(data['summary']);case=json.loads((dataset/(summary['case']+'.json')).read_text(encoding='utf8'))
        parsed=records(data['response_text']);summary.update(metrics(data['response_text']));summary.update(quality_metrics(data['response_text'],data['request']['messages'],case['reference_anchors']));summary.update(relation_integrity(data['request']['messages'],parsed))
        source=input_text(data['request']['messages']);entities=[r for r in parsed if 'mention' in r]
        unsupported=[];literal_low_value=[]
        for r in entities:
            mention=r['mention'];quote=r.get('evidence',{}).get('quote','')
            # Diagnostic proxies, not an independent semantic judge or human truth.
            if mention not in source and (not quote or quote not in source):unsupported.append({'type':r.get('type'),'mention':mention,'reason':'neither mention nor evidence.quote locates literally in this segment'})
            if re.fullmatch(r'[0-9年月日号.\s]+',mention) or mention in {'计划要求','企业实际','联系人'}:literal_low_value.append({'type':r.get('type'),'mention':mention,'reason':'scalar/field/generic phrase provisional scope violation'})
        summary.update(provisional_unsupported_candidates=unsupported,provisional_low_value_candidates=literal_low_value,label_status='agent-reviewed / provisional; proxies are not proof of hallucination',source_range=case['derivation'].get('source_range'))
        summary['expansion_trajectory']=expansion_trajectory(data['response_text']);summary['artifact']=str(p);rows.append(summary);grouped[summary['case']].append(summary)
    merged=[]
    for repeat in sorted({r['repeat'] for r in rows}):
        pair=[r for r in rows if r['case'] in {'entity-segment-a','entity-segment-b'} and r['repeat']==repeat]
        if len(pair)!=2:continue
        combined=[];identities=[];by_segment=[]
        for item in pair:
            data=json.loads(Path(item['artifact']).read_text(encoding='utf8'));values=records(data['response_text']);keys={(r.get('type'),r['mention']) for r in values if 'mention' in r};by_segment.append(keys)
            combined.extend(values);identities.extend((r.get('type'),r['mention']) for r in values if 'mention' in r)
        counts=Counter(identities)
        merged.append({'repeat':repeat,'both_responded':all(not r['error'] for r in pair),'records_sum':len(combined),'identity_union':len(counts),'duplicate_identity_records':sum(n-1 for n in counts.values()),'cross_segment_identity_overlap':len(by_segment[0]&by_segment[1]),'per_segment_within_target':[r['records']<=20 if not r['error'] else None for r in pair],'official_document_mentions':sorted({r['mention'] for r in combined if r.get('type')=='OfficialDocument'}),'combined_completion_tokens':sum((r.get('usage') or {}).get('completion_tokens',0) for r in pair),'combined_seconds_sum':sum(r['seconds'] for r in pair),'warning':'cross-segment overlap may be legitimate repeated-source facts; exact identity is not semantic equivalence; multiple document mentions require alias/type review'} )
    groups=[]
    for case,group in grouped.items():
        responding=[r for r in group if not r['error']]
        groups.append({'case':case,'attempts':len(group),'responses':len(responding),'timeouts_or_errors':len(group)-len(responding),'length':sum(r['finish_reason']=='length' for r in responding),'exact_repeat_responses':sum(r['duplicate_fraction']>0 for r in responding),'identity_repeat_responses':sum(bool(r['identity_duplicate_fraction']) for r in responding),'schema_invalid_responses':sum(r['schema_error_count']>0 for r in responding),'signature_invalid_edges':sum(r['signature_invalid_edges'] for r in responding),'unknown_endpoint_edges':sum(r['unknown_endpoint_edges'] for r in responding),'record_counts':[r['records'] for r in responding],'completion_tokens':[(r.get('usage') or {}).get('completion_tokens') for r in responding]})
    result={'rows':rows,'groups':groups,'merged_segments':merged,'calls':len(rows),'label_status':'agent-reviewed / provisional; no independent human gold','interpretation_boundary':'Count length with verified repetitive output separately from ordinary timeout without partial output. Zero observed loops in a small arm does not prove cure; schema validity alone cannot validate relations.'}
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8',newline='\n');return result

def old_wire(messages,schema):
    # Exact shared injection from a5e1555 (88 merge 65ee14f); no new instruction.
    item=record_schema(schema)
    text=('\n\n输出协议更新：使用 JSONL，覆盖此前要求的外层 JSON 对象/数组格式。'
          '每条记录独占一行，不要 Markdown、解释、外层数组或集合键。'
          '字符串中的换行必须转义为 \\n。每行必须独立完整，禁止重复记录。'
          '没有记录时只输出一行 null。'
          '每行记录的字段约束（仅供本地校验，不使用服务端结构化输出）：'
          +json.dumps(item,ensure_ascii=False))
    result=copy.deepcopy(messages);result[0]['content']+=text;return result

def relation_case(source,protocol,alignment):
    result=copy.deepcopy(source);call=result['call'];schema=call['schema'];messages=call['messages'];user=next(x for x in messages if x['role']=='user');content=user['content']
    allowed=set(schema['properties']['relations']['items']['properties']['rel_type']['enum'])
    issues=json.loads(section(content,'repair issues').group(1));excluded=[]
    if alignment=='scope':
        eligible=[]
        for issue in issues:
            requested=set(issue.get('context',{}).get('expected_relations',[]))
            if requested and not requested.intersection(allowed):excluded.append(issue)
            else:eligible.append(issue)
        content=replace_section(content,'repair issues',json.dumps(eligible,ensure_ascii=False,sort_keys=True))
        signatures=section(content,'ontology relation signatures').group(1)
        kept=[line for line in signatures.splitlines() if any('`'+rid+'`' in line for rid in allowed)]
        content=replace_section(content,'ontology relation signatures','\n'.join(kept))
    elif alignment=='enum_only':
        required={r for issue in issues for r in issue.get('context',{}).get('expected_relations',[])}
        schema['properties']['relations']['items']['properties']['rel_type']['enum']=sorted(allowed|required)
        content=replace_section(content,'output schema',json.dumps(schema,ensure_ascii=False,sort_keys=True))
    elif alignment!='conflict':raise ValueError(alignment)
    if protocol=='clean':
        # Replace only collection-wrapper contract with the matching JSONL item contract.
        content=content.replace('输出必须是严格 JSON 对象；','输出必须遵循本次 JSONL 协议；')
        content=content.replace('输出必须是 JSON 对象；','输出必须遵循本次 JSONL 协议；')
        content=replace_section(content,'output schema',json.dumps(record_schema(schema),ensure_ascii=False,sort_keys=True))
        content=content.replace('## output schema','## record schema')
        content=content.replace('返回空数组','没有记录时返回一行 null').replace('返回空集合','没有记录时返回一行 null')
        content=re.sub(r'^- 只输出 JSON object。.*$', '输出遵循 JSONL：每行一条记录，不要外层集合；没有本次任务可执行且有证据的记录时只输出 null。',content,flags=re.M)
        for message in messages:
            if message['role']=='system':message['content']=message['content'].replace('输出必须是 JSON object，且只能包含本轮 schema 允许的顶层键。','输出必须遵循本次 JSONL 协议，记录只能包含记录 schema 允许的字段。')
    user['content']=content
    call['messages']=old_wire(messages,schema) if protocol=='legacy' else messages_for_jsonl(messages,schema)
    call['context']['response_mode']='frozen-wire'
    result['id']='repair-'+protocol+'-'+alignment
    result['derivation']={'source_case':source['id'],'protocol':protocol,'alignment':alignment,'excluded_issues':excluded,'endpoint_catalog':catalog(messages),'warning':'enum_only may permit nonexistent or mistyped endpoints; not a production fix'}
    for key in ['observed_metrics','observed_finish','observed_usage']:result.pop(key,None)
    return result

def entity_cases(source):
    whole=copy.deepcopy(source);user=next(x for x in whole['call']['messages'] if x['role']=='user');chunk=json.loads(section(user['content'],'current chunk').group(1));text=chunk['text'];cut=text.index('# 二、工作要求')
    title=next(line for line in text.splitlines() if line.startswith('# 关于'))
    result=[]
    for name,start,end in [('whole',0,len(text)),('segment-a',0,cut),('segment-b',cut,len(text))]:
        case=copy.deepcopy(source);call=case['call'];u=next(x for x in call['messages'] if x['role']=='user');value=copy.deepcopy(chunk);value['text']=text[start:end]
        u['content']=replace_section(u['content'],'current chunk',json.dumps(value,ensure_ascii=False,sort_keys=True))
        if name!='whole':
            u['content']=replace_section(u['content'],'local context','仅供辨识文件背景，不从本节新增事实：'+title)
        call['messages']=messages_for_jsonl(call['messages'],call['schema']);call['context']['response_mode']='frozen-wire';case['id']='entity-'+name
        case['derivation']={'source_case':source['id'],'source_range':[start,end],'source_total_chars':len(text),'context_title':title if name!='whole' else None,'target_load':'at most approximately 15–20 evidence-backed entities per semantic segment; provisional, checked against actual outputs','not_output_cap':True}
        for key in ['observed_metrics','observed_finish','observed_usage']:case.pop(key,None)
        result.append(case)
    return result

def build(source_dir,out):
    relation=json.loads((source_dir/'repair_relation-loop.json').read_text(encoding='utf8'));entity=json.loads((source_dir/'entity-loop.json').read_text(encoding='utf8'));out.mkdir(parents=True,exist_ok=True)
    cases=[relation_case(relation,p,a) for p in ['legacy','clean'] for a in ['conflict','scope']]
    cases.append(relation_case(relation,'legacy','enum_only'));cases.extend(entity_cases(entity));entries=[]
    for case in cases:
        data=json.dumps(case,ensure_ascii=False,indent=2)+'\n';(out/(case['id']+'.json')).write_text(data,encoding='utf8',newline='\n');entries.append({'id':case['id'],'sha256':hashlib.sha256(data.encode()).hexdigest()})
    plan=[]
    for repeat in [1,2]:
        sequence=['repair-legacy-conflict','repair-legacy-scope','repair-clean-conflict','repair-clean-scope','repair-legacy-enum_only']
        if repeat==2:sequence.reverse()
        plan.extend([[name,'baseline',repeat] for name in sequence])
    for repeat in [1,2,3]:
        sequence=['entity-whole','entity-segment-a','entity-segment-b']
        if repeat==2:sequence.reverse()
        plan.extend([[name,'baseline',repeat] for name in sequence])
    (out/'plan.json').write_text(json.dumps(plan,indent=2),encoding='utf8',newline='\n')
    (out/'manifest.json').write_text(json.dumps({'cases':entries,'model':'deepseek-ai/DeepSeek-V4-Flash','temperature':0,'max_tokens':16384,'thinking':{'type':'disabled'},'max_retries':0,'timeout_seconds':300,'historical_relation_source_commit':'65ee14f20199f52ec6ee9546c577a3027f5511a1 (a5e1555 merged)','label_status':'agent-reviewed / provisional; no independent human truth'},ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    return cases,plan

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,default=Path('test_data/repetition/v1'));p.add_argument('--out',type=Path,default=Path('test_data/repetition/causal-v1'));p.add_argument('--results',type=Path);p.add_argument('--report',type=Path);a=p.parse_args()
    if a.results:
        result=analyze(a.out,a.results,a.report or Path('tmp/repetition-causal-20261011/report.json'));print(json.dumps({'calls':result['calls'],'groups':result['groups'],'merged_segments':result['merged_segments']},ensure_ascii=False,indent=2))
    else:
        cases,plan=build(a.source,a.out);print(json.dumps({'cases':len(cases),'calls':len(plan),'out':str(a.out)}))
