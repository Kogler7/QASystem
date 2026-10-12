import json
from pathlib import Path

import pytest
import yaml

from docker import config_guard as guard, configure

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def project(tmp_path):
    (tmp_path / '.env.example').write_text('# default and help\nGPU_DEVICE_IDS=4,5,6,7\nAPI_KEY=\n')
    (tmp_path / '.env').write_text('GPU_DEVICE_IDS=4,5,6,7\nAPI_KEY=private-secret\n')
    directory = tmp_path / 'runtime-config'
    directory.mkdir()
    (directory / 'config.json').write_text('{"settings":{},"models":{}}')
    (directory / 'models.compose.json').write_text('{"services":{}}')
    return tmp_path, directory


def approve(project):
    root, directory = project
    guard.record(directory, guard.snapshot(root, directory), reason='reviewed test change', reviewer='tester')


def test_initial_configuration_requires_manual_review(project):
    root, directory = project
    with pytest.raises(ValueError, match='人工确认'):
        guard.require(root, directory)
    assert guard.main(['--project-dir', str(root)]) == 2
    assert not (directory / 'approved-config.json').exists()


@pytest.mark.parametrize('filename,content', [
    ('.env', 'GPU_DEVICE_IDS=0\n'),
    ('.env.example', '# upgraded help\nGPU_DEVICE_IDS=4,5,6,7\n'),
    ('runtime-config/config.json', '{"settings":{"KG_EXTRACT_LLM_MODEL":"changed"},"models":{}}'),
    ('runtime-config/models.compose.json', '{"services":{"unknown":{}}}'),
])
def test_unreviewed_edits_block_deployment(project, filename, content):
    root, directory = project
    approve(project)
    (root / filename).write_text(content)
    with pytest.raises(ValueError, match='禁止部署'):
        guard.require(root, directory)


def test_history_records_revisions_and_hides_credentials(project):
    root, directory = project
    approve(project)
    (root / '.env').write_text('GPU_DEVICE_IDS=4,5,6,7\nAPI_KEY=second-secret\n')
    current = guard.snapshot(root, directory)
    delta = '\n'.join(guard.changes(guard.approved(directory), current))
    assert '[修改] env.API_KEY' in delta
    assert 'private-secret' not in delta and 'second-secret' not in delta
    guard.record(directory, current, reason='rotate credential', reviewer='operator')
    assert guard.require(root, directory) == 2
    records = sorted((directory / 'history').glob('*.json'))
    assert len(records) == 2
    text = '\n'.join(p.read_text() for p in records)
    assert 'private-secret' not in text and 'second-secret' not in text
    assert json.loads(records[1].read_text())['previous']


def test_approved_record_must_match_its_history(project):
    root, directory = project
    approve(project)
    path = directory / 'approved-config.json'
    data = json.loads(path.read_text())
    data['reviewer'] = 'modified'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='历史记录不一致'):
        guard.require(root, directory)


def test_catalog_help_and_hot_flag_changes_are_visible(project, monkeypatch):
    root, directory = project
    approve(project)
    (root / '.env.example').write_text('# new GPU description\nGPU_DEVICE_IDS=4,5,6,7\nAPI_KEY=\n')
    monkeypatch.setattr(guard, 'HOT_KEYS', {'GPU_DEVICE_IDS'})
    delta = '\n'.join(guard.changes(guard.approved(directory), guard.snapshot(root, directory)))
    assert 'new GPU description' in delta
    assert 'startup → hot' in delta


def test_guard_cannot_auto_approve_or_execute_unapproved_command(project, monkeypatch):
    root, _ = project
    monkeypatch.setattr(guard.os, 'execvp', lambda *args: pytest.fail('Must not execute'))
    assert guard.main(['--project-dir', str(root), '--exec', 'python', '-V']) == 2
    with pytest.raises(SystemExit):
        guard.main(['--approve'])


def test_headless_ui_cannot_approve(monkeypatch):
    monkeypatch.setattr(configure.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(ValueError, match='交互终端'):
        configure.review_save(None)


def test_compose_startup_runs_guard_for_all_application_services():
    compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
    for name in ['backend', 'agent', 'clef', 'nanojev']:
        service = compose['services'][name]
        assert service['entrypoint'][1] == '/app/docker/config_guard.py'
        assert service['entrypoint'][-1] == '--exec'
        assert './.env:/app/.env:ro' in service['volumes']
        assert '${QASYSTEM_RUNTIME_CONFIG_DIR:-./runtime-config}:/app/runtime-config:ro' in service['volumes']


def test_runtime_rejects_unapproved_and_modified_hot_configuration(project):
    from docker.runtime_config import read_settings
    root, directory = project
    with pytest.raises(ValueError, match='人工确认'):
        read_settings(directory / 'config.json')
    approve(project)
    assert read_settings(directory / 'config.json') == {}
    (directory / 'config.json').write_text('{"settings":{"KG_EXTRACT_LLM_MODEL":"not-approved"},"models":{}}')
    with pytest.raises(ValueError, match='拒绝使用'):
        read_settings(directory / 'config.json')


def test_deleted_canonical_config_cannot_fall_back_to_unreviewed_legacy_file(project):
    from docker.runtime_config import read_settings
    root, directory = project
    approve(project)
    (directory / 'config.json').unlink()
    (directory / 'settings.json').write_text('{"KG_EXTRACT_LLM_MODEL":"not-approved"}')
    with pytest.raises(ValueError, match='拒绝使用'):
        read_settings(directory / 'config.json')


def test_unknown_model_credential_fields_are_rejected_before_logging(project):
    from docker.runtime_config import read_document
    _, directory = project
    (directory / 'config.json').write_text('{"settings":{},"models":{"custom":{"API_KEY":"never-log"}}}')
    with pytest.raises(ValueError, match='Invalid runtime model') as error:
        read_document(directory / 'config.json')
    assert 'never-log' not in str(error.value)


def test_url_userinfo_is_hidden_even_for_unknown_environment_key(project):
    root, directory = project
    (root / '.env').write_text('CUSTOM_URL=https://user:password@server/path\n')
    text = json.dumps(guard.snapshot(root, directory))
    assert 'password@server' not in text


def test_numeric_token_budgets_remain_visible_in_review(project):
    root, directory = project
    (root / '.env').write_text('AGENT_TEMPLATE_MAX_TOKENS=4096\nKG_EXTRACT_THINKING_MAX_TOKENS=32768\n')
    values = guard.snapshot(root, directory)['values']
    assert values['env.AGENT_TEMPLATE_MAX_TOKENS'] == {'value': '4096'}
    assert values['env.KG_EXTRACT_THINKING_MAX_TOKENS'] == {'value': '32768'}
