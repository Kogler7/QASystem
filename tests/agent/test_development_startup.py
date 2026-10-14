from agentcode import api
from qasystem_config import configuration, startup_contract


def test_development_multi_process_cli_keeps_explicit_concurrency(monkeypatch):
    calls = []
    monkeypatch.delenv('ENVIRONMENT', raising=False)
    monkeypatch.setenv('AGENT_WORKER_CONCURRENCY', '4')
    monkeypatch.setattr(startup_contract, 'in_container_runtime', lambda: False)
    monkeypatch.setattr('sys.argv', ['agentcode-api', '--workers', '2', '--concurrency', '8'])
    import uvicorn
    monkeypatch.setattr(uvicorn, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(api, '_WORKER_CONCURRENCY', 4)
    monkeypatch.setattr(api, '_WORKER_POOL_SIZE', 4)
    monkeypatch.setattr(api, '_API_WORKERS', 1)
    api.main()
    assert configuration.startup_getenv('AGENT_WORKER_CONCURRENCY') == '8'
    assert calls[0][0] == ('agentcode.api:app',)
    assert calls[0][1]['workers'] == 2
    assert api._WORKER_CONCURRENCY == 8
    assert api._WORKER_POOL_SIZE == 8
