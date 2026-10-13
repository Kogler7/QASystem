"""Compare chat judges through the formal scorer on frozen real windows."""
from __future__ import annotations
import argparse, concurrent.futures, hashlib, json, os, pathlib, statistics, sys, threading, time
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[2]))
from eval import run

class BudgetClient(run.JudgeClient):
    lock=threading.Lock()
    used=0
    limit=140
    def _post_chat(self,*args,**kwargs):
        with self.lock:
            if type(self).used>=type(self).limit:raise RuntimeError('request budget exhausted')
            type(self).used+=1
        return super()._post_chat(*args,**kwargs)

def evaluate(row,client,round_index):
    golden=row['golden_items'];predicted=row['predicted_items']
    gi=list(range(len(golden)));pi=list(range(len(predicted)))
    if round_index==3:
        gi.reverse();pi.reverse()
    gs=[golden[i] for i in gi];ps=[predicted[i] for i in pi]
    started=time.perf_counter()
    result={'id':row['id'],'document_id':row['document_id'],'round':round_index,
            'condition':'reversed_item_order' if round_index==3 else 'same_input',
            'status':'failed','golden_count':len(gs),'predicted_count':len(ps)}
    candidates=[]
    try:
        matched,batches,coverage=run._judge_collection(client,row['collection'],gs,ps,
            batch_size=20,golden_batch_size=40,max_concurrency=1,candidate_pairs_out=candidates)
        result.update(status='succeeded',candidate_pairs=sorted({(gi[g],pi[p]) for g,p in candidates}),
                      matched_pairs=sorted((gi[g],pi[p]) for g,p in matched),coverage=coverage,batches=batches)
    except Exception as exc:
        result.update(error_type=type(exc).__name__,batches=getattr(exc,'judge_batches',[]))
    result['seconds']=time.perf_counter()-started
    result['http_attempts']=sum(len(b.get('attempts',[])) for b in result['batches'])
    return result

def quality(records,rows):
    lookup={r['id']:r for r in rows};counts={'tp':0,'tn':0,'fp':0,'fn':0,'failed_labels':0}
    for r in records:
        for label in lookup[r['id']]['labels']:
            if r['status']!='succeeded':counts['failed_labels']+=1;continue
            expected=label['expected_match'];got=[label['golden_index'],label['predicted_index']] in [list(p) for p in r['candidate_pairs']]
            counts['tp' if expected and got else 'fn' if expected else 'fp' if got else 'tn']+=1
    total=sum(counts.values());correct=counts['tp']+counts['tn']
    positive=counts['tp']+counts['fn'];negative=counts['tn']+counts['fp']
    return {**counts,'labels':total,'accuracy_including_failures':correct/total if total else None,
            'positive_recall':counts['tp']/positive if positive else None,
            'negative_recall':counts['tn']/negative if negative else None}

def summarize(records,rows):
    first=[r for r in records if r['round']==0]
    same=[r for r in records if r['round']<3]
    drift=[];order=[];failed_stability=[];count_changes=[];order_evaluated=[];order_failed=[]
    for row in rows:
        rr=[r for r in same if r['id']==row['id']]
        baseline=next((r for r in records if r['id']==row['id'] and r['round']==0),None)
        reverse=next((r for r in records if r['id']==row['id'] and r['round']==3),None)
        if baseline and reverse and baseline['status']==reverse['status']=='succeeded':
            order_evaluated.append(row['id'])
            if set(map(tuple,reverse['candidate_pairs']))!=set(map(tuple,baseline['candidate_pairs'])):order.append(row['id'])
        else:order_failed.append(row['id'])
        if len(rr)!=3 or any(r['status']!='succeeded' for r in rr):failed_stability.append(row['id']);continue
        sets={tuple(sorted(tuple(p) for p in r['candidate_pairs'])) for r in rr}
        if len(sets)>1:drift.append(row['id'])
        ns=[len(r['matched_pairs']) for r in rr]
        if max(ns)!=min(ns):count_changes.append({'id':row['id'],'matched_counts':ns})
    complete=[]
    lookup={r['id']:r for r in rows}
    for record in records:
        row=lookup[record['id']]
        if 'expected_pairs' not in row or record['status']!='succeeded':continue
        reference=len(run._maximum_matching([tuple(p) for p in row['expected_pairs']]))
        found=len(record['matched_pairs']);ng=len(row['golden_items']);np=len(row['predicted_items'])
        expected_f1=2*reference/(ng+np) if ng+np else 0
        got_f1=2*found/(ng+np) if ng+np else 0
        complete.append({'id':row['id'],'round':record['round'],'reference_matched':reference,
                         'model_matched':found,'reference_f1':expected_f1,'model_f1':got_f1,
                         'f1_signed_bias':got_f1-expected_f1})
    latencies=sorted(r['seconds'] for r in first if r['status']=='succeeded')
    semantic=[]
    for row in rows:
        if 'golden_items' not in row:semantic.append(row);continue
        auto=set(run._auto_candidate_pairs(row['golden_items'],row['predicted_items'],relation=row['collection']=='relation_matches'))
        semantic.append({**row,'labels':[x for x in row['labels'] if (x['golden_index'],x['predicted_index']) not in auto]})
    return {'quality_first_round':quality(first,rows),
            'quality_semantic_labels_first_round':quality(first,semantic),
            'quality_first_round_model_required_windows':quality([r for r in first if r['http_attempts']>0],rows),
            'quality_by_round':{str(i):quality([r for r in records if r['round']==i],rows) for i in sorted({r['round'] for r in records})},
            'quality_by_collection':{c:quality([r for r in first if next(x for x in rows if x['id']==r['id'])['collection']==c],rows) for c in sorted({r['collection'] for r in rows})},
            'windows':len(rows),'failed_windows':sum(r['status']!='succeeded' for r in records),
            'same_input_drift_windows':drift,'item_order_sensitive_windows':order,'incomplete_stability_windows':failed_stability,
            'item_order_evaluated_windows':order_evaluated,'item_order_incomplete_windows':order_failed,
            'matched_count_drift':count_changes,'request_attempts':sum(r['http_attempts'] for r in records),
            'complete_reviewed_window_scores':complete,
            'automatic_only_first_round_windows':[r['id'] for r in first if r['http_attempts']==0 and r['status']=='succeeded'],
            'first_round_seconds_total':sum(r['seconds'] for r in first),
            'first_round_mean_window_seconds':statistics.mean(r['seconds'] for r in first) if first else None,
            'first_round_median_window_seconds':statistics.median(latencies) if latencies else None,
            'first_round_p95_window_seconds':latencies[min(len(latencies)-1,int((len(latencies)-1)*.95+.999))] if latencies else None,
            'all_rounds_seconds_total':sum(r['seconds'] for r in records)}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=pathlib.Path,required=True);p.add_argument('--output',type=pathlib.Path,required=True)
    p.add_argument('--model',required=True);p.add_argument('--url',required=True);p.add_argument('--key-env',default='')
    p.add_argument('--workers',type=int,default=2);p.add_argument('--budget',type=int,default=140)
    a=p.parse_args();rows=[json.loads(x) for x in a.dataset.read_text(encoding='utf8').splitlines() if x.strip()]
    if 'tmp' not in a.output.parts:p.error('output must be under tmp/')
    a.output.mkdir(parents=True,exist_ok=False)
    BudgetClient.limit=a.budget
    BudgetClient.used=0
    protocol={'model':a.model,'dataset_sha256':hashlib.sha256(a.dataset.read_bytes()).hexdigest(),
        'eval_run_sha256':hashlib.sha256(pathlib.Path(run.__file__).read_bytes()).hexdigest(),
        'windows':len(rows),'rounds':4,'window_sizes':[40,20],'workers':a.workers,
        'max_tokens':8192,'response_format':'auto','temperature':0,'disable_thinking':True,
        'http_attempt_budget':a.budget,'scope':'Formal _judge_collection on genuine frozen windows, including automatic pairs, focused judgement, type filter and maximum matching. Accuracy only on independently frozen labeled edges; unlabeled returned edges are not assumed correct. No re-extraction.'}
    (a.output/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2),encoding='utf8')
    records=[];started=time.perf_counter();round_wall={}
    def execute(job):
        row,i=job
        client=BudgetClient(base_url=a.url,model=a.model,api_key=os.getenv(a.key_env,'') if a.key_env else '',
            timeout=180,max_tokens=8192,response_format='auto',disable_thinking=True,adapter='chat')
        return evaluate(row,client,i)
    with (a.output/'calls.jsonl').open('w',encoding='utf8') as stream:
        for i in range(4):
            begin=time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
                for record in pool.map(execute,[(row,i) for row in rows]):
                    records.append(record);stream.write(json.dumps(record,ensure_ascii=False)+'\n');stream.flush()
                    print(time.strftime('%Y-%m-%d %H:%M:%S'),a.model,'round',i,record['id'],record['status'],record['http_attempts'],flush=True)
            round_wall[str(i)]=time.perf_counter()-begin
    summary=summarize(records,rows);summary.update(wall_seconds=time.perf_counter()-started,round_wall_seconds=round_wall,http_budget_used=BudgetClient.used)
    (a.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(summary,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
