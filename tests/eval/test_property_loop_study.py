import copy
import hashlib
import json
from pathlib import Path

from eval.tools.property_loop_study import focused_call, audit, safe_error
from eval.tools.repetition_causal import section


ROOT=Path('test_data/repetition/property-loop-v1')


def case(name):
    return json.loads((ROOT/(name+'.json')).read_text(encoding='utf8'))


def source_and_context(call):
    user=next(m['content'] for m in call['messages'] if m['role']=='user')
    return [section(user,h).group(1) for h in ['current chunk','local context']]


def test_partitions_preserve_all_source_and_targets_without_output_cap():
    baseline=case('property-000040-baseline');snapshot=copy.deepcopy(baseline)
    item=baseline['call']['schema']['properties']['updates']['items']['properties']
    ids=item['entity_id']['enum'];keys=set(item['properties']['properties'])
    assert len(ids)==4 and len(keys)==5
    for eid in ids:
        focused=case('property-000040-'+eid)
        assert source_and_context(focused['call'])==source_and_context(baseline['call'])
        target=focused['call']['schema']['properties']['updates']['items']['properties']
        assert target['entity_id']['enum']==[eid]
        assert set(target['properties']['properties'])==keys
    for key in keys:
        focused=case('property-000040-entity-25-'+key)
        assert source_and_context(focused['call'])==source_and_context(baseline['call'])
        target=focused['call']['schema']['properties']['updates']['items']['properties']
        assert target['entity_id']['enum']==['entity-25']
        assert list(target['properties']['properties'])==[key]
    assert focused_call(baseline['call'],'entity-25','职责')
    assert baseline==snapshot


def test_manifest_hashes_and_six_call_diagnostic_plan():
    manifest=json.loads((ROOT/'manifest.json').read_text(encoding='utf8'))
    assert manifest['max_tokens']==8192
    assert manifest['model']=='Qwen/Qwen3.5-4B'
    assert manifest['workers']==1
    for entry in manifest['cases']:
        data=(ROOT/(entry['id']+'.json')).read_text(encoding='utf8').replace('\r\n','\n').encode('utf8')
        assert hashlib.sha256(data).hexdigest()==entry['sha256']
    plan=json.loads((ROOT/'plan.json').read_text(encoding='utf8'))
    assert len(plan)==6
    assert sum('baseline' in name for name,_,_ in plan)==2


def test_audit_does_not_equate_unique_object_with_schema_valid_fact():
    frozen=case('property-000040-baseline')
    malformed={'entity_id':'entity-25','properties':{'职责':'职责'}}
    text='\n'.join(json.dumps(malformed,ensure_ascii=False) for _ in range(45))
    result=audit(frozen['call'],text)
    assert result['records']==45
    assert result['unique_records']==1
    assert result['schema_valid_records']==0
    assert result['quote_bound_records']==0
    assert result['trajectory']['sustained_zero_novel_stop_record']==40


def test_error_sanitizes_credentials_bearer_and_address_parameters():
    output=safe_error(ValueError('secret-value Bearer another-token https://user:pass@host/path?api_key=query-key authorization=third-token'), 'secret-value')
    for secret in ['secret-value','another-token','user:pass','query-key','third-token']:
        assert secret not in output
    assert len(output)<=400
