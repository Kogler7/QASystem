import json
import ast
from pathlib import Path

import pytest

from docker import startup_contract
from docker.runtime_config import read_document
from docker.configure import Configuration


def test_ops_delivery_includes_local_configuration_dependencies():
    root = Path(__file__).resolve().parents[2]
    packaging = (root / 'package-deploy.ps1').read_text(encoding='utf-8-sig')
    for name in ('configure', 'runtime_config', 'config_contract', 'configuration'):
        tree = ast.parse((root / 'docker' / (name + '.py')).read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('docker.'):
                filename = node.module.split('.')[-1] + '.py'
                if (root / 'docker' / filename).is_file():
                    assert '"' + filename + '"' in packaging, filename


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
