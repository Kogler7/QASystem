from copy import deepcopy
from types import SimpleNamespace
from contextlib import contextmanager

import pytest

from qasystem_config.api_services import validate_api_registry, migrate_api_services
from docker.api_ui import edit_field, ui_rows
from external_services.model_routes import resolve_service
from external_services.llm.service import check_service_budget


def config():
    return SimpleNamespace(services={}, accounts={}, routes={})


def test_forms_register_then_route_and_prevent_dangling_deletion():
    cfg = config()
    edit_field(cfg, ui_rows(cfg)[0], prompt=lambda _: 'shared-api')
    edit_field(cfg, next(r for r in ui_rows(cfg) if r['registry_kind']=='用途' and r['record_id']=='extract'), prompt=lambda _: 'shared-api')
    assert cfg.routes == {'extract': 'shared-api'}
    with pytest.raises(ValueError, match='解除'):
        edit_field(cfg, next(r for r in ui_rows(cfg) if r['key']=='delete'), prompt=lambda _: 'DELETE')
    assert cfg.services['shared-api']['capabilities'] == {}
    assert not any('JSON' in r['display_name'] for r in ui_rows(cfg))


def test_schema_and_snapshot_share_account_without_secrets():
    api = {'kind':'api','adapter':'chat','base_url':'https://example.invalid/v1',
           'model':'model','api_key_env':'AGENT_LLM_API_KEY','account_id':'account',
           'limits':{'max_concurrency':2,'qps':0,'qpm':0},
           'capabilities':{'context_tokens':10000,'max_output_tokens':2000}}
    registry = {'services': {'llm':api}, 'accounts': {'account':{'max_concurrency':1,'qps':3,'qpm':60}},
                'routes':{'extract':'llm','graph':'llm','agent':'llm'}}
    validate_api_registry(registry['services'], registry['accounts'], registry['routes'])
    snapshots = [resolve_service(p, registry=registry, revision='r1') for p in ('extract','graph','agent')]
    assert all(s['service_id']=='llm' and s['account_limits']['max_concurrency']==1 for s in snapshots)
    registry['accounts']['account']['max_concurrency']=20
    assert snapshots[0]['account_limits']['max_concurrency']==1
    with pytest.raises(ValueError, match='capability'):
        check_service_budget({'messages': [], 'max_tokens': 2001}, snapshots[0])


def test_local_context_is_derived_from_ops_model_not_duplicated_or_overridden(monkeypatch):
    from qasystem_config import model_registry as model_services
    monkeypatch.setattr(model_services,'local_model_endpoint',lambda *a:{'base_url':'http://local:8000/v1','model':'qwen','adapter':'chat'})
    registry={'models':{'qwen':{'enabled':True,'context':'4096'}},
              'services':{'a':{'kind':'local','model':'qwen','capabilities':{'context_tokens':99999}}},
              'routes':{'extract':'a'}}
    snapshot=resolve_service('extract',registry=registry)
    assert snapshot['capabilities']['context_tokens']==4096


@pytest.mark.parametrize('mutation', ['account','route','negative','output'])
def test_bad_registry_rejected(mutation):
    services = {'a': {'kind':'api','limits':{'max_concurrency':1}}}
    routes, accounts = {}, {}
    if mutation=='account': services['a']['account_id']='missing'
    if mutation=='route': routes['graph']='missing'
    if mutation=='negative': services['a']['limits']['qps']=-1
    if mutation=='output': services['a']['capabilities']={'context_tokens':3,'max_output_tokens':4}
    with pytest.raises(ValueError): validate_api_registry(services, accounts, routes)


def test_migration_preserves_authoritative_local_and_exposes_conflict():
    old = {'KG_EXTRACT_LLM_BASE_URL':'https://example.invalid/v1','KG_EXTRACT_LLM_MODEL':'old'}
    original = {'chat': {'kind':'local','model':'qwen'}}
    services, accounts, routes, conflicts = migrate_api_services(old, original, {}, {'extract':'chat'})
    assert services == original and routes['extract']=='chat'
    assert conflicts and not accounts


def test_provider_migration_account_limits_remain_explicit():
    env = {'KG_EXTRACT_PROVIDERS_JSON':'{"api":{"base_url":"https://example.invalid","model":"m","api_key_env":"AGENT_LLM_API_KEY","quota_group":"same","max_concurrency":2}}'}
    services, accounts, routes, conflicts = migrate_api_services(env, {}, {}, {})
    assert services['api']['limits']['max_concurrency']==2
    assert accounts['same']['max_concurrency']==2
    assert not conflicts and not routes


def test_model_name_without_connection_does_not_invent_api_endpoint():
    services, accounts, routes, conflicts = migrate_api_services({'KG_EXTRACT_LLM_MODEL': 'model'}, {}, {}, {})
    assert not services and not accounts and not routes and not conflicts


def test_old_algorithm_budget_is_not_invented_as_api_capability():
    env = {'KG_EXTRACT_PROVIDERS_JSON':'{"api":{"base_url":"https://example.invalid","model":"m","api_key_env":"AGENT_LLM_API_KEY","max_input_tokens":4096,"max_tokens":1000}}'}
    services, _, _, _ = migrate_api_services(env, {}, {}, {})
    assert services['api']['capabilities']=={}


def test_agent_worker_new_task_refreshes_route_and_same_task_keeps_snapshot(monkeypatch):
    from agent.agentcode import worker
    calls = []
    current = {'base_url': 'https://first.invalid/v1', 'timeout_s': 1, 'model': 'first'}
    def load():
        calls.append(1)
        return deepcopy(current)
    monkeypatch.setattr(worker, '_load_env', load)
    llm = worker._StreamingWorkerLLM()
    try:
        llm.set_task_id('task-a')
        current.update(base_url='https://second.invalid/v1', model='second')
        llm.set_task_id('task-a')
        assert llm._cfg['model']=='first' and len(calls)==1
        llm.set_task_id('task-b')
        assert llm._cfg['model']=='second' and len(calls)==2
        assert llm._url=='https://second.invalid/v1/chat/completions'
    finally:
        llm.close()


def test_extraction_sdk_uses_shared_service_admission_and_checks_capability(monkeypatch):
    from external_services.llm import distributed, runtime
    entered, sent = [], []
    service = {'service_id':'shared', 'limits':{'max_concurrency':1},
               'capabilities':{'max_output_tokens':5}}
    class Pool:
        def calibration(self, key): return {'max_underestimate':0, 'samples':0}
        @contextmanager
        def request_slot(self, deadline, **quota):
            from external_services.llm.quota_state import Reservation
            entered.append(deadline)
            yield Reservation(0, quota['input_tokens'], quota['input_tokens'] + quota['max_output_tokens'])
    monkeypatch.setattr(distributed, 'service_pool', lambda snapshot: Pool())
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: sent.append(kw) or 'ok')))
    cfg = {'service_snapshot':service,'quota_group':'ignored','request_timeout':3,
           'disable_thinking':True,'max_input_tokens':65536}
    client = runtime.ScheduledClient(sdk, cfg)
    assert client.chat.completions.create(messages=[],max_tokens=5)=='ok'
    assert len(entered)==len(sent)==1
    with pytest.raises(ValueError, match='capability'):
        client.chat.completions.create(messages=[],max_tokens=6)
    assert len(entered)==len(sent)==1


def test_graph_uses_registered_service_and_same_admission(monkeypatch):
    from algorithm.ans.QA import utils
    from external_services.llm import service as entry
    sent, admissions = [], []
    service = {'service_id':'shared','base_url':'https://routed.invalid/v1','model':'registered'}
    monkeypatch.setattr(entry, 'resolve_chat_service', lambda _, **kw: (service, 'key'))
    @contextmanager
    def admit(snapshot, payload, **kw):
        admissions.append((snapshot,payload))
        yield 1
    monkeypatch.setattr(entry, 'model_call', admit)
    class Client:
        def __init__(self, **kwargs):
            sent.append(kwargs)
            self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def create(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='result'))])
    monkeypatch.setattr(utils,'OpenAI',Client)
    assert utils.run_llm('question',.1,10,'legacy-key','legacy-model')=='result'
    assert sent[0]['base_url']==service['base_url'] and sent[1]['model']=='registered'
    assert admissions[0][0]['service_id']=='shared' and sent[1]['timeout']==1


def test_agent_summary_defaults_to_registered_model_and_admission(monkeypatch):
    from agent.agentcode.tools.base_tools import memory_tool
    from external_services.llm import service as entry
    sent, admitted = [], []
    snapshot = {'service_id':'agent-service','base_url':'https://summary.invalid/v1','model':'same-agent'}
    monkeypatch.setattr(entry,'resolve_chat_service',lambda _, **kw: (snapshot,''))
    @contextmanager
    def admit(service,payload,**kwargs):
        admitted.append(service)
        yield 1
    monkeypatch.setattr(entry,'model_call',admit)
    class Client:
        def __init__(self, **kwargs):
            self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def create(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='summary'))])
    monkeypatch.setattr(memory_tool,'OpenAI',Client)
    monkeypatch.setattr(memory_tool.tiktoken,'get_encoding',lambda _: None)
    compactor=memory_tool.MemoryCompactor()
    assert compactor._generate_summary('conversation')=='summary'
    assert sent[0]['model']=='same-agent' and admitted==[snapshot]


def test_missing_or_disabled_service_never_uses_old_environment(monkeypatch):
    from external_services import model_routes
    from external_services.llm.service import resolve_chat_service
    monkeypatch.setenv('AGENT_LLM_BASE_URL','https://legacy.invalid/v1')
    monkeypatch.setenv('AGENT_LLM_MODEL','legacy')
    monkeypatch.setattr(model_routes,'registry_snapshot',lambda: ({'services':{},'routes':{}},'unit'))
    with pytest.raises(ValueError,match='用途路由'):
        resolve_chat_service('agent',required=True)
    registry={'services':{'a':{'kind':'api','enabled':False,'base_url':'https://api.invalid','model':'m'}},'routes':{'agent':'a'}}
    with pytest.raises(ValueError,match='关闭'):
        resolve_service('agent',registry=registry)


def test_live_algorithm_factory_requires_registered_or_explicit_connection(monkeypatch):
    from external_services import model_routes
    from algorithm.knowledge_extract.v3 import llm_client
    import openai
    calls=[]
    monkeypatch.setenv('KG_EXTRACT_LLM_BASE_URL','https://legacy.invalid/v1')
    monkeypatch.setattr(model_routes,'registry_snapshot',lambda: ({'services':{},'routes':{}},'unit'))
    monkeypatch.setattr(openai,'OpenAI',lambda **kw: calls.append(kw))
    with pytest.raises(ValueError,match='未选择'):
        llm_client.create_llm_client()
    assert calls==[]


def test_graph_connection_migration_requires_review_and_preserves_ssl_value():
    old={'GRAPH_LLM_BASE_URL':'https://graph.invalid/v1','GRAPH_LLM_MODEL':'g',
         'GRAPH_LLM_VERIFY_SSL':'false','GRAPH_LLM_TIMEOUT_SECONDS':'17'}
    services,_,_,conflicts=migrate_api_services(old,{}, {},{})
    assert services['migrated-graph']['connection']=={'verify_ssl':False,'timeout_seconds':17}
    existing=deepcopy(services)
    existing['migrated-graph']['connection']['verify_ssl']=True
    _,_,_,conflicts=migrate_api_services(old,existing,{}, {'graph':'migrated-graph'})
    assert 'services.migrated-graph.connection' in conflicts


def test_agent_subtasks_keep_parent_service_snapshot(monkeypatch):
    from agent.agentcode import worker
    monkeypatch.setattr(worker,'_load_env',lambda: pytest.fail('must not resolve a new subtask route'))
    cfg={'base_url':'https://parent.invalid/v1','model':'parent','timeout_s':120}
    llm=worker._make_tagged_llm(None,'parent-task','subtask',service_config=cfg)
    try:
        assert llm._cfg['model']=='parent' and llm._task_id=='parent-task'
    finally:
        llm.close()


def test_missing_route_fails_task_and_worker_continues(monkeypatch):
    import threading
    from contextlib import nullcontext
    from agent.agentcode import worker, stream_broker
    stop=threading.Event()
    queue=[('missing',{'user_message':'first'}),('ready',{'user_message':'second'})]
    statuses={}
    class LLM:
        _cfg={}
        def __init__(self, **kwargs): pass
        def set_task_id(self, task_id):
            if task_id=='missing': raise ValueError('模型用途未配置')
        def close(self): pass
    def dequeue(**kwargs):
        if queue: return queue.pop(0)
        stop.set()
        return None
    monkeypatch.setattr(worker,'_StreamingWorkerLLM',LLM)
    monkeypatch.setattr(worker.task_store,'dequeue',dequeue)
    monkeypatch.setattr(worker.task_store,'update',lambda tid,status,**kw: statuses.update({tid:status}))
    monkeypatch.setattr(worker.task_store,'get',lambda tid: {'status':statuses[tid]})
    monkeypatch.setattr(worker.task_store,'delete_auth_context',lambda *a: None)
    monkeypatch.setattr(worker.task_store,'load_auth_context',lambda *a: {})
    monkeypatch.setattr(worker,'_build_config',lambda *a:(None,None))
    monkeypatch.setattr(worker,'run_orchestrated',lambda **kw: SimpleNamespace(status='ok',payload={},spent={},trace_ref=None))
    monkeypatch.setattr(stream_broker,'get_broker',lambda:SimpleNamespace(publish=lambda *a:None))
    tm=SimpleNamespace(bind_user_headers=lambda *a:nullcontext())
    pool=SimpleNamespace(borrow=lambda **kw:nullcontext(tm))
    worker._process_one(pool,None,None,None,stop,0)
    assert statuses=={'missing':'failed','ready':'completed'}
