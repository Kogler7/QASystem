"""Publish a small reviewed corpus from harvested real pairs and explicit labels."""
from __future__ import annotations
import argparse
import collections
import copy
import datetime
import hashlib
import json
from pathlib import Path
import random
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from eval import run as ev
from eval.judge_dataset import SCHEMA_VERSION,fingerprint,read_jsonl,validate,write_jsonl


def build(material,annotation_paths,output):
    selected=list(read_jsonl(material/'selected.jsonl'))
    for name in ['numeric_real.jsonl','direction_real.jsonl']:
        if (material/name).exists():selected+=list(read_jsonl(material/name))
    annotations={r['id']:r for p in annotation_paths for r in read_jsonl(p)}
    sources=json.loads((material/'sources.json').read_text(encoding='utf8'))
    chosen=[];pending=[];excluded=[];counts=collections.Counter()
    for original in selected:
        row=copy.deepcopy(original);annotation=annotations.get(row['id']);relation=row['kind']=='relation'
        if not ev._compatible_types(row['golden'],row['predicted'],relation=relation):
            excluded.append({'id':row['id'],'reason':'本体类型不兼容，由程序类型过滤确定性处理，不纳入 judge 语义性能题集。'})
            continue
        if annotation:
            if annotation['expected_match'] is None:
                pending.append({**row,'review_note':annotation['rationale']});continue
            expected=annotation['expected_match'];label={'source':'agent_review','reviewer':annotation['reviewer'],'rationale':annotation['rationale']};capabilities=annotation['capabilities']
        elif ev._auto_candidate_pairs([row['golden']],[row['predicted']],relation=relation):
            if counts[(row['document_id'],row['kind'],'auto')]>=1:continue
            counts[(row['document_id'],row['kind'],'auto')]+=1
            expected=True;label={'source':'contract_rule','reviewer':'deterministic_contract','rationale':'身份仅有契约允许的格式差异；双方已填写的共享属性归一值一致，关系端点已提供上下文无冲突。'};capabilities=['identity_format','property_missing']
        else:continue
        row.pop('proposal',None);row.update(expected_match=expected,label=label,capabilities=capabilities)
        # Length is descriptive, not a token estimate or a truncation policy.
        row['input_characters']=len(json.dumps([row['golden'],row['predicted']],ensure_ascii=False))
        if row['input_characters']>3000:row['capabilities']=sorted(set(row['capabilities']+['long_context']))
        chosen.append(row)
    unique={}
    for row in chosen:
        h=fingerprint({k:row[k] for k in ['kind','golden','predicted']})
        if h in unique:
            if unique[h]['expected_match']!=row['expected_match']:raise ValueError('conflicting labels for same input')
            continue
        unique[h]=row
    chosen=list(unique.values())
    documents=sorted({r['document_id'] for r in chosen})
    # Conservatively keep old and new fault-rule case names together.
    families=sorted({('rule' if d.startswith('rule/') else d) for d in documents})
    random.Random(20261011).shuffle(families)
    calibration=set(families[:max(1,len(families)//4)])
    for row in chosen:
        family='rule' if row['document_id'].startswith('rule/') else row['document_id']
        row['document_family']=family;row['split']='calibration' if family in calibration else 'test'
    chosen.sort(key=lambda r:(r['split'],r['document_id'],r['kind'],r['id']))
    windows=[]
    # Type-only 2x2 windows are program contract fixtures, not judge quality tasks.
    for r in list(read_jsonl(material/'pending.jsonl'))[:24]:pending.append({**r,'review_note':'未标注，不参加性能准确率汇总。'})
    output.mkdir(parents=True,exist_ok=True)
    write_jsonl(output/'pairs.jsonl',chosen);write_jsonl(output/'windows.jsonl',windows)
    write_jsonl(output/'review_queue.jsonl',pending)
    (output/'excluded_samples.json').write_text(json.dumps({'pairs':excluded,'windows':[],
        'reason':'程序类型过滤题排除；不自动构造跨类型窗口。'},ensure_ascii=False,indent=2),encoding='utf8')
    used={o['source_id'] for r in chosen+windows+pending for o in r['origins']}
    registry={r['source_id']:r for r in sources if r['source_id'] in used}
    (output/'sources.json').write_text(json.dumps(list(registry.values()),ensure_ascii=False,indent=2),encoding='utf8')
    summary={'pairs':len(chosen),'windows':len(windows),'pending':len(pending),'documents':len(documents),'document_families':len(families),'source_snapshots':len(registry),
             'labels':dict(collections.Counter(r['label']['source'] for r in chosen)), 'positive':sum(r['expected_match'] for r in chosen),
             'negative':sum(not r['expected_match'] for r in chosen),'by_split':dict(collections.Counter(r['split'] for r in chosen)),
             'by_kind':dict(collections.Counter(r['kind'] for r in chosen)),'capabilities':dict(collections.Counter(c for r in chosen for c in r['capabilities']))}
    manifest={'schema_version':SCHEMA_VERSION,'version':'1.1.0','created_date':'2026-10-11','label_contract':'eval/run.py _build_judge_messages: reference-relative semantic matching',
              'adjudication':'agent-reviewed initial calibration labels + deterministic contract slice; no independent human verification yet; historical judge choices never become labels',
              'harvest':json.loads((material/'harvest_summary.json').read_text(encoding='utf8')),'summary':summary,'files':{name:hashlib.sha256((output/name).read_bytes()).hexdigest() for name in ['pairs.jsonl','windows.jsonl','review_queue.jsonl','sources.json','excluded_samples.json']}}
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    validate(output);print(json.dumps(summary,ensure_ascii=False,indent=2));return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--material',type=Path,required=True)
    parser.add_argument('--annotations',nargs='+',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('test_data/judge/v1'))
    args=parser.parse_args();build(args.material,args.annotations,args.output)
