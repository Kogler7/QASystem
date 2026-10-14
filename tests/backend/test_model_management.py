from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from docker import configure, model_services as engines
from docker.config_migrations import migrate_inference
from docker.runtime_config import MODEL_DEFAULTS


def record(**values):
    result = deepcopy(MODEL_DEFAULTS)
    result.update(values)
    return result


@pytest.fixture
def editor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return configure.Configuration(tmp_path)


def native_files(path, adapter='clef'):
    path.mkdir(parents=True)
    if adapter == 'clef':
        names = ['joint_schema_model.py', 'joint_head.safetensors', 'joint_head_config.json',
                 'config.json', 'tokenizer.json', 'tokenizer_config.json', 'processor_config.json']
    else:
        names = ['source/scripts/predict_toy_decisions.py', 'source/scripts/train_toy_decisions.py', 'best.safetensors', 'config.json']
        for folder in ('backbone_config', 'tokenizer'):
            (path / folder).mkdir(); (path / folder / 'fixture').touch()
    for name in names:
        destination = path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text('{}')


def test_directory_discovery_never_enables_any_model(editor):
    (editor.model_root() / 'clef-flash').mkdir(parents=True)
    assert any('权重已找到' in row['display_value'] for row in editor.rows())
    assert not any(model['enabled'] for model in editor.models.values())
    assert editor.compose()['services'] == {}


def test_new_same_engine_model_needs_record_only_not_env(editor):
    before = set(editor.values)
    editor.models['another/chat'] = record(enabled=True, device='gpu:4', model_dir='custom/chat')
    assert engines.startup_command('another/chat', editor.models['another/chat'])[:2] == ['vllm', 'serve']
    assert engines.service_name('another/chat') in editor.compose()['services']
    assert set(editor.values) == before


def test_native_cpu_and_shared_gpu_budgets(editor):
    for index, name in enumerate(('clef-flash', 'nanojev')):
        model = editor.models[name]
        model.update(enabled=True, device='gpu:6', port=str(60121 + index), utilization='0.3')
        (editor.model_root() / model['model_dir']).mkdir(parents=True)
    assert editor.validate() == []
    editor.models['nanojev']['utilization'] = '0.8'
    assert any('显存预算超过' in error for error in editor.validate())
    editor.models['nanojev']['device'] = 'cpu'
    assert editor.validate() == []
    service = editor.compose()['services'][engines.service_name('nanojev')]
    assert 'gpus' not in service and service['environment']['CUDA_VISIBLE_DEVICES'] == ''


def test_migration_conflicts_are_proposals_never_enable_native():
    models, services, routes, conflicts = migrate_inference(
        {'CLEF_MODEL_DIR': '/models/clef-flash', 'CLEF_DEVICE': 'cpu', 'CLEF_CUDA_VISIBLE_DEVICES': '7'}, {}, {}, {})
    assert 'clef-flash.device' in conflicts
    assert not models['clef-flash'].get('enabled', False)
    assert models['clef-flash']['model_dir'] == 'clef-flash'
    assert services['clef-flash']['kind'] == 'local'


def test_existing_record_is_not_overwritten_by_old_env():
    old = {'clef-flash': record(engine='native', adapter='clef', model_dir='changed', device='cpu')}
    models, _, _, conflicts = migrate_inference({'CLEF_MODEL_DIR': '/models/original'}, old, {}, {})
    assert 'clef-flash.model_dir' in conflicts
    assert models['clef-flash']['model_dir'] == 'changed'


def test_worker_has_exactly_one_manager_and_no_ops_instance(editor):
    model = editor.models['mineru-vlm']
    model['enabled'] = True
    assert engines.service_name('mineru-vlm') not in editor.compose()['services']
    with pytest.raises(ValueError, match='禁止 ops'):
        engines.startup_command('mineru-vlm', model)
    with pytest.raises(ValueError, match='Worker'):
        engines.local_model_endpoint('mineru-vlm', model)


@pytest.mark.parametrize('adapter', ['clef', 'nanojev'])
def test_actual_required_model_files_are_checked_before_start(tmp_path, adapter):
    root = tmp_path / 'models'
    path = root / 'checkpoint'
    path.mkdir(parents=True)
    model = record(engine='native', adapter=adapter, device='cpu', model_dir='checkpoint')
    with pytest.raises(ValueError, match='不完整'):
        engines.validate_record(adapter, model, root=root, check_files=True)
    path.rmdir()
    native_files(path, adapter)
    engines.validate_record(adapter, model, root=root, check_files=True)


def test_sharded_weights_do_not_pass_with_missing_shards(tmp_path):
    path = tmp_path / 'model'; path.mkdir()
    (path / 'config.json').write_text('{}')
    (path / 'model.safetensors.index.json').write_text(json.dumps({'weight_map': {'x': 'missing.safetensors'}}))
    model = record(device='gpu:4', model_dir='model')
    with pytest.raises(ValueError, match='分片权重不完整'):
        engines.validate_record('chat', model, root=tmp_path, check_files=True)


def test_native_multiple_instances_serve_canonical_adapter_model_name():
    for name in ('clef-cpu', 'clef-gpu'):
        endpoint = engines.local_model_endpoint(name, record(engine='native', adapter='clef'))
        assert endpoint['model'] == 'clef-flash'
        assert endpoint['base_url'] == 'http://' + engines.service_name(name) + ':8000'


def test_missing_gpu_cannot_fall_back_to_cpu(tmp_path, monkeypatch):
    native_files(tmp_path / 'checkpoint')
    model = record(enabled=True, engine='native', adapter='clef', device='gpu:4', model_dir='checkpoint')
    monkeypatch.setattr(engines, '_SELECTED', None)
    monkeypatch.setattr(engines, 'selected_native', lambda _: model)
    original = engines.validate_record
    monkeypatch.setattr(engines, 'validate_record', lambda name, state, **kw: original(name, state, root=tmp_path, check_files=True))
    monkeypatch.setattr(sys, 'argv', ['clef', '--record', 'checkpoint', '--container'])
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '')
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    with pytest.raises(RuntimeError, match='不会回退'):
        engines.native_main('clef')


def test_duplicate_worker_engine_start_is_rejected(monkeypatch):
    from backend.app.execution.supervisor import RuntimeSupervisor
    monkeypatch.setattr('backend.app.execution.supervisor._gpu_ids', lambda: [])
    monkeypatch.setattr('backend.app.execution.supervisor.is_remote_only', lambda: False)
    supervisor = RuntimeSupervisor()
    supervisor.engines = [('4', 8100, object())]
    with pytest.raises(RuntimeError, match='重复启动'):
        supervisor._start_engines()


def test_old_native_internal_bind_requires_explicit_host_bind_review():
    models, _, _, conflicts = migrate_inference({'CLEF_BIND': '0.0.0.0'}, {}, {}, {})
    assert 'clef-flash.bind' in conflicts
    assert models['clef-flash']['bind'] == '127.0.0.1'


def test_native_uuid_can_be_bound_without_internal_cuda_index_configuration():
    model = record(engine='native', adapter='clef', device='gpu:GPU-aaaa-bbbb')
    engines.validate_record('clef', model)
    assert engines.physical_gpu(model) == 'GPU-aaaa-bbbb'
