import json
from pathlib import Path
import signal
import subprocess
import sys
import time
import threading
import uuid
import pytest

from backend.app.execution import process_control as module


@pytest.fixture
def processes(tmp_path):
    spawned = []
    code = '''import signal,time,sys,pathlib
stop=False
def drain(*args):
 global stop
 stop=True
signal.signal(signal.SIGTERM,drain)
pathlib.Path(sys.argv[1]).write_text('ready')
deadline=time.monotonic()+float(sys.argv[2])
while time.monotonic()<deadline: time.sleep(.01)
while not stop: time.sleep(.01)
'''
    def factory(name, seconds=0):
        ready = tmp_path / (name + '-' + str(len(spawned)))
        process = subprocess.Popen([sys.executable, '-c', code, str(ready), str(seconds)])
        spawned.append(process)
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.01)
        return process
    yield factory, spawned
    for process in spawned:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def wait_command(controller, message):
    deadline = time.monotonic() + 5
    while True:
        result = controller.request(message)['command']
        if result['state'] not in {'accepted', 'running'}:
            return result
        assert time.monotonic() < deadline
        time.sleep(.01)


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX cooperative SIGTERM required')
def test_rolling_preserves_gateway_and_drains_real_attempt(processes):
    factory, spawned = processes
    gateway = factory('gateway')
    controller = module.ProcessController(authorize=lambda role, rev: 2)
    controller.register('cpu', lambda name: factory(name, .3), 2)
    originals = [p.pid for p in spawned[1:]]
    message = dict(action='restart', role='cpu', request_id='r1', revision=1)
    started = time.monotonic()
    assert wait_command(controller, message)['state'] == 'applied'
    assert time.monotonic() - started >= .15
    assert gateway.poll() is None
    assert all(p.poll() == 0 for p in spawned[1:3])
    assert all(p.pid not in originals for p in [c.process for c in controller.roles['cpu']['children']])
    size = len(spawned)
    assert wait_command(controller, message)['state'] == 'applied'
    assert len(spawned) == size


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX cooperative SIGTERM required')
def test_scale_and_timeout_never_duplicate_or_kill(processes):
    factory, spawned = processes
    target = [1]
    controller = module.ProcessController(authorize=lambda role, rev: target[0], drain_timeout=.03)
    controller.register('cpu', lambda name: factory(name, 1), 1)
    target[0] = 3
    assert wait_command(controller, dict(action='apply', role='cpu', request_id='up', revision=1))['state'] == 'applied'
    assert len(spawned) == 3
    target[0] = 1
    assert wait_command(controller, dict(action='apply', role='cpu', request_id='down', revision=1))['state'] == 'failed'
    assert len(spawned) == 3
    assert spawned[1].poll() is None
    assert controller.status()['cpu']['processes'][1]['state'] == 'draining'
    time.sleep(1.1)
    assert wait_command(controller, dict(action='apply', role='cpu', request_id='retry', revision=1))['state'] == 'applied'
    assert len(controller.roles['cpu']['children']) == 1


def test_reject_unapproved_unknown_role_and_id_reuse():
    def authorize(role, revision):
        if revision != 7: raise ValueError('unapproved')
        return 1
    controller = module.ProcessController(authorize=authorize)
    controller.register('cpu', lambda name: None, 0)
    with pytest.raises(ValueError):
        controller.request(dict(action='restart', role='api', request_id='bad', revision=7))
    with pytest.raises(ValueError):
        controller.request(dict(action='restart', role='cpu', request_id='bad', revision=8))
    with pytest.raises(ValueError):
        controller.request(dict(action='shell', role='cpu', request_id='bad', revision=7))


@pytest.mark.skipif(not hasattr(__import__('socket'), 'AF_UNIX'), reason='Unix IPC unavailable')
def test_private_socket_status_and_duplicate_owner(tmp_path):
    controller = module.ProcessController(authorize=lambda role, revision: 1)
    path = tmp_path / 'control.sock'
    server = module.ControlSocket(path, controller)
    server.start()
    try:
        assert module.control_request(path, {'action': 'status'}) == dict(ok=True, roles={}, operations=[])
        duplicate = module.ControlSocket(path, controller)
        with pytest.raises(OSError): duplicate.start()
        duplicate.socket.close()
        assert not module.control_request(path, {'action': 'shell'})['ok']
    finally:
        server.close()


@pytest.mark.parametrize('outcome', ['success', 'cancelled', 'failed'])
def test_actual_worker_drains_inflight_and_acks_real_redis(tmp_path, monkeypatch, outcome):
    redis = pytest.importorskip('redis')
    from backend.app.execution import worker as worker_module
    from backend.app.execution.artifacts import ArtifactStore
    from backend.app.execution.hub import ExecutionEventHub
    from backend.app.execution.manager import ExecutionManager, ExecutionCancelled
    from backend.app.execution.store import RedisExecutionStore
    store = RedisExecutionStore('redis://127.0.0.1:16389/14', prefix='process-drain-test:' + uuid.uuid4().hex)
    try:
        store.redis.ping()
    except redis.RedisError:
        pytest.skip('独立执行层测试 Redis 未运行')
    manager = ExecutionManager(store, ArtifactStore(tmp_path), ExecutionEventHub(store))
    worker = worker_module.WorkerRuntime.__new__(worker_module.WorkerRuntime)
    worker.worker_id, worker.consumer_id = 'cpu-0', 'cpu-0@' + uuid.uuid4().hex
    worker.gpu_device = None
    worker.resource_classes = ('cpu.chunk', 'cpu.graph')
    worker.resource_class = ','.join(worker.resource_classes)
    worker.manager, worker.queue = manager, manager.queue
    worker.stopped, worker._last_heartbeat, worker._reaper_enabled = False, 0, False
    original_getenv = worker_module.config_getenv
    monkeypatch.setattr(worker_module, 'config_getenv', lambda key, default=None:
        '1' if key == 'EXECUTION_MAX_ATTEMPTS' else original_getenv(key, default))
    started, release = threading.Event(), threading.Event()
    def operation(_operation, _payload):
        started.set()
        assert release.wait(5)
        if outcome == 'cancelled':
            raise ExecutionCancelled('synthetic cancellation')
        if outcome == 'failed':
            raise ValueError('synthetic failure')
        return {'completed': True}
    monkeypatch.setattr(worker_module, 'run_operation', operation)
    first = manager.submit(operation='synthetic.first', resource_class='cpu.chunk', payload={})
    second = manager.submit(operation='synthetic.second', resource_class='cpu.chunk', payload={})
    thread = threading.Thread(target=worker.run)
    try:
        thread.start()
        assert started.wait(5)
        worker.stop()
        assert thread.is_alive()  # stop does not cancel or kill the current attempt
        assert manager.store.get_job(first.job_id)['status'] == 'running'
        release.set()
        thread.join(timeout=6)
        assert not thread.is_alive()
        expected = {'success': 'succeeded', 'cancelled': 'cancelled', 'failed': 'failed'}[outcome]
        assert store.get_job(first.job_id)['status'] == expected
        assert store.get_job(second.job_id)['status'] == 'queued'
        stream = worker.queue._stream('cpu.chunk')
        assert worker.queue.redis.xpending(stream, worker.queue.consumer_group)['pending'] == 0
        assert not store.redis.exists(store.prefix + ':workers:' + worker.worker_id)
        remaining = worker.queue.consume('cpu.chunk', 'test-verifier', block_ms=1)
        assert remaining[1]['job_id'] == second.job_id
        worker.queue.ack('cpu.chunk', remaining[0])
    finally:
        worker.stop()
        release.set()
        thread.join(timeout=6)
        keys = list(store.redis.scan_iter(match=store.prefix + ':*'))
        if keys:
            store.redis.delete(*keys)


def test_ops_runtime_uses_approval_revision_and_never_restarts_container(tmp_path, monkeypatch):
    from docker import ops
    approvals, commands = [], []
    monkeypatch.setattr(ops.guard, 'require', lambda root, directory: approvals.append(directory) or 8)
    monkeypatch.setattr(ops, 'run', lambda command, root: commands.append(command) or 0)
    assert ops.main(['--project-dir', str(tmp_path), 'runtime', 'restart', '--request-id', 'repeat-1']) == 0
    assert len(approvals) == 1
    command = commands[0]
    assert 'exec' in command and '-T' in command
    assert 'app.execution.process_control' in command
    assert command[-6:] == ['--role', 'cpu', '--revision', '8', '--request-id', 'repeat-1']
    assert not any(token in command for token in ['down', 'up', '--force-recreate'])


def test_ops_runtime_unapproved_never_contacts_supervisor(tmp_path, monkeypatch):
    from docker import ops
    def reject(*args):
        raise ValueError('unapproved')
    commands = []
    monkeypatch.setattr(ops.guard, 'require', reject)
    monkeypatch.setattr(ops, 'run', lambda command, root: commands.append(command) or 0)
    assert ops.main(['--project-dir', str(tmp_path), 'runtime', 'apply']) == 2
    assert commands == []


def test_cpu_controller_rechecks_approval_even_for_idempotent_request():
    revision = [7]
    def authorize(role, requested):
        if requested != revision[0]:
            raise ValueError('old approval')
        return 1
    controller = module.ProcessController(authorize=authorize)
    controller.register('cpu', lambda _: None, 0)
    message = dict(action='drain', role='cpu', request_id='same', revision=7)
    assert wait_command(controller, message)['state'] == 'applied'
    revision[0] = 8
    with pytest.raises(ValueError):
        controller.request(message)
    with pytest.raises(ValueError):
        controller.request({**message, 'revision': 8})


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX cooperative SIGTERM required')
def test_real_worker_subprocess_rolling_preserves_redis_attempt_and_consumer(tmp_path):
    redis = pytest.importorskip('redis')
    from backend.app.execution.artifacts import ArtifactStore
    from backend.app.execution.hub import ExecutionEventHub
    from backend.app.execution.manager import ExecutionManager
    from backend.app.execution.store import RedisExecutionStore
    url = 'redis://127.0.0.1:16389/14'
    prefix = 'process-subprocess-test:' + uuid.uuid4().hex
    store = RedisExecutionStore(url, prefix=prefix)
    try:
        store.redis.ping()
    except redis.RedisError:
        pytest.skip('独立执行层测试 Redis 未运行')
    manager = ExecutionManager(store, ArtifactStore(tmp_path/'artifacts'), ExecutionEventHub(store))
    source = Path(__file__).resolve().parents[2]
    fixture = Path(__file__).with_name('fixtures')/'process_worker.py'
    spawned, instances = [], []
    def await_condition(check):
        deadline = time.monotonic() + 15
        while not check():
            assert time.monotonic() < deadline
            time.sleep(.02)
    def factory(name):
        ready = tmp_path/('ready-' + str(len(spawned)))
        process = subprocess.Popen([sys.executable, str(fixture), '--source', str(source),
            '--redis-url', url, '--prefix', prefix, '--artifacts', str(tmp_path/'artifacts'), '--ready', str(ready)])
        spawned.append(process)
        await_condition(lambda: ready.is_file() or process.poll() is not None)
        assert process.poll() is None
        instances.append(ready.read_text())
        return process
    controller = module.ProcessController(authorize=lambda role, revision: 1, drain_timeout=15)
    try:
        controller.register('cpu', factory, 1)
        first = manager.submit(operation='test.first', resource_class='cpu.chunk', payload={'marker':'first'})
        await_condition(lambda: store.redis.exists(prefix + ':started:first'))
        second = manager.submit(operation='test.second', resource_class='cpu.chunk', payload={'marker':'second'})
        message = dict(action='restart', role='cpu', request_id='real-worker', revision=7)
        controller.request(message)
        await_condition(lambda: controller.status()['cpu']['processes'][0]['state'] == 'draining')
        assert store.get_job(first.job_id)['status'] == 'running'
        assert not store.redis.exists(prefix + ':started:second')
        assert spawned[0].poll() is None
        store.redis.set(prefix + ':release:first', '1', ex=30)
        await_condition(lambda: controller.request(message)['command']['state'] not in {'accepted', 'running'})
        assert controller.request(message)['command']['state'] == 'applied'
        assert spawned[0].poll() == 0
        assert len(spawned) == 2 and instances[0] != instances[1]
        assert store.get_job(first.job_id)['status'] == 'succeeded'
        await_condition(lambda: store.redis.exists(prefix + ':started:second'))
        store.redis.set(prefix + ':release:second', '1', ex=30)
        await_condition(lambda: store.get_job(second.job_id)['status'] == 'succeeded')
        stream = manager.queue._stream('cpu.chunk')
        await_condition(lambda: store.redis.xpending(stream, manager.queue.consumer_group)['pending'] == 0)
        assert len(store.list_executions(first.job_id)) == len(store.list_executions(second.job_id)) == 1
        assert controller.request(message)['command']['state'] == 'applied' and len(spawned) == 2
    finally:
        controller.begin_shutdown()
        for process in spawned:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        keys = list(store.redis.scan_iter(match=prefix + ':*'))
        if keys:
            store.redis.delete(*keys)
