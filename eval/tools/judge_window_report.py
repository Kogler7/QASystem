"""Summarize frozen window runs without additional judge calls."""
import argparse,json,pathlib,statistics
from eval.tools.judge_window_compare import summarize

def report(dataset,results):
    rows=[json.loads(x) for x in dataset.read_text(encoding='utf8').splitlines() if x.strip()]
    reports={};baseline={}
    for path in sorted(results.glob('*/calls.jsonl')):
        records=[json.loads(x) for x in path.read_text(encoding='utf8').splitlines() if x.strip()]
        if len(records)!=4*len(rows):continue
        summary=summarize(records,rows)
        protocols=json.loads((path.parent/'protocol.json').read_text(encoding='utf8'))
        if protocols['dataset_sha256']!=__import__('hashlib').sha256(dataset.read_bytes()).hexdigest():raise ValueError('dataset changed after calls')
        changes=[];candidate_delta=[]
        for row in rows:
            rr={r['round']:r for r in records if r['id']==row['id']}
            if all(rr[i]['status']=='succeeded' for i in [0,3]):
                before=len(rr[0]['matched_pairs']);after=len(rr[3]['matched_pairs'])
                if before!=after:changes.append({'id':row['id'],'before':before,'after':after})
                candidate_delta.append(after-before)
        summary['order_matched_count_changes']=changes
        summary['mean_order_matched_count_delta']=statistics.mean(candidate_delta) if candidate_delta else None
        source=json.loads((path.parent/'summary.json').read_text(encoding='utf8'))
        summary['wall_seconds']=source['wall_seconds'];summary['http_budget_used']=source['http_budget_used']
        summary['round_wall_seconds']=source['round_wall_seconds']
        reports[path.parent.name]=summary
        baseline[path.parent.name]={r['id']:r for r in records if r['round']==0}
    common=[row['id'] for row in rows if baseline and all(row['id'] in rr and rr[row['id']]['status']=='succeeded' for rr in baseline.values())]
    for model,rr in baseline.items():
        reports[model]['common_successful_first_round_windows']=common
        reports[model]['common_successful_first_round_mean_seconds']=statistics.mean(rr[i]['seconds'] for i in common) if common else None
    return reports

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--dataset',type=pathlib.Path,required=True);p.add_argument('--results',type=pathlib.Path,required=True)
    a=p.parse_args();data=report(a.dataset,a.results)
    (a.results/'comparison.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
    for name,r in data.items():
        print(name,json.dumps({k:r[k] for k in ['quality_semantic_labels_first_round','failed_windows','same_input_drift_windows','incomplete_stability_windows','first_round_mean_window_seconds','common_successful_first_round_mean_seconds','order_matched_count_changes','http_budget_used']},ensure_ascii=False))
if __name__=='__main__':main()
