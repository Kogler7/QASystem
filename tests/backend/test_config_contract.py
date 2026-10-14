import json
from pathlib import Path

import pytest

from docker import configuration, config_contract, config_database, config_guard


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def contract(tmp_path):
    (tmp_path / 'backend').mkdir()
    (tmp_path / 'docker/config-releases').mkdir(parents=True)
    source = tmp_path / 'backend/settings.py'
    source.write_text('from docker.configuration import getenv as config_getenv\nvalue = config_getenv("TEST_SETTING", "x")\n')
    catalog = {'version': '1', 'dynamic_keys': [], 'items': {'TEST_SETTING': {
        'desc': 'A test setting', 'default': 'x', 'mode': 'startup', 'consumers': [
            {'path': 'backend/settings.py', 'kind': 'read', 'token': 'TEST_SETTING'}]}}}
    catalog['reader_contracts'] = config_contract.reader_contracts(tmp_path)
    for path in ('docker/config-declarations.json', 'docker/config-releases/1.json'):
        (tmp_path / path).write_text(json.dumps(catalog))
    return tmp_path, source


def test_current_repository_contract():
    assert config_contract.check(ROOT) == []


def test_deleted_module_cannot_leave_configuration_declarations(contract):
    root, source = contract
    source.unlink()
    assert any('失去全部消费者' in e for e in config_contract.check(root))


def test_removed_getter_is_not_satisfied_by_a_leftover_string(contract):
    root, source = contract
    source.write_text('old_name = "TEST_SETTING"\n')
    assert any('失去全部消费者' in e for e in config_contract.check(root))


@pytest.mark.parametrize('code', [
    'import os\nx = os.getenv("TEST_SETTING")',
    'import os as system\nx = system.environ.get("TEST_SETTING")',
    'import os\nx = os.environ["TEST_SETTING"]',
    'from os import getenv\nx = getenv("TEST_SETTING")',
])
def test_raw_environment_read_is_blocked(contract, code):
    root, source = contract
    source.write_text(code)
    assert any('禁止' in e for e in config_contract.check(root))


def test_undeclared_literal_key_is_blocked(contract):
    root, source = contract
    source.write_text('value = config_getenv("UNDECLARED_SETTING")')
    assert any('未声明配置' in e for e in config_contract.check(root))


def test_alias_import_cannot_bypass_declaration_gate(contract):
    root, source = contract
    source.write_text('from docker.configuration import getenv as config\nx = config("UNDECLARED_SETTING")')
    assert any('未声明配置' in e for e in config_contract.check(root))


def test_default_change_requires_versioned_declaration(contract):
    root, _ = contract
    (root / '.env.example').write_text('TEST_SETTING=changed\n')
    assert any('默认值不一致' in e for e in config_contract.check(root))
    catalog = json.loads((root / 'docker/config-declarations.json').read_text())
    catalog['items']['TEST_SETTING']['default'] = 'changed'
    (root / 'docker/config-declarations.json').write_text(json.dumps(catalog))
    assert any('不可变版本记录不一致' in e for e in config_contract.check(root))


def test_caller_fallback_change_requires_new_config_version(contract):
    root, source = contract
    source.write_text(source.read_text().replace('"x"', '"changed"'))
    assert any('默认回退变化' in e for e in config_contract.check(root))


def test_runtime_rejects_undeclared_keys_without_echoing_values(monkeypatch):
    monkeypatch.setenv('UNDECLARED_SETTING', 'hidden-value')
    with pytest.raises(configuration.UndeclaredConfigurationError, match='未声明') as error:
        configuration.getenv('UNDECLARED_SETTING')
    assert 'hidden-value' not in str(error.value)


def test_undeclared_key_cannot_be_silenced_as_numeric_parse_fallback():
    from backend.app.execution.supervisor import _env_int
    with pytest.raises(configuration.UndeclaredConfigurationError):
        _env_int('UNDECLARED_NUMERIC_SETTING', 4)


def test_versioned_archives_and_database_separate_approval_from_reads(tmp_path):
    (tmp_path / '.env').write_text('GPU_DEVICE_IDS=4,5\nAPI_KEY=never-print\n')
    (tmp_path / '.env.example').write_text('GPU_DEVICE_IDS=4,5\nAPI_KEY=\n')
    directory = tmp_path / 'runtime-config'
    directory.mkdir()
    (directory / 'config.json').write_text('{"settings":{},"models":{}}')
    (directory / 'models.compose.json').write_text('{"services":{}}')
    config_guard.record(directory, config_guard.snapshot(tmp_path, directory), reason='test', reviewer='tester')
    archived = (directory / 'versions/r000001/.env').read_bytes()
    assert archived == (tmp_path / '.env').read_bytes()
    config_database.observe_read('API_KEY', 'never-print', directory=directory / 'observed', revision=1, source='environment')
    config_database.observe_read('API_KEY', 'never-print', directory=directory / 'observed', revision=1, source='environment')
    data = config_database.database(directory)
    assert data['versions'][0]['state'] == 'approved'
    assert len(data['observations']) == 1
    assert data['observations'][0]['event'] == 'read'
    assert 'never-print' not in json.dumps(data)
    (tmp_path / '.env').write_text('GPU_DEVICE_IDS=6,7\nAPI_KEY=new-secret\n')
    config_guard.record(directory, config_guard.snapshot(tmp_path, directory), reason='test update', reviewer='tester')
    assert (directory / 'versions/r000001/.env').read_bytes() == archived
    assert (directory / 'versions/r000002/.env').read_bytes() == (tmp_path / '.env').read_bytes()


def test_read_records_preserve_actual_values_under_the_same_approved_revision(tmp_path):
    config_database.observe_read('GPU_DEVICE_IDS', '4,5', directory=tmp_path, revision=2, source='environment')
    config_database.observe_read('GPU_DEVICE_IDS', '6,7', directory=tmp_path, revision=2, source='hot')
    values = [json.loads(p.read_text())['value'] for p in (tmp_path / 'events').glob('*.json')]
    assert sorted(values) == ['4,5', '6,7']


def test_runtime_observation_requires_writable_history_mount(monkeypatch):
    monkeypatch.setattr(configuration.Path, 'is_file', lambda self: True)
    monkeypatch.setattr(config_guard, 'verified_record', lambda _: {'revision': 1})
    monkeypatch.setattr(configuration.Path, 'is_dir', lambda self: False)
    with pytest.raises(ValueError, match='不可写'):
        configuration._observe('GPU_DEVICE_IDS', '4,5', 'environment')


def test_retired_variables_are_removed_only_from_reviewable_plan(tmp_path):
    from docker.configure import Configuration
    (tmp_path / '.env.example').write_bytes((ROOT / '.env.example').read_bytes())
    original = 'GPU_DEVICE_IDS=4,5\nKG_EXTRACT_USE_ROUTER=0\nCUSTOM_COMPAT_VAR=keep\n'
    (tmp_path / '.env').write_text(original)
    editor = Configuration(tmp_path)
    plan = editor.pending_payloads()[tmp_path / '.env']
    assert 'KG_EXTRACT_USE_ROUTER=' not in plan
    assert 'CUSTOM_COMPAT_VAR=keep' in plan
    assert (tmp_path / '.env').read_text() == original


def test_observation_revision_is_bound_to_the_read_hot_snapshot(monkeypatch):
    from docker import runtime_config
    monkeypatch.setattr(runtime_config, 'read_settings', lambda **kw: ({'KG_EXTRACT_LLM_MODEL': 'old'}, 1))
    observations = []
    monkeypatch.setattr(configuration, '_observe', lambda *args, **kw: observations.append((args, kw)))
    assert configuration.getenv('KG_EXTRACT_LLM_MODEL') == 'old'
    assert observations[0][1]['revision'] == 1


def test_public_extraction_error_redacts_undeclared_provider_configuration(monkeypatch):
    from backend.app.services import extraction_config
    def fail(_, **kwargs):
        raise configuration.UndeclaredConfigurationError('private config details')
    monkeypatch.setattr(extraction_config, 'resolve_config', fail)
    with pytest.raises(extraction_config.ExtractionConfigurationError) as error:
        extraction_config.snapshot_payload('graph.extract', {'ontology': 'test', 'options': {}})
    assert 'private config details' not in str(error.value)


def test_missing_consumer_is_rejected_even_if_another_consumer_survives(contract):
    root, source = contract
    catalog = json.loads((root / 'docker/config-declarations.json').read_text())
    catalog['items']['TEST_SETTING']['consumers'].append(
        {'path': 'backend/deleted.py', 'kind': 'read', 'token': 'TEST_SETTING'})
    for name in ('config-declarations.json', 'config-releases/1.json'):
        (root / 'docker' / name).write_text(json.dumps(catalog))
    assert any('消费者路径不存在' in e for e in config_contract.check(root))


def test_external_tools_can_be_excluded_only_in_delivered_source(contract):
    root, source = contract
    catalog = json.loads((root / 'docker/config-declarations.json').read_text())
    catalog['items']['TEST_SETTING']['consumers'].append(
        {'path': 'eval/absent.py', 'kind': 'external', 'token': 'TEST_SETTING'})
    for name in ('config-declarations.json', 'config-releases/1.json'):
        (root / 'docker' / name).write_text(json.dumps(catalog))
    assert any('消费者路径不存在' in e for e in config_contract.check(root))
    assert config_contract.check(root, deployed=True) == []
