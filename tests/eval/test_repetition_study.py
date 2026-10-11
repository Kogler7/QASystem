from eval.tools.repetition_study import metrics,records
from eval.tools.repetition_report import simulate_guard,prompt_contract_audit
import hashlib,json
from pathlib import Path

def test_recovers_complete_records_from_truncated_legacy_array():
    text='{"relations":[{"from_id":"a","to_id":"b","rel_type":"r"},{"from_id":"a","to_id":"b","rel_type":"r"},{"from_id":"'
    result=metrics(text)
    assert result['records']==2
    assert result['unique_records']==1
    assert result['duplicate_fraction']==.5
    assert result['first_duplicate_record']==2

def test_detects_loop_inside_unfinished_property_string():
    text='{"properties":{"category":"'+('流程约束、合规约束、质量约束、权限约束、'*40)
    result=metrics(text)
    assert result['records']==0
    assert result['repeated_substring_chars']>400

def test_schema_wrappers_and_jsonl_have_same_records():
    assert records('{"entities":[{"type":"T","mention":"x"}]}')==records('{"type":"T","mention":"x"}\n')

def test_normal_array_property_is_not_collection_wrapper():
    assert records('{"tags":["a","b"]}')==[{'tags':['a','b']}]
    assert records('{"entity_id":"e1","properties":{"tags":["a","b"]}}')==[{'entity_id':'e1','properties':{'tags':['a','b']}}]

def test_simulated_guard_keeps_unique_prefix_and_marks_simulation():
    text='{"type":"T","mention":"a"}\n'*50
    stopped=simulate_guard(text,step=32)
    assert stopped['trigger_chars']<len(text)
    assert stopped['unique_retention']==1
    assert stopped['simulation_only'] is True

def test_simulated_guard_does_not_stop_normal_short_output():
    assert simulate_guard('{"type":"T","mention":"a"}')['trigger_chars'] is None

def test_frozen_repair_case_exposes_unemittable_required_relation():
    case=json.loads(Path('test_data/repetition/v1/repair_relation-loop.json').read_text(encoding='utf8'))
    audited=prompt_contract_audit(case)
    assert audited['issue_count']==4
    assert audited['unemittable_required_relations']==['CONSTRAINS_AGENT']

def test_frozen_manifest_and_credential_boundary():
    root=Path('test_data/repetition/v1');manifest=json.loads((root/'manifest.json').read_text(encoding='utf8'))
    assert len(manifest['cases'])>=8
    for case in manifest['cases']:
        raw=(root/(case['id']+'.json')).read_bytes().replace(b'\r\n',b'\n')
        assert hashlib.sha256(raw).hexdigest()==case['sha256']
        decoded=json.loads(raw)
        assert 'call' in decoded and 'reference_anchors' in decoded
        assert 'api_key' not in decoded and 'Authorization' not in decoded
        assert decoded['quality_labels']['independent_human_review'] is False
        assert decoded['quality_labels']['label_status']=='agent-reviewed / provisional'
        assert 'manual_review_status' not in decoded['quality_labels']
