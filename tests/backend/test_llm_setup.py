import json
from pathlib import Path

import pytest

from docker import setup_llm as wizard


def test_env_update_preserves_secrets_and_gpu_pool_and_removes_duplicate_keys():
    text = '# preserved\nAPI_KEY="secret"\nGPU_DEVICE_IDS=4,5,6,7\nLLM_MODEL_DIR=old\nLLM_MODEL_DIR=duplicate\n'
    new = wizard.update_env(text, {"LLM_MODEL_DIR": "llm/a b", "EXECUTION_GPU_RESERVED_DEVICE_IDS": "6,7"})
    values = wizard.read_env(new)
    assert values["API_KEY"] == "secret"
    assert values["GPU_DEVICE_IDS"] == "4,5,6,7"
    assert values["LLM_MODEL_DIR"] == "llm/a b"
    assert new.count("LLM_MODEL_DIR=") == 1
    assert new.startswith('# preserved\nAPI_KEY="secret"\n')


@pytest.mark.parametrize("selected,changes", [
    ("0", {}), ("7", {}), ("6", {"used": 1024}), ("6", {"utilization": 1}),
    ("6", {"total": 4096}),
])
def test_gpu_preflight_rejects_disallowed_reserved_busy_and_undersized_cards(selected, changes):
    gpus = [{"id": "6", "name": "A40", "total": 46080, "used": 0, "utilization": 0, **changes},
            {"id": "7", "name": "A40", "total": 46080, "used": 0, "utilization": 0}]
    with pytest.raises(ValueError):
        wizard.choose_gpu(gpus, selected, {"GPU_DEVICE_IDS": "4,5,6,7",
                                         "EXECUTION_GPU_RESERVED_DEVICE_IDS": "7"}, 8 * 1024**3)


def test_gpu_probe_parses_actual_nvidia_csv():
    rows = wizard.gpu_rows('6, NVIDIA A40, 46080, 0, 0\n')
    assert wizard.choose_gpu(rows, "6", {"GPU_DEVICE_IDS": "4,5,6,7"}, 8 * 1024**3)["id"] == "6"


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / 'docker').mkdir()
    for relative in ('docker-compose.yml', 'docker/docker-compose.llm.yml'):
        (tmp_path / relative).write_text('services: {}')
    env = 'GPU_DEVICE_IDS=4,5,6,7\nEXECUTION_GPU_RESERVED_DEVICE_IDS=7\nAPI_KEY=private\n'
    (tmp_path / '.env').write_text(env)
    (tmp_path / '.env.example').write_text(env + 'LLM_GPU_MEMORY_UTILIZATION=0.90\n')
    model = {"repo": "Qwen/Qwen3.5-4B", "files": []}
    fake_script = tmp_path / 'docker/setup_llm.py'
    monkeypatch.setattr(wizard, '__file__', str(fake_script))
    (fake_script.with_name('llm-models.json')).write_text(json.dumps([model]))
    model_root = tmp_path / 'models'
    (model_root / 'llm/Qwen--Qwen3.5-4B').mkdir(parents=True)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if 'nvidia-smi' in command:
            return '6, NVIDIA A40, 46080, 0, 0'
        return ''

    monkeypatch.setattr(wizard, 'run', run)
    monkeypatch.setattr(wizard, 'wait_ready', lambda *a: None)
    # Bind a system-assigned free port for deterministic selection.
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    answers = [str(model_root), '1', '6', str(port), '32768', 'y']
    return tmp_path, env, calls, answers


def answer(monkeypatch, answers):
    iterator = iter(answers)
    monkeypatch.setattr(wizard, 'prompt', lambda *a: next(iterator))


def test_cancel_leaves_environment_and_services_untouched(project, monkeypatch):
    root, env, calls, answers = project
    answer(monkeypatch, answers + ['no'])
    assert wizard.main(['--project-dir', str(root)]) == 0
    assert (root / '.env').read_text() == env
    assert not any('up' in command for command in calls)


def test_apply_reserves_gpu_before_starting_model_and_sets_existing_judge_config(project, monkeypatch):
    root, env, calls, answers = project
    answer(monkeypatch, answers + ['yes'])
    assert wizard.main(['--project-dir', str(root)]) == 0
    values = wizard.read_env((root / '.env').read_text())
    assert values['GPU_DEVICE_IDS'] == '4,5,6,7'
    assert values['EXECUTION_GPU_RESERVED_DEVICE_IDS'] == '6,7'
    assert values['QASYSTEM_JUDGE_BASE_URL'] == 'http://llm:8000/v1'
    assert values['API_KEY'] == 'private'
    assert next(root.glob('.env.backup-*')).read_text() == env
    deployments = [command for command in calls if 'up' in command]
    assert deployments[0][-2:] == ['backend', 'agent']
    assert deployments[1][-1] == 'llm'
    assert all('--no-deps' in command for command in deployments)
    assert sum('nvidia-smi' in command for command in calls) == 2


def test_invalid_compose_restores_original_environment_without_restarting(project, monkeypatch):
    root, env, calls, answers = project
    original_run = wizard.run

    def run(command, **kwargs):
        if 'config' in command:
            raise RuntimeError('invalid compose')
        return original_run(command, **kwargs)

    monkeypatch.setattr(wizard, 'run', run)
    answer(monkeypatch, answers + ['yes'])
    assert wizard.main(['--project-dir', str(root)]) == 1
    assert (root / '.env').read_text() == env
    assert not any('up' in command for command in calls)


def test_start_failure_retains_live_configuration_and_never_stops_other_services(project, monkeypatch):
    root, _, calls, answers = project
    original_run = wizard.run

    def run(command, **kwargs):
        result = original_run(command, **kwargs)
        if command[-1] == 'llm' and 'up' in command:
            raise RuntimeError('startup failed')
        return result

    monkeypatch.setattr(wizard, 'run', run)
    answer(monkeypatch, answers + ['yes'])
    assert wizard.main(['--project-dir', str(root)]) == 1
    assert wizard.read_env((root / '.env').read_text())['LLM_GPU_DEVICE_IDS'] == '6'
    assert not any('stop' in command or 'down' in command for command in calls)


def test_existing_llm_is_not_replaced_and_no_configuration_is_changed(project, monkeypatch):
    root, env, calls, _ = project
    original_run = wizard.run

    def run(command, **kwargs):
        if '--filter' in command:
            return 'existing-llm'
        return original_run(command, **kwargs)

    monkeypatch.setattr(wizard, 'run', run)
    monkeypatch.setattr(wizard, 'prompt', lambda *a: pytest.fail('Must stop before requesting changes'))
    assert wizard.main(['--project-dir', str(root)]) == 1
    assert (root / '.env').read_text() == env
    assert not any('up' in command for command in calls)


def test_gpu_becoming_busy_during_selection_does_not_apply_configuration(project, monkeypatch):
    root, env, calls, answers = project
    original_run = wizard.run
    probes = []

    def run(command, **kwargs):
        value = original_run(command, **kwargs)
        if 'nvidia-smi' in command:
            probes.append(1)
            if len(probes) == 2:
                return '6, NVIDIA A40, 46080, 5000, 40'
        return value

    monkeypatch.setattr(wizard, 'run', run)
    answer(monkeypatch, answers + ['yes'])
    assert wizard.main(['--project-dir', str(root)]) == 1
    assert (root / '.env').read_text() == env
    assert not any('up' in command for command in calls)


@pytest.mark.parametrize('budget', ['nan', 'inf', '0', '1'])
def test_invalid_memory_budget_is_rejected(budget):
    with pytest.raises(ValueError, match='LLM_GPU_MEMORY_UTILIZATION'):
        wizard.choose_gpu([{'id': '6', 'name': 'A40', 'total': 46080, 'used': 0, 'utilization': 0}],
                          '6', {'LLM_GPU_MEMORY_UTILIZATION': budget}, 1)


def test_wrong_served_model_cannot_pass_readiness(project, monkeypatch):
    root, _, _, _ = project
    monkeypatch.setattr(wizard, 'run', lambda *a, **k: '["different-model"]')
    # The fixture replaces wait_ready; reload the real function from its module.
    import importlib.util
    source = Path(__file__).resolve().parents[2] / 'docker/setup_llm.py'
    spec = importlib.util.spec_from_file_location('setup_readiness', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'run', lambda *a, **k: '["different-model"]')
    with pytest.raises(ValueError, match='模型名'):
        module.wait_ready(['docker', 'compose'], root, 'expected')
