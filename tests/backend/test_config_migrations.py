import json
from pathlib import Path

import pytest

from docker import configure, configuration
from docker.config_migrations import ALIASES, migrate


ROOT = Path(__file__).resolve().parents[2]


def make_editor(tmp_path, text):
    (tmp_path / '.env').write_text(text, encoding='utf8')
    return configure.Configuration(tmp_path)


@pytest.mark.parametrize('old,new', ALIASES.items())
def test_alias_moves_to_standard_without_values_in_conflicts(old, new):
    values, conflicts = migrate({old: 'private-value'})
    assert values == {new: 'private-value'}
    assert conflicts == {}
    values, conflicts = migrate({old: 'private-value', new: 'other-value'})
    assert conflicts == {new: [old]}
    assert 'private-value' not in repr(conflicts)
    assert old not in values


def test_editor_proposes_migration_without_touching_original(tmp_path):
    original = 'KG_EXTRACT_API_URL=http://model.test/v1\nKG_EXTRACT_MODEL=test-model\nKG_EXTRACT_API_KEY=secret\n'
    editor = make_editor(tmp_path, original)
    assert (tmp_path / '.env').read_text() == original
    proposed = editor.pending_payloads()
    env = configure.helper.read_env(proposed[tmp_path / '.env'])
    registry = json.loads(proposed[editor.directory / 'config.json'])
    assert registry['services'][registry['routes']['extract']]['model'] == 'test-model'
    assert env['KG_EXTRACT_LLM_API_KEY'] == 'secret'
    assert 'KG_EXTRACT_MODEL' not in env and 'KG_EXTRACT_API_KEY' not in env
    assert any('迁移：KG_EXTRACT_API_KEY' in f['description'] for f in editor.fields)


def test_alias_conflict_with_canonical_hot_setting_requires_explicit_choice(tmp_path):
    (tmp_path / 'runtime-config').mkdir()
    (tmp_path / 'runtime-config/config.json').write_text(json.dumps({'settings': {'KG_EXTRACT_LLM_MAX_TOKENS': '64'}, 'models': {}}))
    editor = make_editor(tmp_path, 'KG_EXTRACT_MAX_TOKENS=32\n')
    assert any('冲突' in e for e in editor.validate())
    editor.resolved_migrations.add('KG_EXTRACT_LLM_MAX_TOKENS')
    assert not any('冲突' in e for e in editor.validate())


def test_old_persisted_judge_is_removed_only_in_migration_mode(tmp_path):
    from docker.runtime_config import read_document
    directory = tmp_path / 'runtime-config'
    directory.mkdir()
    path = directory / 'config.json'
    path.write_text(json.dumps({'settings': {'QASYSTEM_JUDGE_MODEL': 'old'}, 'models': {}}))
    with pytest.raises(ValueError):
        read_document(path)
    editor = make_editor(tmp_path, '')
    assert 'QASYSTEM_JUDGE_MODEL' not in editor.pending_payloads()[path]
    assert 'QASYSTEM_JUDGE_MODEL' in path.read_text()


def test_retired_secret_names_cannot_bypass_declarations():
    with pytest.raises(configuration.UndeclaredConfigurationError):
        configuration.getenv('KG_EXTRACT_API_KEY')


def test_current_registry_contains_no_aliases_or_false_revision_config():
    catalog = json.loads((ROOT / 'docker/config-declarations.json').read_text(encoding='utf8'))
    assert not set(ALIASES) & catalog['items'].keys()
    assert 'NANOJEV_REVISION' not in catalog['items']
    assert catalog['aliases'] == ALIASES


def test_old_model_parameters_move_without_enabling_or_changing_paths(tmp_path):
    editor = make_editor(tmp_path, 'LLM_SERVED_MODEL_NAME=Qwen/Qwen3.5-4B\nLLM_GPU_DEVICE_IDS=6\n'
                         'LLM_HOST_PORT=9991\nLLM_MODEL_DIR=custom/weights\nLLM_BIND=0.0.0.0\nLLM_MAX_NUM_SEQS=3\n')
    state = editor.models['Qwen/Qwen3.5-4B']
    assert state['device'] == 'gpu:6' and state['port'] == '9991'
    assert state['model_dir'] == 'custom/weights' and state['bind'] == '0.0.0.0'
    assert not state['enabled']
    assert all(m['max_num_seqs'] == '3' for m in editor.models.values()
               if m['engine'] == 'vllm' and m['adapter'] == 'chat' and m['manager'] == 'ops')
    assert 'LLM_HOST_PORT' not in editor.pending_payloads()[tmp_path / '.env']


def test_conflicting_model_plan_is_visible_even_when_disabled(tmp_path):
    directory = tmp_path / 'runtime-config'
    directory.mkdir()
    repo = 'Qwen/Qwen3.5-4B'
    (directory / 'config.json').write_text(json.dumps({'settings': {}, 'models': {repo: {'port': '9990', 'enabled': False}}}))
    editor = make_editor(tmp_path, 'LLM_HOST_PORT=9991\n')
    assert any('冲突' in e for e in editor.validate())
    rows = editor.rows()
    row = next(r for r in rows if r.get('repo') == repo and r['key'] == 'port')
    assert '冲突' in row['description']
    assert not editor.models[repo]['enabled']


def test_duplicate_standard_assignments_require_choice_then_emit_once(tmp_path):
    editor = make_editor(tmp_path, 'BACKEND_HOST_PORT=8010\nBACKEND_HOST_PORT=8020\n')
    assert any('BACKEND_HOST_PORT' in e and '冲突' in e for e in editor.validate())
    editor.resolved_migrations.add('BACKEND_HOST_PORT')
    proposed = editor.pending_payloads()[tmp_path / '.env']
    assert proposed.count('BACKEND_HOST_PORT=') == 1
    assert configure.helper.read_env(proposed)['BACKEND_HOST_PORT'] == '8020'
