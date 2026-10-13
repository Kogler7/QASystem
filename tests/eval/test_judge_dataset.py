import copy
import json
from pathlib import Path

import pytest

from eval.judge_dataset import classification, fingerprint, read_jsonl, validate, write_jsonl
from eval.run import _compatible_types
from eval.tools.build_judge_dataset import build
from eval.tools.judge_benchmark import messages_for, runtime_metrics

DATASET=Path(__file__).resolve().parents[2]/"test_data/judge/v1"
TYPE_CONTRACT=DATASET.parents[1]/'eval/judge-type-contract'


def test_frozen_corpus_integrity_and_real_provenance():
    result=validate(DATASET)
    assert result['documents']>=15
    assert result['pairs']>=100
    rows=list(read_jsonl(DATASET/'pairs.jsonl'))
    assert all(r['origins'] for r in rows)
    assert all('proposal' not in r for r in rows)
    assert {'entity','relation'}=={r['kind'] for r in rows}
    assert {'numeric_conflict','different_identity','endpoint_context'}<={c for r in rows for c in r['capabilities']}
    assert all(_compatible_types(r['golden'],r['predicted'],relation=r['kind']=='relation') for r in rows)
    assert all('type_constraint' not in r['capabilities'] for r in rows)
    assert not list(read_jsonl(DATASET/'windows.jsonl'))
    manifest=json.loads((DATASET/'manifest.json').read_text())
    assert manifest['version']=='1.1.1'
    assert manifest['summary']['pairs']==len(rows)==103


def test_frozen_exclusions_use_cross_platform_lf_bytes():
    assert b'\r\n' not in (DATASET / 'excluded_samples.json').read_bytes()


def test_hash_tampering_is_rejected(tmp_path):
    for f in DATASET.iterdir():
        if f.is_file():(tmp_path/f.name).write_bytes(f.read_bytes())
    with (tmp_path/'pairs.jsonl').open('a',encoding='utf8') as handle:handle.write('{}\n')
    with pytest.raises(ValueError,match='hash mismatch'):validate(tmp_path)


def test_failed_requests_never_turn_into_true_negatives():
    scores=classification([{'expected':False,'predicted':False,'status':'failed'},
                           {'expected':True,'predicted':True,'status':'succeeded'}])
    assert scores['tn']==0
    assert scores['coverage']==.5
    assert scores['accuracy_valid']==1
    assert scores['accuracy_end_to_end']==.5
    assert scores['balanced_accuracy'] is None


def test_balanced_accuracy_exposes_all_yes_judge():
    scores=classification([{'expected':x,'predicted':True,'status':'succeeded'} for x in [True,True,True,False]])
    assert scores['accuracy_valid']==.75
    assert scores['balanced_accuracy']==.5
    assert scores['specificity']==0


def test_prompts_exclude_labels_reviewer_and_history():
    row=next(read_jsonl(DATASET/'pairs.jsonl'))
    row=copy.deepcopy(row);row['label']['rationale']='SECRET_LABEL_SENTINEL'
    row['expected_match']=False
    messages,collection,ng,np=messages_for(row)
    text=json.dumps(messages,ensure_ascii=False)
    assert 'SECRET_LABEL_SENTINEL' not in text
    assert 'expected_match' not in text
    assert ng==np==1
    assert collection in ['entity_matches','relation_matches']


def test_pending_labels_are_not_scored_or_claimed_human():
    scored=list(read_jsonl(DATASET/'pairs.jsonl'))
    pending=list(read_jsonl(DATASET/'review_queue.jsonl'))
    assert {r['id'] for r in scored}.isdisjoint(r['id'] for r in pending)
    assert all(r['label']['source']!='human_review' for r in scored)
    assert any('未标注' in r['review_note'] for r in pending)


def test_complete_window_labels_match_all_expected_edges():
    windows=list(read_jsonl(TYPE_CONTRACT/'windows.jsonl'))
    assert len(windows)==4
    for row in windows:
        expected={(x['golden_index'],x['predicted_index']) for x in row['cell_labels'] if x['expected_match']}
        assert expected=={tuple(p) for p in row['expected_pairs']}
        assert len(row['cell_labels'])==len(row['golden_items'])*len(row['predicted_items'])


def test_removed_type_contract_pairs_remain_program_regressions():
    rows=list(read_jsonl(TYPE_CONTRACT/'pairs.jsonl'))
    assert len(rows)==28
    assert all(not r['expected_match'] for r in rows)
    assert all(not _compatible_types(r['golden'],r['predicted'],relation=r['kind']=='relation') for r in rows)
    retained=list(read_jsonl(DATASET/'pairs.jsonl'))
    assert {r['id'] for r in rows}.isdisjoint(r['id'] for r in retained)
    removal=json.loads((DATASET/'excluded_samples.json').read_text())
    assert {r['id'] for r in removal['pairs']}=={r['id'] for r in rows}


def test_builder_excludes_cross_types_even_when_annotated_and_builds_no_type_windows(tmp_path):
    positives=[r for r in read_jsonl(DATASET/'pairs.jsonl') if r['expected_match'] and r['kind']=='entity']
    a,b=next((a,b) for a in positives for b in positives if a['document_id']==b['document_id'] and a['golden']['type']!=b['golden']['type'])
    excluded=next(read_jsonl(TYPE_CONTRACT/'pairs.jsonl'))
    selected=[a,b,excluded]
    write_jsonl(tmp_path/'selected.jsonl',selected)
    write_jsonl(tmp_path/'pending.jsonl',[])
    write_jsonl(tmp_path/'annotations.jsonl',[{'id':r['id'],'expected_match':True,
                 'rationale':'Explicit test label','reviewer':'test','capabilities':['paraphrase']} for r in selected])
    (tmp_path/'sources.json').write_bytes((DATASET/'sources.json').read_bytes())
    (tmp_path/'harvest_summary.json').write_text('{}')
    output=tmp_path/'output'
    summary=build(tmp_path,[tmp_path/'annotations.jsonl'],output)
    assert summary['pairs']==2
    assert summary['windows']==0
    assert {r['id'] for r in read_jsonl(output/'pairs.jsonl')}=={a['id'],b['id']}
    assert json.loads((output/'excluded_samples.json').read_text())['pairs'][0]['id']==excluded['id']


def test_runtime_counts_requests_and_case_totals_across_rounds():
    records=[{'id':'a','round':0,'seconds':2,'status':'succeeded','predicted_pairs':[]},
             {'id':'b','round':0,'seconds':4,'status':'failed'},
             {'id':'a','round':1,'seconds':6,'status':'succeeded','predicted_pairs':[]}]
    latency,stability,aggregate=runtime_metrics(records,15)
    assert latency['wall_seconds']==15
    assert latency['request_seconds_total']==12
    assert latency['mean_seconds_per_request']==4
    assert latency['mean_seconds_per_case']==6
    assert latency['mean_seconds_first_round']==latency['mean_seconds_all']==3
    assert latency['successful_seconds']==[2]
    assert latency['request_count']==3
    assert latency['case_count']==2
    assert stability=={'a':True}
    assert aggregate['repeated_case_count']==1


def test_stability_detects_edge_drift_ignores_order_and_excludes_failures():
    def record(case,round_,pairs=None,status='succeeded'):
        row={'id':case,'round':round_,'seconds':1,'status':status}
        if pairs is not None:row['predicted_pairs']=pairs
        return row
    records=[record('stable',0,[[0,1],[1,0]]),record('stable',1,[[1,0],[0,1]]),
             record('drift',0,[]),record('drift',1,[[0,0]]),
             record('failed',0,[]),record('failed',1,[]),record('failed',2,status='failed'),
             record('insufficient',0,[]),record('insufficient',1,status='failed')]
    _,stability,aggregate=runtime_metrics(records,10)
    assert stability=={'stable':True,'drift':False,'failed':None,'insufficient':None}
    assert aggregate['repeated_case_count']==4
    assert aggregate['evaluated_case_count']==2
    assert aggregate['drift_case_count']==1
    assert aggregate['drift_ratio']==.5
    assert aggregate['cases_with_failed_requests']==2


def test_single_round_cannot_establish_stability():
    _,stability,aggregate=runtime_metrics([{'id':'only','round':0,'seconds':1,
                                          'status':'succeeded','predicted_pairs':[]}],1)
    assert stability=={}
    assert aggregate['evaluated_case_count']==0
    assert aggregate['drift_ratio'] is None
