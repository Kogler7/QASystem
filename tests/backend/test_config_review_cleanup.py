import json
import ast
from pathlib import Path

import pytest

from qasystem_config import startup_contract
from qasystem_config.runtime_config import read_document
from docker.configure import Configuration


def test_ops_delivery_includes_local_configuration_dependencies():
    root = Path(__file__).resolve().parents[2]
    packaging = (root / 'package-deploy.ps1').read_text(encoding='utf-8-sig')
    for relative in ('docker/configure.py', 'docker/ops.py', 'qasystem_config/runtime_config.py',
                     'qasystem_config/config_contract.py', 'qasystem_config/configuration.py'):
        tree = ast.parse((root / relative).read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('docker.'):
                filename = node.module.split('.')[-1] + '.py'
                if (root / 'docker' / filename).is_file():
                    assert '"' + filename + '"' in packaging, filename
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('qasystem_config.'):
                assert '"qasystem_config"' in packaging
                assert (root / 'qasystem_config' / (node.module.split('.')[-1] + '.py')).is_file()


def test_internal_redis_follows_topology_not_legacy_env(monkeypatch):
    monkeypatch.setenv('TASK_STORE_REDIS_URL', 'redis://invalid:9999/99')
    monkeypatch.setattr(startup_contract, 'in_container_runtime', lambda: True)
    assert startup_contract.internal_redis_url(0) == 'redis://redis:6379/0'
    monkeypatch.setattr(startup_contract, 'in_container_runtime', lambda: False)
    assert startup_contract.internal_redis_url(1) == 'redis://localhost:6379/1'


def test_service_account_roundtrip_and_reference_validation(tmp_path):
    file = tmp_path / 'config.json'
    document = {'services': {'shared': {'kind': 'api', 'base_url': 'http://llm:8000',
        'model': 'model', 'account_id': 'account', 'limits': {'max_concurrency': 2},
        'capabilities': {'context_tokens': 32000, 'max_output_tokens': 8000}}},
        'accounts': {'account': {'max_concurrency': 4}}, 'routes': {'agent': 'shared'}}
    file.write_text(json.dumps(document))
    assert read_document(file)['accounts'] == document['accounts']
    document['accounts'] = {}
    file.write_text(json.dumps(document))
    with pytest.raises(ValueError, match='账号'):
        read_document(file)


def test_configuration_preserves_accounts_in_pending_payloads(tmp_path, monkeypatch):
    monkeypatch.setattr(Configuration, 'refresh_live', lambda self: None)
    config = Configuration(tmp_path)
    config.accounts['shared'] = {'max_concurrency': 3, 'qps': 1, 'qpm': 30}
    payload = config.pending_payloads()[config.directory / 'config.json']
    assert json.loads(payload)['accounts']['shared']['qpm'] == 30
    assert any(row['kind'] == 'api_registry' and row['registry_kind'] == '账号' for row in config.rows())


def test_default_execution_ignores_retired_inline_switch(tmp_path, monkeypatch):
    from backend.app.execution.manager import ExecutionManager, ExecutionUnavailable
    from backend.app.execution.store import MemoryExecutionStore
    from backend.app.execution.artifacts import ArtifactStore
    from backend.app.execution.hub import ExecutionEventHub
    monkeypatch.setenv('EXECUTION_MODE', 'inline')
    monkeypatch.setenv('EXECUTION_ALLOW_MEMORY_QUEUE', 'false')
    store = MemoryExecutionStore()
    manager = ExecutionManager(store, ArtifactStore(tmp_path), ExecutionEventHub(store))
    assert manager.is_async
    with pytest.raises(ExecutionUnavailable):
        manager.submit(operation='unit', resource_class='cpu.default', payload={})


def test_retired_redis_and_artifact_overrides_cannot_change_topology(tmp_path, monkeypatch):
    from backend.app.execution import store, artifacts
    calls = []
    monkeypatch.setenv('EXECUTION_REDIS_URL', 'redis://foreign.invalid/99')
    monkeypatch.setenv('EXECUTION_DATA_DIR', str(tmp_path / 'legacy'))
    monkeypatch.setattr(startup_contract, 'in_container_runtime', lambda: True)
    monkeypatch.setattr(store, '_STORE', None)
    monkeypatch.setattr(store, 'RedisExecutionStore', lambda url: calls.append(url) or object())
    store.get_execution_store()
    assert calls == ['redis://redis:6379/1']
    monkeypatch.setattr(artifacts, 'execution_data_directory', lambda: tmp_path / 'managed')
    assert artifacts.ArtifactStore().root == tmp_path / 'managed'
    assert not (tmp_path / 'legacy').exists()


def test_retired_hot_connection_inputs_are_preserved_in_service_migration(tmp_path):
    directory = tmp_path / 'runtime-config'
    directory.mkdir()
    (directory / 'config.json').write_text(json.dumps({'settings': {
        'KG_EXTRACT_LLM_BASE_URL': 'http://model.test/v1', 'KG_EXTRACT_LLM_MODEL': 'old-model',
        'KG_EXTRACT_LLM_MAX_CONCURRENCY': '2'}, 'models': {}}))
    config = Configuration(tmp_path)
    service = config.services[config.routes['extract']]
    assert service['model'] == 'old-model' and service['limits']['max_concurrency'] == 2
    payload = json.loads(config.pending_payloads()[directory / 'config.json'])
    assert not set(payload['settings']) & {'KG_EXTRACT_LLM_BASE_URL', 'KG_EXTRACT_LLM_MODEL'}
