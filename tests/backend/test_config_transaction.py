import json
from pathlib import Path

import pytest

from docker import configure

from qasystem_config import config_guard as guard, config_transaction as tx

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def editor(tmp_path, monkeypatch):
    (tmp_path / '.env.example').write_bytes((ROOT / '.env.example').read_bytes())
    (tmp_path / '.env').write_text('GPU_DEVICE_IDS=4,5,6,7\n')
    (tmp_path / 'docker-compose.yml').write_text('services: {}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(configure.helper, 'run', lambda *a, **kw: pytest.fail('configuration does not deploy'))
    return configure.Configuration(tmp_path)


def test_plan_does_not_publish_and_does_not_disclose(editor):
    original = (editor.root / '.env').read_bytes()
    editor.values['KG_EXTRACT_LLM_API_KEY'] = 'private-test-secret'
    report, _ = tx.plan(editor)
    assert report['base_revision'] == 0
    assert 'private-test-secret' not in json.dumps(report)
    assert (editor.root / '.env').read_bytes() == original
    assert not (editor.directory / 'approved-config.json').exists()


def test_initial_apply_guard_history_and_database(editor):
    report = tx.apply(editor, reason='initial deployment setup', base_revision=0)
    assert report['revision'] == 1 and report['actor'] == 'agent'
    assert report['deployed'] is False
    assert guard.require(editor.root, editor.directory) == 1
    data = json.loads((editor.directory / 'database.json').read_text())
    assert data['versions'][0]['revision'] == 1
    assert data['versions'][0]['actor'] == 'agent'
    assert data['versions'][0]['audit']['base_revision'] == 0
    assert (editor.directory / 'versions/r000001/.env').is_file()
    assert (editor.directory / 'changes/r000001/changes.md').is_file()


def test_revision_cas_and_unknown_key_fail_without_publication(editor):
    tx.apply(editor, reason='initial', base_revision=0)
    reopened = configure.Configuration(editor.root)
    with pytest.raises(ValueError, match='版本已变化'):
        tx.apply(reopened, reason='stale change', base_revision=0)
    with pytest.raises(ValueError, match='标准配置'):
        tx.patch_configuration(reopened, {'settings': {'UNKNOWN_CONFIG': 'value'}})
    assert guard.require(editor.root, editor.directory) == 1


def test_save_cas_preserves_concurrent_file(editor):
    (editor.root / '.env').write_text('GPU_DEVICE_IDS=7\n')
    with pytest.raises(ValueError, match='其他进程修改'):
        tx.apply(editor, reason='conflict', base_revision=0)
    assert (editor.root / '.env').read_text() == 'GPU_DEVICE_IDS=7\n'
    assert not (editor.directory / 'approved-config.json').exists()


def test_archive_failure_restores_every_publication(editor, monkeypatch):
    tx.apply(editor, reason='initial', base_revision=0)
    reopened = configure.Configuration(editor.root)
    old = (editor.root / '.env').read_bytes()
    old_approved = (editor.directory / 'approved-config.json').read_bytes()
    reopened.values['BACKEND_HOST_PORT'] = '60999'
    def fail(*args, **kw): raise OSError('injected view write failure')
    monkeypatch.setattr(tx, 'database', fail)
    with pytest.raises(OSError): tx.apply(reopened, reason='port change', base_revision=1)
    assert (editor.root / '.env').read_bytes() == old
    assert (editor.directory / 'approved-config.json').read_bytes() == old_approved
    assert not (editor.directory / 'versions/r000002').exists()
    assert len(list((editor.directory / 'history').glob('*.json'))) == 1
    assert guard.require(editor.root, editor.directory) == 1


def test_schema_invalid_service_does_not_approve(editor):
    tx.patch_configuration(editor, {'services': {'bad': {'kind': 'api', 'base_url': 'http://user:secret@host', 'model': 'model'}}})
    with pytest.raises(ValueError): tx.apply(editor, reason='bad', base_revision=0)
    assert not (editor.directory / 'approved-config.json').exists()


def test_apply_requires_reason_and_lock_prevents_concurrent_writer(editor):
    with pytest.raises(ValueError, match='非空'): tx.apply(editor, reason='', base_revision=0)
    with tx.transaction_lock(editor.directory):
        with pytest.raises(ValueError, match='另一个配置事务'): tx.apply(editor, reason='change', base_revision=0)


def test_upgrade_retirements_require_explicit_resolution(editor):
    editor.migration_conflicts['OLD_CONFIG'] = {'legacy': 'value'}
    with pytest.raises(ValueError, match='冲突'): tx.plan(editor)
    tx.patch_configuration(editor, {'resolve_conflicts': {'OLD_CONFIG': 'retire'}})
    report = tx.apply(editor, reason='retire unused configuration', base_revision=0)
    assert report['resolved_conflicts'] == ['OLD_CONFIG']


def test_rejected_reason_does_not_leak_or_publish(editor):
    editor.values['KG_EXTRACT_LLM_API_KEY'] = 'private-test-secret'
    with pytest.raises(ValueError, match='原因不得包含凭据'):
        tx.apply(editor, reason='replace private-test-secret', base_revision=0)
    assert not (editor.directory / 'approved-config.json').exists()


def test_retired_name_cannot_be_written_and_alias_migration_is_reported(editor):
    (editor.root / '.env').write_text('GPU_DEVICE_IDS=4,5,6,7\nFILE_SERVICE_URL=http://application:80\n')
    migrated = configure.Configuration(editor.root)
    report, _ = tx.plan(migrated)
    row = next(item for item in report['migrations'] if item['source'] == 'FILE_SERVICE_URL')
    assert row['target'] == 'APPLICATION_SERVICE_URL'
    assert migrated.values['APPLICATION_SERVICE_URL'] == 'http://application:80'
    with pytest.raises(ValueError, match='标准配置'):
        tx.patch_configuration(migrated, {'settings': {'FILE_SERVICE_URL': 'http://other'}})


def test_secrets_remain_private_in_all_json_views(editor):
    editor.values['KG_EXTRACT_LLM_API_KEY'] = 'private-test-secret'
    report = tx.apply(editor, reason='register credentials', base_revision=0)
    assert 'private-test-secret' not in json.dumps(report)
    for folder in ('history', 'changes'):
        assert all('private-test-secret' not in path.read_text()
                   for path in (editor.directory / folder).rglob('*.json'))
    assert 'private-test-secret' not in (editor.directory / 'database.json').read_text()
    assert 'private-test-secret' in (editor.directory / 'versions/r000001/.env').read_text()


def test_unknown_patch_structure_and_raw_model_credentials_rejected(editor):
    with pytest.raises(ValueError, match='顶层'): tx.patch_configuration(editor, {'unexpected': {}})
    model = next(iter(editor.models.values()))
    model['engine_options'] = {'api_key': 'private-test-secret'}
    with pytest.raises(ValueError, match='原始凭据'): tx.plan(editor)


def test_registry_migration_patch_is_explicitly_audited(editor):
    editor.migration_conflicts['services.extract-api.legacy_api'] = {'legacy': {}}
    tx.patch_configuration(editor, {
        'services': {'extract-api': {'kind': 'api', 'adapter': 'chat', 'model': 'model', 'base_url': 'http://api:80'}},
        'resolve_conflicts': {'services.extract-api.legacy_api': 'patched'}})
    assert 'services.extract-api.legacy_api' in editor.resolved_migrations
    assert tx.plan(editor)[0]['resolved_conflicts'] == ['services.extract-api.legacy_api']


def test_cli_plan_apply_dispatch_uses_same_transaction(monkeypatch, tmp_path, capsys):
    from docker import ops
    calls = []
    monkeypatch.setattr(configure, 'run_transaction', lambda root, **kw: calls.append((root, kw)) or {'deployed': False})
    assert ops.main(['--project-dir', str(tmp_path), 'config', 'plan', '--patch', 'private.json']) == 0
    assert calls[-1][1]['operation'] == 'plan'
    assert json.loads(capsys.readouterr().out)['deployed'] is False
    assert ops.main(['--project-dir', str(tmp_path), 'config', 'apply', '--patch', 'private.json',
                     '--reason', 'configure services', '--base-revision', '0']) == 0
    assert calls[-1][1]['base_revision'] == 0
    assert json.loads(capsys.readouterr().out)['deployed'] is False
