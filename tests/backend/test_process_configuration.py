from pathlib import Path

import pytest

from qasystem_config import configuration, runtime_config
from docker.configure import Configuration


def snapshot(value, revision):
    def read(*args, with_revision=False, **kwargs):
        document = {'settings': {} if value is None else {'EXECUTION_CPU_WORKERS': value}}
        return (document, revision) if with_revision else document
    return read


def test_cpu_plan_getters_never_read_old_container_env(monkeypatch):
    monkeypatch.setenv('EXECUTION_CPU_WORKERS', '31')
    monkeypatch.setattr(runtime_config, 'read_registry', snapshot('2', 7))
    observed = []
    monkeypatch.setattr(configuration, '_observe', lambda *a, **kw: observed.append((a, kw)))
    assert configuration.approved_execution_plan(revision=7) == {'revision': 7, 'cpu_workers': '2'}
    assert configuration.getenv('EXECUTION_CPU_WORKERS', '30') == '2'
    assert configuration.startup_getenv('EXECUTION_CPU_WORKERS', '29') == '2'
    assert all(args[2] == 'process-plan' and kwargs['revision'] == 7 for args, kwargs in observed)


@pytest.mark.parametrize('value,revision,requested', [
    ('2', None, None), ('2', 7, 8), (None, 7, 7), ('0', 7, 7),
    ('33', 7, 7), ('-1', 7, 7), ('1.5', 7, 7), (2, 7, 7), ('bad', 7, 7),
])
def test_cpu_plan_rejects_uncommitted_stale_missing_and_invalid(monkeypatch, value, revision, requested):
    monkeypatch.setattr(runtime_config, 'read_registry', snapshot(value, revision))
    with pytest.raises(ValueError):
        configuration.approved_execution_plan(revision=requested)


def test_cpu_plan_is_persisted_once_even_when_default_unchanged(tmp_path):
    source = Path(__file__).resolve().parents[2]
    (tmp_path/'.env.example').write_text((source/'.env.example').read_text(encoding='utf-8'), encoding='utf-8')
    config = Configuration(tmp_path)
    payloads = config.pending_payloads()
    import json
    plan = json.loads(payloads[config.directory/'config.json'])
    assert plan['settings']['EXECUTION_CPU_WORKERS'] == 'auto'
    assert not any(line.startswith('EXECUTION_CPU_WORKERS=') for line in payloads[tmp_path/'.env'].splitlines())
    row = next(row for row in config.rows() if row.get('key') == 'EXECUTION_CPU_WORKERS')
    assert row['process'] and not row['hot'] and row['display_name'].startswith('[进程]')


def test_cpu_legacy_env_migrates_to_plan_and_not_container_env(tmp_path):
    source = Path(__file__).resolve().parents[2]
    (tmp_path/'.env.example').write_text((source/'.env.example').read_text(encoding='utf-8'), encoding='utf-8')
    (tmp_path/'.env').write_text('EXECUTION_CPU_WORKERS=3\n', encoding='utf-8')
    config = Configuration(tmp_path)
    import json
    payloads = config.pending_payloads()
    assert json.loads(payloads[config.directory/'config.json'])['settings']['EXECUTION_CPU_WORKERS'] == '3'
    assert 'EXECUTION_CPU_WORKERS=' not in payloads[tmp_path/'.env']


def test_ui_never_presents_old_env_cpu_count_as_running(tmp_path):
    source = Path(__file__).resolve().parents[2]
    (tmp_path/'.env.example').write_text((source/'.env.example').read_text(encoding='utf-8'), encoding='utf-8')
    config = Configuration(tmp_path)
    config.live = [{'name': 'qasystem_backend', 'status': 'running', 'env': {'EXECUTION_CPU_WORKERS': '31'}}]
    assert config.actual_value('EXECUTION_CPU_WORKERS').startswith('未确认')
    config.process_status = {'processes': [{'state': 'running'}, {'state': 'draining'}], 'applied_revision': 7}
    actual = config.actual_value('EXECUTION_CPU_WORKERS')
    assert '运行 1' in actual and '排空 1' in actual and 'r7' in actual and '31' not in actual


def test_supervisor_startup_reads_same_committed_plan(monkeypatch):
    from backend.app.execution import supervisor
    monkeypatch.setenv('EXECUTION_CPU_WORKERS', '31')
    monkeypatch.setattr(supervisor, '_gpu_ids', lambda: [])
    monkeypatch.setattr('qasystem_config.model_registry.worker_record', lambda: None)
    monkeypatch.setattr(configuration, 'approved_execution_plan', lambda **kwargs: {'revision': 7, 'cpu_workers': '2'})
    runtime = supervisor.RuntimeSupervisor()
    assert runtime.cpu_worker_count == 2 and runtime.cpu_plan['revision'] == 7


def test_supervisor_cpu_spawn_never_enters_fail_all_collection(monkeypatch):
    from backend.app.execution import supervisor
    monkeypatch.setattr(supervisor, '_gpu_ids', lambda: [])
    monkeypatch.setattr('qasystem_config.model_registry.worker_record', lambda: None)
    monkeypatch.setattr(configuration, 'approved_execution_plan',
                        lambda **kwargs: {'revision': 7, 'cpu_workers': '1'})
    events = []
    monkeypatch.setattr(supervisor.RuntimeSupervisor, '_record_cpu_control', lambda self, event: events.append(event))
    runtime = supervisor.RuntimeSupervisor()
    processes = []
    class Process:
        pid = 123
        def poll(self): return 1
    monkeypatch.setattr(supervisor.subprocess, 'Popen', lambda *args, **kwargs: processes.append(Process()) or processes[-1])
    runtime._start_core_workers()
    assert len(runtime.children) == 1 and runtime.children[0].name == 'api'
    assert runtime.cpu_controller.status()['cpu']['processes'][0]['state'] == 'failed'
    assert events[0]['revision'] == 7 and events[0]['action'] == 'startup'


def test_cpu_getters_fail_closed_without_committed_json(monkeypatch):
    monkeypatch.setenv('EXECUTION_CPU_WORKERS', '2')
    monkeypatch.setattr(runtime_config, 'read_registry', snapshot(None, None))
    for getter in (configuration.getenv, configuration.startup_getenv):
        with pytest.raises(ValueError):
            getter('EXECUTION_CPU_WORKERS', 'auto')


def test_supervisor_development_override_rejected_in_managed_environment(monkeypatch):
    from backend.app.execution import supervisor
    monkeypatch.setattr(supervisor, '_gpu_ids', lambda: [])
    monkeypatch.setenv('ENVIRONMENT', 'production')
    with pytest.raises(ValueError, match='受管部署'):
        supervisor.RuntimeSupervisor(cpu_workers='1')
