"""Offline three-level information pilot; no extraction API or runtime policy changes."""
import argparse,collections,concurrent.futures,datetime,hashlib,json,os,pathlib,statistics,sys,time,urllib.request

INSTRUCTIONS={
 'zh':'判断给定文本的独立业务信息量。一台设备及其车站和状态合算一条记录；重复事实只算一次，字段名和年份不单独计数。连续正文中不同的业务任务、对象或规则各算一项。只判断给出的连续片段，不推断全文。不按字数或行数判断。',
 'en':'Classify the distinct business information in the supplied text. One device with its station and status is one record. Count repeated facts once; field names and years do not count separately. In prose, each distinct business task, object or rule is one item. Judge only the supplied contiguous passage, never infer the rest of the document. Do not count characters or lines.'}
CRITERIA={'zh':{'low':'0至3项不同业务信息','medium':'4至10项不同业务信息','high':'11项及以上不同业务信息'},
          'en':{'low':'0 to 3 distinct business information items','medium':'4 to 10 distinct business information items','high':'11 or more distinct business information items'}}
ORDERS=[['low','medium','high'],['high','medium','low'],['medium','low','high']]

def summarize(records,samples):
    by_id={s['id']:s for s in samples};result={}
    for model in sorted({r['model'] for r in records}):
        selected=[r for r in records if r['model']==model];valid=[r for r in selected if r['status']=='succeeded']
        first=[r for r in valid if r['order']==0 and r['language']=='zh' and by_id[r['id']]['kind']=='control']
        order_checks=[];language_checks=[]
        for sample in samples:
            for lang in INSTRUCTIONS:
                rr=[r for r in selected if r['id']==sample['id'] and r['language']==lang]
                if len(rr)==3 and all(r['status']=='succeeded' for r in rr):order_checks.append(len({r['label'] for r in rr})>1)
            rr=[r for r in valid if r['id']==sample['id'] and r['order']==0]
            if len(rr)==2:language_checks.append(len({r['label'] for r in rr})>1)
        result[model]={'requests':len(selected),'failed':len(selected)-len(valid),
            'control_first_correct':sum(r['label']==by_id[r['id']]['expected_density'] for r in first),'control_first_total':sum(s['kind']=='control' for s in samples),
            'order_drift':sum(order_checks),'order_cases':len(order_checks),'instruction_language_disagreement':sum(language_checks),'language_cases':len(language_checks),
            'choices':dict(collections.Counter(r['label'] for r in valid)),
            'request_seconds_total':sum(r['seconds'] for r in selected),'mean_seconds':statistics.mean(r['seconds'] for r in selected),
            'real_first_labels':{r['id']:r['label'] for r in valid if r['order']==0 and r['language']=='zh' and by_id[r['id']]['kind']=='real'}}
    return result

def run(model,url,key,samples,out,single=False):
    records=[];opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for sample in samples:
        for lang in (['zh'] if single else INSTRUCTIONS):
            for order,keys in enumerate(ORDERS[:1] if single else ORDERS):
                question={'type':'choice','instructions':INSTRUCTIONS[lang],'criteria':{k:CRITERIA[lang][k] for k in keys}}
                payload={'model':model,'state':sample['view'],'questions':{'density':question}}
                headers={'Content-Type':'application/json'}
                if key:headers['Authorization']='Bearer '+key
                row={'id':sample['id'],'model':model,'language':lang,'order':order,'status':'failed'};start=time.perf_counter()
                try:
                    req=urllib.request.Request(url.rstrip('/')+'/v1/systemone',data=json.dumps(payload,ensure_ascii=False).encode(),headers=headers)
                    with opener.open(req,timeout=60) as response:data=json.load(response)
                    answer=data['answers']['density'];label=answer['choice']
                    if label not in CRITERIA[lang]:raise ValueError('invalid choice')
                    row.update(status='succeeded',label=label,confidence=answer.get('confidence'),probabilities=answer.get('probabilities'))
                except Exception as exc:
                    row['error_type']=type(exc).__name__
                    if hasattr(exc,'code'):row['http_status']=exc.code
                row['seconds']=time.perf_counter()-start;records.append(row)
                (out/(model+'-results.json')).write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf8')
        print(datetime.datetime.now().isoformat(timespec='seconds'),model,len(records),sample['id'],flush=True)
    return records

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--dataset',type=pathlib.Path,required=True);p.add_argument('--output',type=pathlib.Path,required=True)
    p.add_argument('--nano-url');p.add_argument('--clef-url');p.add_argument('--kev-url');p.add_argument('--single',action='store_true');a=p.parse_args()
    services=[(m,u,k) for m,u,k in [('nanojev',a.nano_url,'NANOJEV_SERVICE_API_KEY'),('clef-flash',a.clef_url,'CLEF_SERVICE_API_KEY'),('kev-latest',a.kev_url,'KEV_SERVICE_API_KEY')] if u]
    if not services:p.error('at least one model URL is required')
    if 'tmp' not in a.output.parts:p.error('output must be under tmp/')
    a.output.mkdir(parents=True,exist_ok=False)
    samples=json.loads(a.dataset.read_text(encoding='utf-8-sig'))
    protocol={'instructions':INSTRUCTIONS,'criteria':CRITERIA,'orders':ORDERS[:1] if a.single else ORDERS,'languages':['zh'] if a.single else list(INSTRUCTIONS),'samples':len(samples),'dataset_sha256':hashlib.sha256(a.dataset.read_bytes()).hexdigest(),
              'notes':'Single-question Chinese/English instructions and option order checks. First control accuracy uses Chinese order0 only; real data has no verified density labels. No API extraction calls. Fixed maximum contiguous 1200-character views; control counts are exploratory.'}
    (a.output/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2),encoding='utf8')
    start=time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(services)) as pool:
        jobs=[pool.submit(run,m,u,os.getenv(k,''),samples,a.output,a.single) for m,u,k in services]
        records=[r for job in jobs for r in job.result()]
    summary={'models':summarize(records,samples),'wall_seconds':time.perf_counter()-start}
    (a.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(summary,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
