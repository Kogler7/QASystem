import time
import uuid
from types import SimpleNamespace

import pytest

from backend.app.execution.artifacts import ArtifactStore
from backend.app.execution.hub import ExecutionEventHub
from backend.app.execution.manager import ExecutionManager, ExecutionCancelled, ExecutionLeaseLost
from backend.app.execution.queue import RedisTaskQueue
from backend.app.execution.resources import ResourceScheduler, GpuSnapshot
from backend.app.execution.store import MemoryExecutionStore
from backend.app.execution.worker import WorkerRuntime, _error_retryable
from qasystem_config.startup_contract import EXECUTION_LIVENESS


@pytest.fixture
def active_job(tmp_path, monkeypatch):
    store = MemoryExecutionStore()
    manager = ExecutionManager(store, ArtifactStore(tmp_path), ExecutionEventHub(store))
    manager.mode, manager.allow_memory_queue = 'async', True
    manager.scheduler.prefix = 'liveness-' + uuid.uuid4().hex
    manager.scheduler.lease_ttl_seconds = .5
    device = GpuSnapshot('0', None, 4096, 0, 0, 'unit')
    lease_id = uuid.uuid4().hex
    lease = dict(lease_id=lease_id, physical_index='0', worker_id='worker')
    def allocate(*_):
        assert manager.scheduler._reserve(device, lease_id, 32, worker_id='worker')
        return {'worker_id': 'worker', 'gpu_allocations': [lease]}
    monkeypatch.setattr(manager, '_allocation', allocate)
    submission = manager.submit(operation='unit', resource_class='gpu.vlm', payload={})
    job = store.get_job(submission.job_id)
    envelope = dict(job_id=submission.job_id, payload_ref=job['payload_ref'], resource_class='gpu.vlm', attempt=1)
    yield manager, store, envelope, lease
    manager.scheduler.release(lease)


def test_long_task_renews_reservation_and_releases_after_completion(active_job):
    manager, store, envelope, lease = active_job
    def runner(_):
        time.sleep(1.6)  # More than three lease lifetimes; no task timeout implied.
        assert manager.scheduler.renew(lease)
        return {'ok': True}
    assert manager.run_attempt(envelope['job_id'], envelope, runner) == {'ok': True}
    assert store.get_job(envelope['job_id'])['status'] == 'succeeded'
    assert not manager.scheduler.renew(lease)


@pytest.mark.parametrize('error', [RuntimeError('failure'), ExecutionCancelled('cancelled')])
def test_failed_or_cancelled_task_releases_reservation(active_job, error):
    manager, store, envelope, lease = active_job
    def runner(_):
        time.sleep(.2)
        raise error
    with pytest.raises(type(error)):
        manager.run_attempt(envelope['job_id'], envelope, runner)
    assert store.get_job(envelope['job_id'])['status'] == ('cancelled' if isinstance(error, ExecutionCancelled) else 'failed')
    assert not manager.scheduler.renew(lease)


@pytest.mark.parametrize('failure', [False, ConnectionError('redis unavailable')])
def test_failed_renewal_discards_result_without_automatic_replay(active_job, monkeypatch, failure):
    manager, store, envelope, lease = active_job
    def renew(*_, **__):
        if isinstance(failure, Exception): raise failure
        return failure
    monkeypatch.setattr(manager.scheduler, 'renew', renew)
    with pytest.raises(ExecutionLeaseLost):
        manager.run_attempt(envelope['job_id'], envelope, lambda _: {'must_not_publish': True})
    job = store.get_job(envelope['job_id'])
    assert job['status'] == 'failed' and not job.get('result_ref')
    assert not _error_retryable(ExecutionLeaseLost('lost'))


def test_expired_lease_is_not_resurrected(active_job):
    manager, _, _, lease = active_job
    manager._allocation('gpu.vlm', {})
    time.sleep(.6)
    assert not manager.scheduler.renew(lease)


@pytest.fixture
def redis_queue():
    redis = pytest.importorskip('redis')
    client = redis.Redis(host='127.0.0.1', port=16389, db=14, decode_responses=True,
                         socket_connect_timeout=.3, socket_timeout=2)
    try: client.ping()
    except redis.RedisError: pytest.skip('独立执行层测试Redis未运行')
    prefix = 'execution-liveness-test:' + uuid.uuid4().hex
    queue = RedisTaskQueue(SimpleNamespace(redis=client, prefix=prefix))
    yield queue
    keys = list(client.scan_iter(match=prefix + ':*'))
    if keys: client.delete(*keys)


def _old_pending(queue, owner):
    message_id = queue.enqueue('cpu.chunk', {'job_id': uuid.uuid4().hex})
    queue.consume('cpu.chunk', owner, block_ms=1)
    queue.redis.xclaim(queue._stream('cpu.chunk'), queue.consumer_group, owner, 0, [message_id], idle=700000)
    return message_id


def test_real_redis_does_not_reclaim_long_task_from_live_worker(redis_queue):
    queue = redis_queue
    message_id = _old_pending(queue, 'alive')
    queue.redis.set(queue.store.prefix + ':workers:alive', '{}', ex=30)
    assert queue.reclaim('cpu.chunk', 'reaper', min_idle_seconds=600) == []
    pending = queue.redis.xpending_range(queue._stream('cpu.chunk'), queue.consumer_group, message_id, message_id, 1)
    assert pending[0]['consumer'] == 'alive'


def test_real_redis_reclaims_dead_worker_and_paginates_past_live_worker(redis_queue):
    queue = redis_queue
    _old_pending(queue, 'alive')
    dead = _old_pending(queue, 'dead')
    queue.redis.set(queue.store.prefix + ':workers:alive', '{}', ex=30)
    assert queue.reclaim('cpu.chunk', 'reaper', min_idle_seconds=600, count=1) == []
    recovered = queue.reclaim('cpu.chunk', 'reaper', min_idle_seconds=600, count=1)
    assert [item[0] for item in recovered] == [dead]
    queue.ack('cpu.chunk', dead)
    assert not queue.redis.xpending_range(queue._stream('cpu.chunk'), queue.consumer_group, dead, dead, 1)


def test_real_redis_long_reservation_renews_and_releases(redis_queue):
    scheduler = ResourceScheduler(redis_queue.store)
    scheduler.lease_ttl_seconds = 1
    device = GpuSnapshot('0', None, 4096, 0, 0, 'test')
    lease = dict(lease_id=uuid.uuid4().hex, physical_index='0', worker_id='live')
    assert scheduler._reserve(device, lease['lease_id'], 32, worker_id='live')
    for _ in range(3):
        time.sleep(.4)
        assert scheduler.renew(lease)
    assert scheduler.release(lease)
    assert not scheduler.renew(lease)
    expired = {**lease, 'lease_id': uuid.uuid4().hex}
    assert scheduler._reserve(device, expired['lease_id'], 32, worker_id='live')
    time.sleep(1.1)
    assert not scheduler.renew(expired)


def test_real_worker_heartbeat_survives_blocking_task_and_stops_on_exit(redis_queue, monkeypatch):
    queue = redis_queue
    message_id = _old_pending(queue, 'alive@current')
    worker = WorkerRuntime.__new__(WorkerRuntime)
    worker.worker_id, worker.gpu_device = 'alive', None
    worker.consumer_id, worker.queue = 'alive@current', queue
    worker.resource_classes, worker._last_heartbeat = ('cpu.chunk',), 0
    worker.manager = SimpleNamespace(scheduler=ResourceScheduler(queue.store))
    monkeypatch.setitem(EXECUTION_LIVENESS, 'heartbeat_seconds', .1)
    monkeypatch.setitem(EXECUTION_LIVENESS, 'heartbeat_ttl_seconds', 1)
    def blocking_task():
        time.sleep(1.3)  # Initial heartbeat would expire without the background loop.
        assert queue.redis.exists(queue.store.prefix + ':workers:alive')
        assert queue.reclaim('cpu.chunk', 'reaper', min_idle_seconds=600) == []
    monkeypatch.setattr(worker, '_run_messages', blocking_task)
    worker.run()
    time.sleep(.06)
    assert not queue.redis.exists(queue.store.prefix + ':workers:alive')
    assert [item[0] for item in queue.reclaim('cpu.chunk', 'reaper', min_idle_seconds=600)] == [message_id]


def test_real_redis_restart_with_same_worker_name_recovers_old_instances(redis_queue):
    queue = redis_queue
    old = _old_pending(queue, 'stable@old')
    legacy = _old_pending(queue, 'stable')
    current = _old_pending(queue, 'stable@new')
    scheduler = ResourceScheduler(queue.store)
    scheduler.register_worker(worker_id='stable', resource_classes=['cpu.chunk'], gpu_devices=[],
                              queue_consumer_id='stable@new')
    queue.heartbeat_consumer('stable@new')
    assert {entry[0] for entry in queue.reclaim('cpu.chunk', 'reaper', min_idle_seconds=600)} == {old, legacy}
    scheduler.unregister_worker('stable', queue_consumer_id='stable@old')
    assert queue.redis.exists(queue.store.prefix + ':workers:stable')
    pending = queue.redis.xpending_range(queue._stream('cpu.chunk'), queue.consumer_group, current, current, 1)
    assert pending[0]['consumer'] == 'stable@new'


@pytest.mark.parametrize('status', ['succeeded', 'failed', 'cancelled', 'running'])
def test_real_redis_directed_recovery_acks_correct_stream_and_does_not_replay_terminal(redis_queue, status):
    queue = redis_queue
    envelope = {'job_id': 'known', 'attempt': 1, 'reserved_allocation': {'gpu_allocations': []}}
    message_id = queue.enqueue('cpu.chunk', envelope, target_worker='stable')
    queue.consume('cpu.chunk', 'stable@dead', block_ms=1, target_worker='stable')
    stream = queue._stream('cpu.chunk', 'stable')
    queue.redis.xclaim(stream, queue.consumer_group, 'stable@dead', 0, [message_id], idle=700000)
    released, updates = [], []
    worker = WorkerRuntime.__new__(WorkerRuntime)
    worker.worker_id, worker.consumer_id, worker.queue = 'stable', 'stable@new', queue
    worker._reaper_min_idle_seconds = 600
    worker.manager = SimpleNamespace(
        store=SimpleNamespace(get_job=lambda _: {'status': status}, delete_auth_context=lambda _: None,
                              update_job=lambda *a, **kw: updates.append(kw)),
        release_allocation=lambda item: released.append(item),
        hub=SimpleNamespace(emit=lambda *a, **kw: None))
    worker._reap('cpu.chunk')
    assert not queue.redis.xpending_range(stream, queue.consumer_group, message_id, message_id, 1)
    assert released == [envelope['reserved_allocation']]
    new_messages = queue.redis.xrange(queue._stream('cpu.chunk'))
    assert len(new_messages) == (1 if status == 'running' else 0)
    assert bool(updates) == (status == 'running')
