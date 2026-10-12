import json
from pathlib import Path

import pytest

from docker import configure
from algorithm import runtime_settings
from backend.app.services import extraction_config


@pytest.fixture
def editor(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    (tmp_path / '.env.example').write_bytes((root / '.env.example').read_bytes())
    (tmp_path / '.env').write_text('GPU_DEVICE_IDS=4,5,6,7\nEXECUTION_GPU_RESERVED_DEVICE_IDS=7\nHOST_MODELS_DIR=./models\nPRIVATE_API_KEY=never-print\n')
    (tmp_path / 'docker-compose.yml').write_text('services: {}')
    monkeypatch.setattr(configure.helper, 'run', lambda *a, **kw: pytest.fail('Opening or saving must not execute Docker'))
    return configure.Configuration(tmp_path)


def test_catalog_covers_every_release_key_and_masks_secrets(editor):
    assert {r['key'] for r in editor.fields} == set(configure.helper.read_env(editor.template))
    assert configure.HOT_KEYS == runtime_settings.HOT_KEYS
    rows = editor.rows()
    assert all(not editor.models[m['repo']]['enabled'] for m in editor.catalog)
    assert all('never-print' not in r['label'] for r in rows)
    assert editor.backup.joinpath('deployment.env').read_text() == editor.original


def test_save_generates_defaults_and_hot_file_without_deploy(editor, monkeypatch):
    editor.values['KG_EXTRACT_LLM_MODEL'] = 'test-model'
    editor.save()
    assert json.loads((editor.directory / 'settings.json').read_text())['KG_EXTRACT_LLM_MODEL'] == 'test-model'
    env = configure.helper.read_env((editor.root / '.env').read_text())
    assert env['PRIVATE_API_KEY'] == 'never-print'
    assert env['GPU_DEVICE_IDS'] == '4,5,6,7'
    assert env['EXECUTION_GPU_RESERVED_DEVICE_IDS'] == '7'
    assert json.loads((editor.directory / 'models.compose.json').read_text()) == {'services': {}}
    monkeypatch.setenv('QASYSTEM_RUNTIME_SETTINGS_FILE', str(editor.directory / 'settings.json'))
    assert runtime_settings.getenv('KG_EXTRACT_LLM_MODEL') == 'test-model'
    editor.values['KG_EXTRACT_LLM_MODEL'] = 'second-model'
    editor.save()
    assert runtime_settings.getenv('KG_EXTRACT_LLM_MODEL') == 'second-model'


def enable(editor, index, gpu='6', port='60123'):
    entry = editor.catalog[index]
    directory = editor.model_root() / 'llm' / entry['repo'].replace('/', '--')
    directory.mkdir(parents=True)
    editor.models[entry['repo']].update(enabled=True, gpu=gpu, port=port)
    return entry['repo']


def test_multiple_models_detect_gpu_and_port_collisions(editor):
    enable(editor, 0)
    enable(editor, 1)
    errors = editor.validate()
    assert any('端口重复' in e for e in errors)
    assert any('使用同一卡' in e for e in errors)


def test_saved_plan_does_not_reserve_gpu_or_count_as_deployed(editor):
    enable(editor, 0)
    editor.save()
    reopened = configure.Configuration(editor.root)
    assert reopened.validate() == []
    assert reopened.previous_models == {}
    assert reopened.values['EXECUTION_GPU_RESERVED_DEVICE_IDS'] == '7'


def test_conflict_does_not_overwrite_original(editor):
    enable(editor, 0, gpu='7')
    original = (editor.root / '.env').read_bytes()
    with pytest.raises(ValueError, match='预留'): editor.save()
    assert (editor.root / '.env').read_bytes() == original


def test_concurrent_configuration_edit_is_preserved(editor):
    (editor.root / '.env').write_text('OTHER_CONFIG=changed\n')
    with pytest.raises(ValueError, match='其他进程'): editor.save()
    assert (editor.root / '.env').read_text() == 'OTHER_CONFIG=changed\n'


def test_runtime_rejects_secrets_and_malformed_file(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    monkeypatch.setenv('QASYSTEM_RUNTIME_SETTINGS_FILE', str(path))
    monkeypatch.setenv('KG_EXTRACT_LLM_MODEL', 'fallback')
    assert runtime_settings.getenv('KG_EXTRACT_LLM_MODEL') == 'fallback'
    for text in ('{', '{"KG_EXTRACT_LLM_API_KEY":"secret"}', '[]'):
        path.write_text(text)
        with pytest.raises(ValueError, match='Invalid runtime'): runtime_settings.read_settings()


def test_new_requests_reload_model_and_version_existing_snapshot_is_stable(editor, monkeypatch):
    monkeypatch.setenv('QASYSTEM_RUNTIME_SETTINGS_FILE', str(editor.directory / 'settings.json'))
    editor.values.update(KG_EXTRACT_ALGO_VERSION='v3', KG_EXTRACT_LLM_MODEL='first',
                         KG_EXTRACT_LLM_MAX_TOKENS='1024', KG_EXTRACT_LLM_MAX_OUTPUT_TOKENS='1024')
    editor.save()
    first = extraction_config.resolve_config({})
    editor.values.update(KG_EXTRACT_ALGO_VERSION='v2', KG_EXTRACT_LLM_MODEL='second')
    editor.save()
    second = extraction_config.resolve_config({})
    assert (first['model'], first['version'], first['max_tokens']) == ('first', 'v3', 1024)
    assert (second['model'], second['version']) == ('second', 'v2')


def test_model_compose_uses_unified_image_offline_readonly_weights(editor):
    repo = enable(editor, 0)
    service = editor.compose()['services'][configure.slug(repo)]
    assert service['image'] == 'qasystem-env-v1'
    assert service['volumes'][0].endswith(':/models:ro')
    assert service['environment']['HF_HUB_OFFLINE'] == '1'
    assert service['pull_policy'] == 'never'


def test_old_entry_point_opens_editor_without_legacy_checks(monkeypatch):
    from docker import setup_llm
    calls = []
    monkeypatch.setattr(configure, 'main', lambda args: calls.append(args) or 0)
    assert setup_llm.main(['--project-dir', '/deployment']) == 0
    assert calls == [['--project-dir', '/deployment']]


def test_apply_reserves_gpu_before_recreating_existing_backend(editor, monkeypatch):
    enable(editor, 0)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if '--query-gpu=index,name,memory.total,memory.used,utilization.gpu' in command:
            return '6, A40, 46080, 0, 0'
        if '--force-recreate' in command:
            current = configure.helper.read_env((editor.root / '.env').read_text())
            assert current['EXECUTION_GPU_RESERVED_DEVICE_IDS'] == '6,7'
        return ''
    monkeypatch.setattr(configure.helper, 'run', run)
    editor.apply()
    commands = [c for c in calls if 'up' in c]
    assert commands[0][-2:] == ['backend', 'agent']
    assert commands[1][-1].startswith('llm_')
    assert all('build' not in c and 'pull' not in c for c in calls)
    assert (editor.directory / 'applied-models.json').exists()


def test_disabled_model_stops_only_previously_managed_service(editor, monkeypatch):
    repo = enable(editor, 0)
    editor.previous_models = json.loads(json.dumps(editor.models))
    configure.helper.atomic_write(editor.directory / 'applied.compose.json', json.dumps(editor.compose()))
    editor.models[repo]['enabled'] = False
    calls = []
    monkeypatch.setattr(configure.helper, 'run', lambda command, **kwargs: calls.append(command) or '')
    editor.apply()
    stops = [c for c in calls if 'stop' in c]
    assert len(stops) == 1
    assert stops[0][-1] == configure.slug(repo)
    assert not any('clef' in c or 'nanojev' in c for c in calls)


def test_busy_gpu_prevents_starting_models(editor, monkeypatch):
    enable(editor, 0)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return '6, A40, 46080, 2000, 10' if 'nvidia-smi' in command else ''
    monkeypatch.setattr(configure.helper, 'run', run)
    with pytest.raises(ValueError, match='正在使用'): editor.apply()
    assert all('up' not in c for c in calls)


def test_host_judge_uses_runtime_settings_and_explicit_override_wins(editor, monkeypatch):
    from eval.run import build_judge_client
    editor.values.update(QASYSTEM_JUDGE_MODEL='local-test', QASYSTEM_JUDGE_BASE_URL='http://127.0.0.1:60123/v1',
                         QASYSTEM_JUDGE_ADAPTER='chat', QASYSTEM_JUDGE_MAX_TOKENS='512')
    editor.save()
    monkeypatch.setenv('QASYSTEM_RUNTIME_SETTINGS_FILE', str(editor.directory / 'settings.json'))
    client = build_judge_client({})
    assert client.model == 'local-test'
    assert client.max_tokens == 512
    assert build_judge_client({'_judge_overrides': {'model': 'override'}}).model == 'override'


@pytest.mark.parametrize('columns,lines', [(56, 18), (80, 24), (109, 28), (110, 24), (120, 32), (200, 60)])
def test_terminal_layout_fits_cjk_text_and_preserves_action_bar(editor, columns, lines):
    from docker import terminal_ui as screen
    rows = editor.rows()
    frame = screen.render(editor, rows, 0, '模型', '', '保存不会部署；' * 30, columns, lines)
    assert len(frame.splitlines()) <= lines
    assert all(screen.width(line) <= columns - 1 for line in frame.splitlines())
    assert 's 保存' in frame
    assert 'a 部署' in frame
    assert '\x1b[1;30;46m' in frame


def test_terminal_detail_masks_current_and_default_credentials(editor):
    from docker import terminal_ui as screen
    editor.values['KG_EXTRACT_LLM_API_KEY'] = 'do-not-expose'
    row = next(r for r in editor.rows() if r['key'] == 'KG_EXTRACT_LLM_API_KEY')
    frame = screen.render(editor, [row], 0, '抽取', '', '', 120, 32)
    assert 'do-not-expose' not in frame
    assert '******' in frame


def test_terminal_category_filters_and_plain_color_mode(editor):
    from docker import terminal_ui as screen
    models = screen.filter_rows(editor.rows(), '模型')
    assert any(r['kind'] == 'model' for r in models)
    assert not any(r['key'] == 'BACKEND_HOST_PORT' for r in models)
    judge = screen.filter_rows(editor.rows(), '裁判')
    assert all(r['key'].startswith('QASYSTEM_JUDGE_') for r in judge)
    frame = screen.render(editor, judge, 0, '裁判', '', '', 120, 32, color=False)
    assert '\x1b' not in frame


def test_terminal_sanitizes_control_characters_and_handles_tiny_windows(editor):
    from docker import terminal_ui as screen
    assert '\x1b' not in screen.fit('text\x1b[2J\nmalicious', 40)
    assert screen.width(screen.fit('中文参数目录' * 20, 15)) == 15
    frame = screen.render(editor, [], 0, '模型', '', '', 40, 12)
    assert '终端过小' in frame
    assert all(screen.width(line) <= 39 for line in frame.splitlines())


def test_keyboard_ui_navigation_restores_terminal_without_saving(editor, monkeypatch, capsys):
    keys = iter(['right', 'end', 'pageup', '\t', 'q'])
    monkeypatch.setattr(configure, 'read_key', lambda: next(keys))
    original = (editor.root / '.env').read_bytes()
    configure.ui(editor)
    output = capsys.readouterr().out
    assert '[服务]' in output and '[抽取]' in output
    assert output.startswith('\x1b[?1049h')
    assert output.endswith('\x1b[0m\x1b[?25h\x1b[?1049l')
    assert (editor.root / '.env').read_bytes() == original
