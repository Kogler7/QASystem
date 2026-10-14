import json
from pathlib import Path
import shutil

import pytest

from docker import configure
from qasystem_config import configuration as cfg, config_contract as gate, config_guard as guard
from qasystem_config import config_transaction as tx, config_database as database
from qasystem_config.credentials import bindings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def editor(tmp_path, monkeypatch):
    (tmp_path / '.env.example').write_bytes((ROOT / '.env.example').read_bytes())
    (tmp_path / '.env').write_text('GPU_DEVICE_IDS=4,5,6,7\n')
    (tmp_path / 'docker-compose.yml').write_text('services: {}')
    monkeypatch.chdir(tmp_path)
    return configure.Configuration(tmp_path)


def service():
    return {'kind': 'api', 'base_url': 'https://example.invalid/v1', 'model': 'test',
            'api_key_env': 'VENDOR_ACCESS', 'enabled': True}


def register(editor):
    tx.patch_configuration(editor, {'services': {'vendor': service()},
                                   'settings': {'VENDOR_ACCESS': 'private-bound-value'}})


def test_registry_and_credential_can_be_created_in_one_controlled_transaction(editor, monkeypatch):
    register(editor)
    field = next(field for field in editor.fields if field['key'] == 'VENDOR_ACCESS')
    assert field['secret'] and field['default'] == '' and 'services.vendor' in field['description']
    report = tx.apply(editor, reason='register service', base_revision=0)
    assert report['revision'] == 1
    assert 'private-bound-value' not in json.dumps(report)
    from qasystem_config.io import read_env
    assert read_env((editor.root / '.env').read_text())['VENDOR_ACCESS'] == 'private-bound-value'
    monkeypatch.setenv('VENDOR_ACCESS', 'private-bound-value')
    observed = []
    monkeypatch.setattr(cfg, '_observe', lambda *args, **kwargs: observed.append(kwargs))
    assert cfg.getenv('VENDOR_ACCESS') == 'private-bound-value'
    assert observed[-1]['credential'] is True
    for path in editor.directory.rglob('*.json'):
        assert 'private-bound-value' not in path.read_text()


def test_unreferenced_secret_and_unapproved_binding_are_denied(monkeypatch):
    monkeypatch.setattr(cfg, 'read_registry', lambda **kwargs: ({'services': {'vendor': service()}}, None))
    monkeypatch.setenv('FREESTYLE_SECRET', 'hidden')
    for key in ('FREESTYLE_SECRET', 'VENDOR_ACCESS', 'EXECUTION_GPU_MEMORY_REQUEST_MB_UNKNOWN'):
        with pytest.raises(cfg.UndeclaredConfigurationError):
            cfg.getenv(key)


def test_registry_failure_never_falls_back_to_env(monkeypatch):
    def broken(**kwargs):
        raise ValueError('unapproved registry')
    monkeypatch.setattr(cfg, 'read_registry', broken)
    monkeypatch.setenv('VENDOR_ACCESS', 'hidden')
    with pytest.raises(ValueError, match='unapproved'):
        cfg.getenv('VENDOR_ACCESS')


def test_model_binding_and_deleted_reference(monkeypatch):
    model = {'engine_options': {'api_key_env': 'MODEL_ACCESS'}}
    monkeypatch.setattr(cfg, 'read_registry', lambda **kwargs: ({'models': {'local': model}}, 7))
    monkeypatch.setenv('MODEL_ACCESS', 'hidden')
    assert cfg.getenv('MODEL_ACCESS') == 'hidden'
    model['engine_options'].clear()
    with pytest.raises(cfg.UndeclaredConfigurationError):
        cfg.getenv('MODEL_ACCESS')


@pytest.mark.parametrize('key', ['APPLICATION_SERVICE_URL', 'AGENT_API_WORKERS'])
def test_noncredential_or_retired_reference_is_rejected(key):
    with pytest.raises(ValueError):
        bindings({'services': {'bad': {'api_key_env': key}}})


def test_unknown_patch_and_credential_in_reason_are_rejected(editor):
    with pytest.raises(ValueError, match='标准配置'):
        tx.patch_configuration(editor, {'settings': {'FREESTYLE_SECRET': 'hidden'}})
    register(editor)
    with pytest.raises(ValueError, match='凭据'):
        tx.apply(editor, reason='private-bound-value', base_revision=0)
    assert not (editor.directory / 'approved-config.json').exists()


def test_observation_uses_fingerprint_for_no_suffix_credential(tmp_path):
    database.observe_read('VENDOR_ACCESS', 'private-bound-value', directory=tmp_path,
                          revision=2, source='environment', credential=True)
    raw = next((tmp_path / 'events').glob('*.json')).read_text()
    assert 'private-bound-value' not in raw and 'fingerprint' in raw


def test_removing_service_retires_private_dynamic_value(editor):
    register(editor)
    tx.apply(editor, reason='register', base_revision=0)
    reopened = configure.Configuration(editor.root)
    tx.patch_configuration(reopened, {'services': {'vendor': None}})
    tx.apply(reopened, reason='remove unused service', base_revision=1)
    assert 'VENDOR_ACCESS=' not in (editor.root / '.env').read_text()
    assert 'private-bound-value' not in json.dumps(guard.approved(editor.directory))
    for path in editor.directory.rglob('*.json'):
        assert 'private-bound-value' not in path.read_text()


@pytest.mark.parametrize('code', [
    'import os\nx=os.getenv("FREESTYLE_SECRET")',
    'import os\nx=os.environ["FREESTYLE_SECRET"]',
    'import os\nos.environ["KEY"]="hidden"',
    'import os\ndel os.environ["KEY"]',
    'import os\nos.environ.update(KEY="hidden")',
    'import os\nos.putenv("KEY","hidden")',
    'import os\nenv=os.environ\nx=env.get("KEY")',
    'import os\nread=os.getenv\nx=read("KEY")',
])
def test_core_new_module_cannot_bypass_environment_gate(tmp_path, code):
    core = tmp_path / 'qasystem_config'
    core.mkdir()
    (core / 'config-releases').mkdir()
    document = {'version': 'fixture', 'items': {}, 'reader_contracts': {}}
    for path in (core / 'config-declarations.json', core / 'config-releases/fixture.json'):
        path.write_text(json.dumps(document))
    (core / 'new_module.py').write_text(code)
    assert any('禁止绕过集中配置模块' in error for error in gate.check(tmp_path))


def test_trusted_core_function_changes_require_new_declaration(tmp_path):
    shutil.copytree(ROOT / 'qasystem_config', tmp_path / 'qasystem_config')
    source = tmp_path / 'qasystem_config/configuration.py'
    source.write_text(source.read_text().replace('def getenv(key, default=None):',
                      'def getenv(key, default=None):\n    os.getenv("FREESTYLE_SECRET")'))
    assert any('受信核心ENV函数变化' in error for error in gate.check(tmp_path))


def test_unknown_gpu_class_is_rejected():
    from backend.app.execution.resources import ResourceScheduler, ResourceLeaseUnavailable
    with pytest.raises(ResourceLeaseUnavailable):
        ResourceScheduler._memory_request('gpu.unknown')
