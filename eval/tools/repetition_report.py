"""Recompute frozen replay metrics and simulate stopping on saved output (no API)."""
from __future__ import annotations
import argparse,collections,json
from pathlib import Path
from eval.tools.repetition_study import metrics,quality_metrics
from algorithm.knowledge_extract.jsonl_protocol import decode_jsonl
from algorithm.knowledge_extract.v3.structured_output import validate_json_schema

def prompt_contract_audit(case):
    call=case['call'];item=call.get('schema',{}).get('properties',{}).get('relations',{}).get('items',{})
    relation_field=item.get('properties',{}).get('rel_type',{});allowed=set(relation_field.get('enum',[]));issues=[]
    for message in call['messages']:
        content=message.get('content','');heading='## repair issues'
        if heading not in content:continue
        try:parsed=json.loads(content.split(heading,1)[1].strip())
        except ValueError:continue
        if isinstance(parsed,list):issues.extend(parsed)
    required=set(r for issue in issues for r in issue.get('context',{}).get('expected_relations',[]))
    return {'case':case['id'],'required_relations':sorted(required),'schema_allowed_relations':sorted(allowed),'unemittable_required_relations':sorted(required-allowed) if 'enum' in relation_field else [],'issue_count':len(issues)}

def simulate_guard(text,step=256):
    complete=metrics(text)
    for end in range(step,len(text)+step,step):
        end=min(end,len(text));prefix=text[:end];current=metrics(prefix)
        if current['max_exact_multiplicity']>=3 or current['repeated_substring_chars']>=400:
            return {'trigger_chars':end,'full_chars':len(text),'emitted_character_reduction':1-end/len(text),'recoverable_unique_before':current['unique_records'],'recoverable_unique_full':complete['unique_records'],'unique_retention':current['unique_records']/complete['unique_records'] if complete['unique_records'] else None,'reason':'exact_record_third_occurrence' if current['max_exact_multiplicity']>=3 else 'repeated_substring','simulation_only':True}
    return {'trigger_chars':None,'full_chars':len(text),'simulation_only':True}

def report(dataset,replays,historical,out):
    rows=[];guards=[]
    for p in sorted(replays.glob('*/*--*.json')):
        d=json.loads(p.read_text(encoding='utf8'));row=dict(d['summary']);case=json.loads((dataset/(row['case']+'.json')).read_text(encoding='utf8'))
        decoded,errors=decode_jsonl(d['response_text'],case['call']['schema'])
        row.update(metrics(d['response_text']));row.pop('semantic_unique',None);row.pop('semantic_duplicate_fraction',None)
        row.update(quality_metrics(d['response_text'],d['request']['messages'],case['reference_anchors']))
        row.update(phase=p.parent.name,max_tokens=d['request']['max_tokens'],parse_errors=errors,schema_error_count=len(validate_json_schema(decoded,case['call']['schema'])),artifact=str(p))
        row['historical_prompt_tokens']=(case.get('observed_usage') or {}).get('prompt_tokens');rows.append(row)
        guard=simulate_guard(d['response_text']);
        if guard['trigger_chars']:guards.append({'case':row['case'],'method':row['method'],'repeat':row['repeat'],'phase':row['phase'],**guard})
    for p in sorted(historical.glob('*loop.json')):
        d=json.loads(p.read_text(encoding='utf8'));guards.append({'case':d['id'],'phase':'historical',**simulate_guard(d['observed_response']['text'])})
    groups=[];grouped=collections.defaultdict(list)
    for row in rows:grouped[(row['case'],row['method'],row['max_tokens'])].append(row)
    for (case,method,cap),group in grouped.items():
        valid=[r for r in group if not r['error']];token_values=[r['usage']['completion_tokens'] for r in valid if r.get('usage')]
        groups.append({'case':case,'method':method,'max_tokens':cap,'attempts':len(group),'responses':len(valid),'errors':len(group)-len(valid),'length':sum(r['finish_reason']=='length' for r in valid),'any_exact_duplicate':sum(r['duplicate_fraction']>0 for r in valid),'any_identity_duplicate':sum(bool(r['identity_duplicate_fraction']) for r in valid),'substring_loop':sum(r['repeated_substring_chars']>=400 for r in valid),'record_range':[min((r['records'] for r in valid),default=0),max((r['records'] for r in valid),default=0)],'completion_token_range':[min(token_values,default=0),max(token_values,default=0)],'anchor_hits':sum(r['anchor_hits'] for r in valid),'anchor_count':sum(r['anchor_count'] for r in valid)})
    contract_audits=[prompt_contract_audit(json.loads(p.read_text(encoding='utf8'))) for p in dataset.glob('*.json') if p.stem!='manifest']
    result={'rows':rows,'groups':groups,'offline_guards':guards,'prompt_contract_audits':contract_audits,'completed_calls':len(rows),'responses':sum(not r['error'] for r in rows),'errors':sum(bool(r['error']) for r in rows),'length':sum(r['finish_reason']=='length' for r in rows),'observed_completion_tokens':sum((r.get('usage') or {}).get('completion_tokens',0) for r in rows),'limitations':['Small convenience corpus; no population failure-rate estimate.','8192 screening and 16384 confirmation are separate conditions.','Identity dedup is exact key equality, not semantic equivalence.','Natural corpus has sparse agent-reviewed provisional anchors, with no independent human gold or review; not full gold quality/F1.','Guard reductions are offline emission simulations, not observed billing reductions.','Concurrent server experiments and provider latency confound timing.','Legacy property JSON-schema API was not re-enabled; prompt-only replay is not the original structured-output API.']}
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8',newline='\n');print(json.dumps({k:v for k,v in result.items() if k not in {'rows','groups'}},ensure_ascii=False,indent=2));return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--dataset',type=Path,required=True);p.add_argument('--replays',type=Path,required=True);p.add_argument('--historical',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args();report(a.dataset,a.replays,a.historical,a.out)
