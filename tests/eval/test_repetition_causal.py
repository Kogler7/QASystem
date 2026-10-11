import copy,json
import re
from types import SimpleNamespace
from pathlib import Path
from eval.tools.repetition_causal import relation_case,entity_cases,catalog,section,relation_integrity,expansion_trajectory,analyze
from eval.tools.repetition_report import prompt_contract_audit

def fixture(name):return json.loads((Path('test_data/repetition/v1')/(name+'.json')).read_text(encoding='utf8'))

def test_relation_scope_alignment_removes_only_ineligible_repairs():
    original=fixture('repair_relation-loop');snapshot=copy.deepcopy(original)
    aligned=relation_case(original,'legacy','scope')
    assert original==snapshot
    assert len(aligned['derivation']['excluded_issues'])==4
    assert prompt_contract_audit(aligned)['unemittable_required_relations']==[]
    assert aligned['call']['schema']==original['call']['schema']
    assert catalog(aligned['call']['messages'])==catalog(original['call']['messages'])

def test_current_type_filtered_schema_reproduces_frozen_missing_constraint_relation():
    from algorithm.knowledge_extract.v2.candidate_generation import output_schema_for_types
    case=fixture('repair_relation-loop');user=next(m for m in case['call']['messages'] if m['role']=='user')
    triples=re.findall(r'`([^`]+)` -\[`([^`]+)`\]-> `([^`]+)`',section(user['content'],'ontology relation signatures').group(1))
    specs=[SimpleNamespace(source=s,relation_id=r,target=t) for s,r,t in triples]
    plan=SimpleNamespace(output_schemas={'relation_candidates':case['call']['schema']},relation_specs={'all':specs})
    endpoint_types={x['type'] for x in catalog(case['call']['messages']).values()}
    produced=output_schema_for_types(plan,'relation_candidates',endpoint_types)
    assert produced==case['call']['schema']
    assert 'OperationalConstraint' not in endpoint_types
    assert 'CONSTRAINS_AGENT' not in produced['properties']['relations']['items']['properties']['rel_type']['enum']

def test_enum_only_is_labeled_unsafe_when_constraint_endpoint_missing():
    aligned=relation_case(fixture('repair_relation-loop'),'legacy','enum_only')
    assert 'CONSTRAINS_AGENT' in aligned['call']['schema']['properties']['relations']['items']['properties']['rel_type']['enum']
    assert not any(x['type']=='OperationalConstraint' for x in catalog(aligned['call']['messages']).values())
    assert 'not a production fix' in aligned['derivation']['warning']
    edge={'from_id':'entity-0','rel_type':'CONSTRAINS_AGENT','to_id':'entity-14'}
    integrity=relation_integrity(aligned['call']['messages'],[edge])
    assert integrity['unknown_endpoint_edges']==0
    assert integrity['signature_invalid_edges']==1

def test_clean_protocol_removes_conflicting_wrapper_and_empty_entity_instruction():
    clean=relation_case(fixture('repair_relation-loop'),'clean','conflict')
    messages='\n'.join(m['content'] for m in clean['call']['messages'])
    assert '只输出 JSON object' not in messages
    assert '输出必须是 JSON object' not in messages
    assert '{"entities": []}' not in messages
    assert '## output schema' not in messages
    assert prompt_contract_audit(clean)['unemittable_required_relations']==['CONSTRAINS_AGENT']

def test_semantic_segments_cover_every_source_character_without_output_cap():
    source=fixture('entity-loop');cases=entity_cases(source)
    def text(case):
        message=next(m for m in case['call']['messages'] if m['role']=='user')
        return json.loads(section(message['content'],'current chunk').group(1))['text']
    assert text(cases[1])+text(cases[2])==text(cases[0])
    assert [len(text(c)) for c in cases]==[734,498,236]
    assert all('最多输出' not in m['content'] for c in cases for m in c['call']['messages'])
    assert cases[1]['derivation']['source_range'][1]==cases[2]['derivation']['source_range'][0]

def test_sustained_guard_tolerates_small_duplicates_and_preserves_progress():
    normal='\n'.join(json.dumps({'type':'T','mention':str(i)}) for i in range(50))
    normal+='\n'+json.dumps({'type':'T','mention':'1'})
    assert expansion_trajectory(normal)['sustained_zero_novel_stop_record'] is None
    looping=normal+'\n'+('\n'.join(json.dumps({'type':'T','mention':'1'}) for _ in range(100)))
    measured=expansion_trajectory(looping)
    assert measured['last_new_record']==50
    assert measured['sustained_zero_novel_stop_record']==70
    assert measured['sustained_guard_unique_retention']==1

def test_causal_manifest_hashes_frozen_cases():
    import hashlib
    root=Path('test_data/repetition/causal-v1')
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf8'))
    assert len(manifest['cases'])==8
    for case in manifest['cases']:
        data=(root/(case['id']+'.json')).read_text(encoding='utf8').replace('\r\n','\n').encode('utf8')
        assert hashlib.sha256(data).hexdigest()==case['sha256']

def test_analysis_includes_adaptive_fifth_pair_and_marks_timeout_unknown(tmp_path):
    dataset=Path('test_data/repetition/causal-v1')
    for name,error in [('entity-segment-a',{'type':'APITimeoutError'}),('entity-segment-b',None)]:
        case=json.loads((dataset/(name+'.json')).read_text(encoding='utf8'))
        data={'summary':{'case':name,'repeat':5,'error':error,'seconds':1,'usage':None,'finish_reason':None if error else 'stop','schema_error_count':0},'request':{'messages':case['call']['messages']},'response_text':'' if error else 'null'}
        (tmp_path/(name+'--baseline--5.json')).write_text(json.dumps(data),encoding='utf8')
    result=analyze(dataset,tmp_path,tmp_path/'report.json')
    assert result['calls']==2
    assert result['merged_segments'][0]['repeat']==5
    assert result['merged_segments'][0]['both_responded'] is False
    assert result['merged_segments'][0]['per_segment_within_target']==[None,True]
