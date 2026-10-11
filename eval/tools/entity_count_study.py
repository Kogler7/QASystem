"""Evaluate count bands against frozen successful historical extraction proxies."""
import argparse,ast,collections,datetime,hashlib,json,os,pathlib,statistics,time,urllib.request

def question_templates():
    file=pathlib.Path(__file__).resolve().parents[2]/'algorithm/knowledge_pipeline/entity_count_estimator.py'
    tree=ast.parse(file.read_text(encoding='utf8'))
    direct=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='ENTITY_COUNT_QUESTION_ZH' for t in n.targets))
    english={'type':'choice','instructions':'Estimate how many distinct entities could be extracted from the entire supplied passage. Count real identifiable organizations, persons, equipment, places, documents, tasks and business rules. Count the same referent only once. Attributes, field names, years and repeated evidence are not separate entities. Count neither lines nor characters. Do not infer missing document content. Choose only the count interval.',
        'criteria':{'le20':'0 through 20 distinct entities, inclusive','21to40':'21 through 40 distinct entities, inclusive','gt40':'41 or more distinct entities'}}
    thresholds={f'gt{n}':{'type':'choice','instructions':english['instructions']+f' Does the estimated distinct entity count exceed {n}?',
                        'criteria':{'yes':f'The count is greater than {n}','no':f'The count is at most {n}'}} for n in [20,40]}
    return {'direct_zh':{'entity_count':direct},'direct_en':{'entity_count':english},'thresholds_en':thresholds}

def decode(answers,variant):
    if variant!='thresholds_en':
        label=answers['entity_count']['choice']
        if label not in ['le20','21to40','gt40']:raise ValueError('invalid count band')
        return label
    labels=[answers[k]['choice'] for k in ['gt20','gt40']]
    if any(x not in ['yes','no'] for x in labels):raise ValueError('invalid threshold answer')
    if labels==['no','yes']:raise ValueError('inconsistent thresholds')
    return 'le20' if labels==['no','no'] else 'gt40' if labels==['yes','yes'] else '21to40'

def summarize(records,rows):
    expected={r['id']:r for r in rows};out={};rank={'le20':0,'21to40':1,'gt40':2}
    for variant in question_templates():
        rr=[r for r in records if r['variant']==variant];valid=[r for r in rr if r['status']=='succeeded']
        confusion={e:{p:0 for p in rank} for e in rank}
        for r in valid:confusion[expected[r['id']]['expected_bucket']][r['bucket']]+=1
        per_bucket={b:(confusion[b][b]/sum(confusion[b].values()) if sum(confusion[b].values()) else None) for b in rank}
        out[variant]={'total':len(rr),'failed':len(rr)-len(valid),'correct':sum(r['bucket']==expected[r['id']]['expected_bucket'] for r in valid),
                     'accuracy':sum(r['bucket']==expected[r['id']]['expected_bucket'] for r in valid)/len(rr) if rr else None,
                     'under_split':sum(rank[r['bucket']]<rank[expected[r['id']]['expected_bucket']] for r in valid),
                     'over_split':sum(rank[r['bucket']]>rank[expected[r['id']]['expected_bucket']] for r in valid),
                     'confusion':confusion,'recall_by_bucket':per_bucket,'mean_seconds':statistics.mean(r['seconds'] for r in rr) if rr else None,
                     'request_seconds_total':sum(r['seconds'] for r in rr)}
    return out

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--dataset',type=pathlib.Path,required=True);p.add_argument('--output',type=pathlib.Path,required=True)
    p.add_argument('--model',required=True);p.add_argument('--url',required=True);p.add_argument('--key-env',default='');a=p.parse_args()
    rows=[json.loads(line) for line in a.dataset.read_text(encoding='utf8').splitlines() if line.strip()]
    if 'tmp' not in a.output.parts:p.error('output must be under tmp/')
    a.output.mkdir(parents=True,exist_ok=False);questions=question_templates()
    protocol={'dataset_sha256':hashlib.sha256(a.dataset.read_bytes()).hexdigest(),'questions':questions,'model':a.model,'samples':len(rows),'notes':'Frozen historical extraction-count proxies, not independent human truth. Same full chunk, no labels/entities in model input. No extraction API calls; no retries.'}
    (a.output/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2),encoding='utf8')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));records=[];wall_start=time.perf_counter()
    for variant,q in questions.items():
        for row in rows:
            payload={'model':a.model,'state':row['text'],'questions':q};headers={'Content-Type':'application/json'}
            if a.key_env and os.getenv(a.key_env):headers['Authorization']='Bearer '+os.environ[a.key_env]
            record={'id':row['id'],'variant':variant,'status':'failed'};start=time.perf_counter()
            try:
                req=urllib.request.Request(a.url.rstrip('/')+'/v1/systemone',data=json.dumps(payload,ensure_ascii=False).encode(),headers=headers)
                with opener.open(req,timeout=60) as response:data=json.load(response)
                answers=data['answers']
                if set(answers)!=set(q):raise ValueError('missing answers')
                record.update(status='succeeded',bucket=decode(answers,variant),answers=answers)
            except Exception as exc:
                record['error_type']=type(exc).__name__
                if hasattr(exc,'code'):record['http_status']=exc.code
            record['seconds']=time.perf_counter()-start;records.append(record)
            (a.output/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf8')
        print(datetime.datetime.now().isoformat(timespec='seconds'),a.model,variant,'done',flush=True)
    by_split={}
    for split in sorted({row.get('split','unspecified') for row in rows}):
        selected=[row for row in rows if row.get('split','unspecified')==split]
        ids={row['id'] for row in selected}
        by_split[split]=summarize([r for r in records if r['id'] in ids],selected)
    summary={'variants':summarize(records,rows),'by_split':by_split,'wall_seconds':time.perf_counter()-wall_start,'model':a.model}
    (a.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(summary,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
