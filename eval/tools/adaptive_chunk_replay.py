"""Collect exact classifier probes and replay frozen decisions without model calls."""
import argparse,collections,hashlib,json,pathlib,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[2]))
from eval.tools.adaptive_chunk_probe import plan_adaptive,_split

def probe_id(text):return 'probe-'+hashlib.sha256(text.encode()).hexdigest()[:24]

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--dataset',type=pathlib.Path,required=True);p.add_argument('--output',type=pathlib.Path,required=True);p.add_argument('--decisions',type=pathlib.Path);a=p.parse_args()
    if 'tmp' not in a.output.parts:p.error('output must be under tmp/')
    a.output.mkdir(parents=True,exist_ok=True)
    samples=[s for s in json.loads(a.dataset.read_text(encoding='utf8')) if s['kind']=='real'];probes={};plans={}
    def collect(text):
        ident=probe_id(text);probes[ident]={'id':ident,'kind':'real','text':text,'view':text,'expected_density':None}
        return 'medium'
    for s in samples:plan_adaptive(s['text'],collect)
    (a.output/'probes.json').write_text(json.dumps(list(probes.values()),ensure_ascii=False,indent=2),encoding='utf8')
    if not a.decisions:
        print('unique_probes',len(probes));return
    summary={}
    for model in sorted(path.name.removesuffix('-results.json') for path in a.decisions.glob('*-results.json')):
        records=json.loads((a.decisions/(model+'-results.json')).read_text(encoding='utf8'));by_id={r['id']:r for r in records}
        def classify(text):
            r=by_id[probe_id(text)]
            if r['status']!='succeeded':raise RuntimeError('classification failed')
            return {'label':r['label'],'confidence':r.get('confidence')}
        plans[model]={s['id']:plan_adaptive(s['text'],classify) for s in samples}
        flattened=[c for chunks in plans[model].values() for c in chunks]
        for sample in samples:
            chunks=plans[model][sample['id']];covered=[False]*len(sample['text'])
            for chunk in chunks:
                left,right=chunk['range'];assert sample['text'][left:right]==chunk['text']
                covered[left:right]=[True]*(right-left)
            assert all(covered)
        summary[model]={'inputs':len(samples),'chunks':len(flattened),'protected_chunks':sum(c['protected'] for c in flattened),
            'chunk_density':dict(collections.Counter(c['density'] for c in flattened)),
            'fallback_chunks':sum(bool(c['fallback_reason']) for c in flattened),
            'source_coverage_verified':True,'classification_calls':len(records),'classification_seconds':sum(r['seconds'] for r in records)}
    summary['baseline']={'chunks':sum(len(_split(s['text'],1200,50)) for s in samples),'base_size':1200,'overlap':50,
        'notes':'Same source-aware character splitter; no extraction, no F1 or optimum size claimed.'}
    (a.output/'plans.json').write_text(json.dumps(plans,ensure_ascii=False,indent=2),encoding='utf8')
    (a.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(summary,ensure_ascii=False))
if __name__=='__main__':main()
