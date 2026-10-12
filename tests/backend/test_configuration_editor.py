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
    assert json.loads((editor.directory / 'config.json').read_text())['settings']['KG_EXTRACT_LLM_MODEL'] == 'test-model'
    env = configure.helper.read_env((editor.root / '.env').read_text())
    assert env['PRIVATE_API_KEY'] == 'never-print'
    assert env['GPU_DEVICE_IDS'] == '4,5,6,7'
    assert env['EXECUTION_GPU_RESERVED_DEVICE_IDS'] == '7'
    assert 'KG_EXTRACT_LLM_MODEL' not in env
    assert json.loads((editor.directory / 'models.compose.json').read_text()) == {'services': {}}
    configure.guard.record(editor.directory, configure.guard.snapshot(editor.root, editor.directory), reason='test', reviewer='tester')
    monkeypatch.setenv('QASYSTEM_RUNTIME_SETTINGS_FILE', str(editor.directory / 'config.json'))
    assert runtime_settings.getenv('KG_EXTRACT_LLM_MODEL') == 'test-model'
    editor.values['KG_EXTRACT_LLM_MODEL'] = 'second-model'
    editor.save()
    configure.guard.record(editor.directory, configure.guard.snapshot(editor.root, editor.directory), reason='test', reviewer='tester')
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
    monkeypatch.setenv('QASYSTEM_RUNTIME_SETTINGS_FILE', str(editor.directory / 'config.json'))
    editor.values.update(KG_EXTRACT_ALGO_VERSION='v3', KG_EXTRACT_LLM_MODEL='first',
                         KG_EXTRACT_LLM_MAX_TOKENS='1024', KG_EXTRACT_LLM_MAX_OUTPUT_TOKENS='1024')
    editor.save()
    configure.guard.record(editor.directory, configure.guard.snapshot(editor.root, editor.directory), reason='test', reviewer='tester')
    first = extraction_config.resolve_config({})
    editor.values.update(KG_EXTRACT_ALGO_VERSION='v2', KG_EXTRACT_LLM_MODEL='second')
    editor.save()
    configure.guard.record(editor.directory, configure.guard.snapshot(editor.root, editor.directory), reason='test', reviewer='tester')
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
    editor.prepare_deployment()
    editor.save()
    configure.guard.record(editor.directory, configure.guard.snapshot(editor.root, editor.directory), reason='test', reviewer='tester')
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
    editor.prepare_deployment()
    editor.save()
    configure.guard.record(editor.directory, configure.guard.snapshot(editor.root, editor.directory), reason='test', reviewer='tester')
    calls = []
    monkeypatch.setattr(configure.helper, 'run', lambda command, **kwargs: calls.append(command) or '')
    editor.apply()
    stops = [c for c in calls if 'stop' in c]
    assert len(stops) == 1
    assert stops[0][-1] == configure.slug(repo)
    assert not any('clef' in c or 'nanojev' in c for c in calls)


def test_busy_gpu_prevents_starting_models(editor, monkeypatch):
    enable(editor, 0)
    editor.prepare_deployment()
    editor.save()
    configure.guard.record(editor.directory, configure.guard.snapshot(editor.root, editor.directory), reason='test', reviewer='tester')
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return '6, A40, 46080, 2000, 10' if 'nvidia-smi' in command else ''
    monkeypatch.setattr(configure.helper, 'run', run)
    with pytest.raises(ValueError, match='正在使用'): editor.apply()
    assert all('up' not in c for c in calls)


def test_host_judge_uses_temporary_cli_settings_and_ignores_persistent_defaults(editor, monkeypatch):
    from eval.run import build_judge_client
    monkeypatch.setenv('QASYSTEM_JUDGE_MODEL', 'stale-model')
    monkeypatch.setenv('QASYSTEM_JUDGE_MAX_TOKENS', '9999')
    params = {'_judge_overrides': {'model': 'local-test', 'base_url': 'http://127.0.0.1:60123/v1',
                                  'adapter': 'chat', 'max_tokens': 512}}
    client = build_judge_client(params)
    assert client.model == 'local-test'
    assert client.max_tokens == 512
    params['_judge_overrides']['model'] = 'override'
    assert build_judge_client(params).model == 'override'


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
    keys = iter(['right', 'right', 'end', 'pageup', '\t', 'q'])
    monkeypatch.setattr(configure, 'read_key', lambda: next(keys))
    original = (editor.root / '.env').read_bytes()
    configure.ui(editor)
    output = capsys.readouterr().out
    assert '[服务]' in output and '[抽取]' in output
    assert output.startswith('\x1b[?1049h')
    assert output.endswith('\x1b[0m\x1b[?25h\x1b[?1049l')
    assert (editor.root / '.env').read_bytes() == original


def test_key_value_columns_have_common_right_edge_and_flag(editor):
    from docker import terminal_ui as screen
    rows = [r for r in editor.rows() if r['kind'] == 'env']
    row = next(r for r in rows if r['key'] == 'BACKEND_HOST_PORT')
    assert row['display_name'] == '[启动] BACKEND_HOST_PORT'
    line = screen.item_line(row, 76)
    assert line.rstrip().endswith('8006')
    assert screen.width(line) == 76
    hot = next(r for r in rows if r['key'] == 'KG_EXTRACT_LLM_MODEL')
    assert hot['display_name'].startswith('[热更] ')
    assert screen.width(screen.item_line(hot, 76)) == 76
    assert screen.width(screen.pair('中文名称', '中文值', 35)) == 35


def test_live_inspection_is_readonly_masks_secrets_and_reports_actual_difference(editor, monkeypatch):
    metadata = {'name': '/qasystem_backend', 'image': 'qasystem-env-v1', 'status': 'running',
        'health': 'healthy', 'ports': {'8000/tcp': [{'HostPort': '8006'}]}, 'mounts': [],
        'command': ['python', '-m', 'app.execution.supervisor']}
    model = {**metadata, 'name': '/qasystem-matrix-qwen35-4b', 'command': ['vllm', 'serve', '/models/4b',
        '--served-model-name', 'Qwen/Qwen3.5-4B', '--max-model-len', '262144', '--api-key', 'never-expose'],
        'ports': {'8000/tcp': [{'HostPort': '9998'}]}}
    raw = '\n'.join(json.dumps(x) for x in [metadata, {'env': 'KG_EXTRACT_LLM_MODEL=actual-model'}, model, {'env': 'CUDA_VISIBLE_DEVICES=6'}])
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if command[0] == 'nvidia-smi': return '6, GPU-test'
        return 'abc def' if command[1] == 'ps' else raw
    monkeypatch.setattr(configure.helper, 'run', run)
    editor.refresh_live()
    assert all(c[0] == 'nvidia-smi' or c[1] in {'ps', 'inspect'} for c in commands)
    assert 'KG_EXTRACT_LLM_API_KEY' not in commands[1][3]
    assert editor.actual_value('KG_EXTRACT_LLM_MODEL') == 'actual-model'
    rows = editor.rows()
    r = next(r for r in rows if r['key'] == 'KG_EXTRACT_LLM_MODEL')
    assert r['display_value'].startswith('! ')
    assert r['actual_value'] == 'actual-model'
    runtime = next(r for r in rows if r['key'] == 'qasystem-matrix-qwen35-4b')
    assert runtime['container']['gpu'] == '6'
    assert runtime['container']['context'] == '262144'
    assert runtime['container']['host_ports'] == '9998'
    assert 'never-expose' not in json.dumps(editor.live)


def test_actual_value_uses_live_mount_not_unapplied_edit_directory(editor, tmp_path):
    actual_dir = tmp_path / 'actual-mount'
    actual_dir.mkdir()
    (actual_dir / 'settings.json').write_text('{"KG_EXTRACT_LLM_MODEL":"mounted-model"}')
    editor.live = [{'name': 'qasystem_backend', 'status': 'running', 'env': {'KG_EXTRACT_LLM_MODEL': 'old-env'},
        'mounts': [{'Destination': '/app/runtime-config', 'Source': str(actual_dir)}]}]
    assert editor.actual_value('KG_EXTRACT_LLM_MODEL') == 'mounted-model'


def test_save_migrates_legacy_files_and_env_without_persisting_template_defaults(editor):
    directory = editor.directory
    (directory / 'settings.json').write_text(json.dumps({'KG_EXTRACT_LLM_MODEL': 'legacy-model'}))
    (directory / 'models.json').write_text('{}')
    with (editor.root / '.env').open('a') as stream:
        stream.write('KG_EXTRACT_LLM_MAX_CONCURRENCY=3\n')
    reopened = configure.Configuration(editor.root)
    assert reopened.values['KG_EXTRACT_LLM_MODEL'] == 'legacy-model'
    reopened.save()
    settings = json.loads((directory / 'config.json').read_text())['settings']
    assert settings == {'KG_EXTRACT_LLM_MODEL': 'legacy-model', 'KG_EXTRACT_LLM_MAX_CONCURRENCY': '3'}
    assert 'KG_EXTRACT_LLM_MAX_CONCURRENCY' not in configure.helper.read_env((editor.root / '.env').read_text())
    assert not (directory / 'settings.json').exists()
    assert not (directory / 'models.json').exists()
    assert (reopened.backup / 'settings.json').is_file()
    assert 'PRIVATE_API_KEY=never-print' in (editor.root / '.env').read_text()


def test_canonical_runtime_file_does_not_overlay_old_files(editor):
    from docker.runtime_config import read_document
    (editor.directory / 'config.json').write_text('{"settings": {}, "models": {}}')
    (editor.directory / 'settings.json').write_text('{"KG_EXTRACT_LLM_MODEL":"stale"}')
    assert read_document(editor.directory / 'config.json') == {'settings': {}, 'models': {}}


@pytest.mark.parametrize('filename', ['settings.json', 'models.json'])
def test_migration_rejects_invalid_legacy_structure(editor, filename):
    from docker.runtime_config import read_document
    (editor.directory / filename).write_text('[]')
    with pytest.raises(ValueError, match='Invalid runtime'):
        read_document(editor.directory / 'config.json')


def test_container_mount_precedes_host_relative_config_dir(monkeypatch):
    from docker.runtime_config import runtime_path
    original = Path.is_dir
    monkeypatch.setattr(Path, 'is_dir', lambda path: str(path).replace('\\', '/') == '/app/runtime-config' or original(path))
    assert str(runtime_path(Path('/app'), {'QASYSTEM_RUNTIME_CONFIG_DIR': './host-only'})).replace('\\', '/') == '/app/runtime-config/config.json'


def test_judge_budget_does_not_inherit_extraction_budget(editor, monkeypatch):
    from eval.run import build_judge_client
    monkeypatch.setenv('QASYSTEM_RUNTIME_SETTINGS_FILE', str(editor.directory / 'config.json'))
    monkeypatch.setenv('KG_EXTRACT_LLM_MAX_OUTPUT_TOKENS', '16384')
    monkeypatch.setenv('KG_EXTRACT_LLM_TIMEOUT_SECONDS', '900')
    monkeypatch.setenv('QASYSTEM_JUDGE_BASE_URL', 'http://127.0.0.1:60123/v1')
    client = build_judge_client({'eval': {'judge_max_tokens': 768, 'judge_timeout_seconds': 45},
                                 '_judge_overrides': {'base_url': 'http://127.0.0.1:60123/v1'}})
    assert client.max_tokens == 768
    assert client.timeout == 45


def test_apply_cannot_run_docker_without_manual_approval(editor):
    with pytest.raises(ValueError, match='人工确认'):
        editor.apply()


def test_ui_cancelled_review_does_not_write_hot_changes(editor, monkeypatch):
    monkeypatch.setattr(configure.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(configure, 'read_key', lambda: 'q')
    original = (editor.root / '.env').read_bytes()
    editor.values['KG_EXTRACT_LLM_MODEL'] = 'pending-only'
    assert configure.review_save(editor) is False
    assert (editor.root / '.env').read_bytes() == original
    assert not (editor.directory / 'config.json').exists()
    assert not (editor.directory / 'approved-config.json').exists()


def test_ui_explicit_review_records_saved_configuration(editor, monkeypatch):
    monkeypatch.setattr(configure.sys.stdin, 'isatty', lambda: True)
    columns, lines = configure.shutil.get_terminal_size((100, 28))
    changes = configure.guard.changes(None, configure.guard.snapshot(editor.root, editor.directory, payloads=editor.pending_payloads()))
    length = sum(len(configure.screen.wrap(line, max(30, columns - 2))) for line in changes)
    pages = configure.math.ceil(length / max(4, lines - 7))
    keys = iter(['pagedown'] * (pages - 1) + ['\r'])
    monkeypatch.setattr(configure, 'read_key', lambda: next(keys))
    answers = iter(['initial deployment review', 'confirm r1'])
    monkeypatch.setattr('builtins.input', lambda *args: next(answers))
    assert configure.review_save(editor)
    assert configure.guard.require(editor.root, editor.directory) == 1
    record = configure.guard.approved(editor.directory)
    assert record['reason'] == 'initial deployment review'
    assert 'never-print' not in json.dumps(record)
