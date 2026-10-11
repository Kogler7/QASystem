import json
import pytest
from algorithm.knowledge_pipeline import entity_count_estimator as module
from algorithm.knowledge_pipeline.entity_count_chunking import refine_chunks

def test_estimator_uses_entire_chunk_and_configured_model(monkeypatch):
    calls={}
    class Client:
        def __init__(self,**kwargs):calls['config']=kwargs
        def systemone(self,payload):
            calls['payload']=payload
            return {'answers':{'entity_count':{'choice':'21to40','confidence':.8}}}
    monkeypatch.setattr(module,'ClefFlashClient',Client)
    text='完整文本'*4000
    assert module.estimate_entities(text)=={'bucket':'21to40','model':'clef-flash','confidence':.8}
    assert calls['payload']['state']==text
    assert calls['config']=={'model':'clef-flash'}
    assert calls['payload']['questions']['entity_count']['instructions'].startswith('Estimate how many')
    assert set(calls['payload']['questions']['entity_count']['criteria'])=={'le20','21to40','gt40'}

def test_nanojev_uses_existing_client_and_unknown_models_fail(monkeypatch):
    calls=[]
    class Client:
        def systemone(self,payload):
            calls.append(payload)
            return {'answers':{'entity_count':{'choice':'le20'}}}
    monkeypatch.setattr(module,'NanoJevServiceClient',Client)
    assert module.estimate_entities('正文',model='nanojev')=={'bucket':'le20','model':'nanojev'}
    assert calls[0]['state']=='正文'
    with pytest.raises(ValueError,match='unsupported'):module.estimate_entities('正文',model='unregistered')

def test_decision_file_matches_actual_split_and_failure(monkeypatch,tmp_path):
    path=tmp_path/'decisions.jsonl';monkeypatch.setenv('CHUNK_ENTITY_LOG_PATH',str(path))
    text='文本'*100
    parts=refine_chunks(text,[{'text':text,'range':[0,len(text)]}],lambda _: {'estimated_count':41},overlap=50)
    log=json.loads(path.read_text(encoding='utf8'))
    assert log['estimated_count']==41
    assert log['planned_parts']==log['actual_parts']==len(parts)==3
    assert log['ranges']==[p['range'] for p in parts]
    assert log['effective_overlap']==50
    def fail(_):raise RuntimeError('Authorization: SECRET')
    refine_chunks(text,[{'text':text,'range':[0,len(text)]}],fail)
    lines=path.read_text(encoding='utf8').splitlines()
    assert len(lines)==2 and 'SECRET' not in '\n'.join(lines)
    assert json.loads(lines[-1])['status']=='estimator_fallback'

def test_cannot_persist_action_is_not_returned_as_success(monkeypatch,tmp_path):
    monkeypatch.setenv('CHUNK_ENTITY_LOG_PATH',str(tmp_path))
    with pytest.raises(RuntimeError,match='audit'):
        refine_chunks('abc',[{'text':'abc','range':[0,3]}],lambda _: {'estimated_count':1})
