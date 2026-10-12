import json
from pathlib import Path

import pytest

from docker import config_guard as guard, ops
from docker.runtime_config import read_document
from backend.app.services import extraction_config
from external_services.models import RemoteEmbeddingClient, RemoteDocumentParserClient
from external_services.clef import ClefFlashClient


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('GPU_DEVICE_IDS=4,5\nSERVICE_API_KEY=hidden\n')
    (tmp_path / '.env.example').write_text('GPU_DEVICE_IDS=4,5\nSERVICE_API_KEY=\n')
    directory = tmp_path / 'runtime-config'
    directory.mkdir()
    (directory / 'config.json').write_text('{"settings":{},"models":{}}')
    (directory / 'models.compose.json').write_text('{"services":{}}')
    monkeypatch.setenv('QASYSTEM_RUNTIME_SETTINGS_FILE', str(directory / 'config.json'))
    return tmp_path, directory


def save(project, settings, models=None):
    root, directory = project
    (directory / 'config.json').write_text(json.dumps({'settings': settings, 'models': models or {}}))
    guard.record(directory, guard.snapshot(root, directory), reason='test review', reviewer='tester')


def test_dynamic_clients_read_new_approved_settings_without_restarting(project):
    save(project, {'EMBEDDING_SERVICE_URL': 'http://first:80', 'EMBEDDING_MODEL': 'first',
                   'EMBEDDING_MAX_BATCH_SIZE': '8', 'CLEF_SERVICE_URL': 'http://first:80',
                   'DOCUMENT_PARSE_SERVICE_URL': 'http://first:80'})
    old = RemoteEmbeddingClient()
    save(project, {'EMBEDDING_SERVICE_URL': 'http://second:80', 'EMBEDDING_MODEL': 'second',
                   'EMBEDDING_MAX_BATCH_SIZE': '16', 'CLEF_SERVICE_URL': 'http://second:80',
                   'CLEF_SERVICE_PATH': '/custom', 'DOCUMENT_PARSE_SERVICE_URL': 'http://second:80',
                   'DOCUMENT_PARSE_TIMEOUT_SECONDS': '42', 'NANOJEV_SERVICE_URL': 'http://nano:80'})
    new = RemoteEmbeddingClient()
    assert (new.base_url, new.model, new.max_batch_size) == ('http://second:80', 'second', 16)
    assert old.base_url == 'http://first:80'
    assert RemoteEmbeddingClient(base_url='http://override').base_url == 'http://override'
    assert ClefFlashClient().base_url == 'http://second:80'
    assert ClefFlashClient().path == '/custom'
    assert ClefFlashClient(env_prefix='NANOJEV', capability='models.nanojev.create').base_url == 'http://nano:80'
    assert RemoteDocumentParserClient().timeout == 42


def test_unapproved_dynamic_changes_do_not_reach_clients(project):
    save(project, {})
    (project[1] / 'config.json').write_text('{"settings":{"CLEF_SERVICE_URL":"http://changed"}}')
    with pytest.raises(ValueError):
        ClefFlashClient()


@pytest.mark.parametrize('settings', [
    {'CLEF_SERVICE_URL': 'http://user:secret@host'},
    {'NANOJEV_SERVICE_URL': 'file:///models'},
    {'EMBEDDING_MAX_BATCH_SIZE': '0'},
    {'DOCUMENT_PARSE_TIMEOUT_SECONDS': 'nan'},
    {'CLEF_SERVICE_PATH': '//other-host'},
    {'EMBEDDING_SERVICE_API_KEY': 'secret'},
])
def test_invalid_hot_config_rejected_without_values(project, settings):
    (project[1] / 'config.json').write_text(json.dumps({'settings': settings}))
    with pytest.raises(ValueError, match='Invalid runtime') as error:
        read_document(project[1] / 'config.json')
    assert 'secret' not in str(error.value)


def test_reload_cannot_approve_or_restart(project, monkeypatch):
    commands = []
    monkeypatch.setattr(ops, 'run', lambda command, root: commands.append(command) or 0)
    root, directory = project
    assert ops.main(['--project-dir', str(root), 'reload']) == 2
    assert commands == []
    assert not (directory / 'approved-config.json').exists()
    save(project, {'KG_EXTRACT_LLM_MODEL': 'new-model'})
    assert ops.main(['--project-dir', str(root), 'reload']) == 0
    assert len(commands) == 2
    assert all('exec' in c and '_probe' in c and 'restart' not in c for c in commands)


def test_reload_stops_on_service_failure(project, monkeypatch):
    save(project, {})
    commands = []
    monkeypatch.setattr(ops, 'run', lambda command, root: commands.append(command) or 2)
    assert ops.reload_services(*project, ['backend', 'agent']) == 2
    assert len(commands) == 1


def test_reload_refuses_unapplied_model_plan(project, monkeypatch):
    save(project, {}, {'test/model': {'enabled': True, 'gpu': '5', 'port': '9998'}})
    monkeypatch.setattr(ops, 'run', lambda *args: pytest.fail('Must not execute Docker'))
    with pytest.raises(ops.ControlError, match='尚未部署'):
        ops.reload_services(*project, ['backend'])


def test_probe_detects_startup_changes_without_disclosing_secrets(project, monkeypatch, capsys):
    save(project, {})
    monkeypatch.setenv('GPU_DEVICE_IDS', '4,5')
    monkeypatch.setenv('SERVICE_API_KEY', 'old-key')
    assert ops.main(['--project-dir', str(project[0]), '_probe']) == 2
    output = capsys.readouterr().err
    assert '重新创建容器' in output
    assert 'hidden' not in output and 'old-key' not in output


def test_probe_checks_request_configuration_and_reports_only_keys(project, monkeypatch, capsys):
    save(project, {'KG_EXTRACT_LLM_MODEL': 'private-model-name'})
    monkeypatch.setenv('GPU_DEVICE_IDS', '4,5')
    monkeypatch.setenv('SERVICE_API_KEY', 'hidden')
    calls = []
    monkeypatch.setattr(extraction_config, 'resolve_config', lambda options: calls.append(options))
    assert ops.probe_runtime(*project) == 0
    output = capsys.readouterr().out
    assert calls == [{}]
    assert 'KG_EXTRACT_LLM_MODEL' in output
    assert 'private-model-name' not in output and 'hidden' not in output


def test_packaging_delivers_controller():
    root = Path(__file__).resolve().parents[2]
    script = (root / 'package-deploy.ps1').read_text(encoding='utf-8-sig')
    assert '"ops.py"' in script
    assert "@('ops', 'ops.py', 'ops.ps1')" in script


def test_install_registers_user_command_and_backs_up_shell_files(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.bashrc').write_text('# existing shell config\n')
    monkeypatch.setattr(ops.sys, 'platform', 'linux')
    monkeypatch.setattr(ops.Path, 'home', lambda: home)
    monkeypatch.setattr(ops.shutil, 'which', lambda _: None)
    assert ops.install_command(tmp_path) == 0
    launcher = home / '.local/bin/ops'
    assert 'QASystem managed ops launcher' in launcher.read_text()
    assert str(tmp_path / 'ops.py') in launcher.read_text()
    assert (home / '.bashrc.qasystem-ops.bak').read_text() == '# existing shell config\n'
    assert ops.install_command(tmp_path) == 0
    assert (home / '.bashrc').read_text().count('# QASystem ops user command') == 1


def test_install_refuses_command_name_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.sys, 'platform', 'linux')
    monkeypatch.setattr(ops.shutil, 'which', lambda _: str(tmp_path / 'other/ops'))
    with pytest.raises(ops.ControlError, match='已有其他 ops'):
        ops.install_command(tmp_path)


def test_linux_launcher_is_valid_delivery_script():
    root = Path(__file__).resolve().parents[2]
    content = (root / 'ops').read_bytes()
    assert content.startswith(b'#!/bin/sh\n')
    assert b'\r' not in content
