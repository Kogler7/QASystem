"""Real Redis and spawned-process tests; never connect to deployment Redis."""
import multiprocessing
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from external_services.llm.shared_quota import RedisQuotaPool, SharedQuotaUnavailable


@pytest.fixture(scope='module')
def redis_url():
    redis = pytest.importorskip('redis')
    binary = shutil.which('redis-server')
    if binary is None:
        candidates = sorted((Path(__file__).resolve().parents[1] / 'tmp/redis-test').glob('redis-*/src/redis-server'))
        binary = str(candidates[-1]) if candidates else None
    if binary is None:
        pytest.skip('A local redis-server binary is required for process integration tests')
    with tempfile.TemporaryDirectory(prefix='qa-redis-', dir='/tmp') as directory:
        socket = str(Path(directory) / 'redis.sock')
        process = subprocess.Popen([binary, '--port', '0', '--unixsocket', socket,
                                    '--unixsocketperm', '700', '--save', '', '--appendonly', 'no'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = 'unix://' + socket + '?db=0'
        client = redis.Redis.from_url(url)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    client.ping()
                    break
                except redis.RedisError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        pytest.fail('Temporary Redis server did not start')
                    time.sleep(.01)
            yield url
        finally:
            client.close()
            process.terminate()
            process.wait(timeout=5)


def limits(redis_url, **overrides):
    return dict(max_concurrency=16, qps=0, qpm=0, quota_redis_url=redis_url,
                quota_tiers=[dict(max_input_tokens=100, rpm=1000000, tpm=100)], **overrides)


def _worker(url, group, max_concurrency, ready, gate, results):
    quota = RedisQuotaPool(group, dict(max_concurrency=max_concurrency, qps=0, qpm=0,
        quota_redis_url=url, quota_tiers=[dict(max_input_tokens=100, rpm=1000000, tpm=100)]))
    ready.put(True)
    gate.wait(timeout=5)
    try:
        with quota.request_slot(time.monotonic() + 5, input_tokens=10, max_output_tokens=40,
                                lease_seconds=6) as reservation:
            snapshot = quota.snapshot()
            results.put(('admitted', snapshot['active'], sum(r['total_tokens'] for r in snapshot['reservations'])))
            time.sleep(.15)
            reservation.settle({'usage': {'prompt_tokens': 1, 'completion_tokens': 0}})
        results.put(('finished',))
    except Exception as exc:
        results.put(('error', type(exc).__name__))


@pytest.mark.parametrize('concurrency,expected_peak', [(1, 1), (16, 2)])
def test_atomic_cross_process_concurrency_and_tpm(redis_url, concurrency, expected_peak):
    context = multiprocessing.get_context('spawn')
    ready, results, gate = context.Queue(), context.Queue(), context.Event()
    group = uuid4().hex
    workers = [context.Process(target=_worker, args=(redis_url, group, concurrency, ready, gate, results))
               for _ in range(4)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            ready.get(timeout=8)
        gate.set()
        observed = [results.get(timeout=8) for _ in range(8)]
        assert all(row[0] != 'error' for row in observed), observed
        admitted = [row for row in observed if row[0] == 'admitted']
        assert len(admitted) == 4
        assert max(row[1] for row in admitted) == expected_peak
        assert all(row[2] <= 100 for row in admitted)
        for worker in workers:
            worker.join(timeout=5)
            assert worker.exitcode == 0
        assert RedisQuotaPool(group, limits(redis_url)).snapshot()['active'] == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)


@pytest.mark.parametrize('usage', [None, {}, {'total_tokens': 0}, {'prompt_tokens': 0, 'completion_tokens': 0}])
def test_missing_usage_is_retained_across_independent_clients(redis_url, usage):
    group = uuid4().hex
    first = RedisQuotaPool(group, limits(redis_url))
    with first.request_slot(time.monotonic() + 1, input_tokens=10, max_output_tokens=90) as reservation:
        reservation.settle({'usage': usage})
    other = RedisQuotaPool(group, limits(redis_url))
    snapshot = other.snapshot()
    assert snapshot['active'] == 0
    assert snapshot['reservations'][0]['total_tokens'] == 100
    with pytest.raises(TimeoutError):
        with other.request_slot(time.monotonic() + .1, input_tokens=1):
            pytest.fail('Unreported tokens were refunded across processes')


def test_failed_request_is_retained_and_settlement_is_idempotent(redis_url):
    group = uuid4().hex
    first = RedisQuotaPool(group, limits(redis_url))
    with pytest.raises(RuntimeError):
        with first.request_slot(time.monotonic() + 1, input_tokens=10, max_output_tokens=90):
            raise RuntimeError('transport failed')
    other = RedisQuotaPool(group, limits(redis_url))
    assert other.snapshot()['reservations'][0]['total_tokens'] == 100
    with pytest.raises(TimeoutError):
        with other.request_slot(time.monotonic() + .1, input_tokens=1):
            pytest.fail('Failed request reservation was removed')
    group = uuid4().hex
    first = RedisQuotaPool(group, limits(redis_url))
    with first.request_slot(time.monotonic() + 1, input_tokens=10, max_output_tokens=90) as reservation:
        reservation.settle({'usage': {'prompt_tokens': 10, 'completion_tokens': 10}})
        reservation.settle({'usage': {'prompt_tokens': 1, 'completion_tokens': 0}})
    assert RedisQuotaPool(group, limits(redis_url)).snapshot()['reservations'][0]['total_tokens'] == 20


def test_new_worker_cannot_reset_rpm_or_loosen_configuration(redis_url):
    group = uuid4().hex
    config = limits(redis_url)
    config.update(qpm=1, max_concurrency=1)
    first = RedisQuotaPool(group, config)
    with first.request_slot(time.monotonic() + 1, input_tokens=1):
        pass
    other = RedisQuotaPool(group, limits(redis_url))
    assert other.snapshot()['limits'] == dict(max_concurrency=1, qps=0, qpm=1)
    with pytest.raises(TimeoutError):
        with other.request_slot(time.monotonic() + .1, input_tokens=1):
            pytest.fail('New worker reset the minute request counter')


def test_shared_429_cooldown_and_timeout_classification(redis_url):
    group = uuid4().hex
    first = RedisQuotaPool(group, limits(redis_url))
    assert first.report_error(TimeoutError('token rate exceeded')) is None
    error = RuntimeError('RPM limit')
    error.status_code = 429
    error.response = SimpleNamespace(headers={'retry-after': '5'})
    first.report_error(error)
    other = RedisQuotaPool(group, limits(redis_url))
    assert other.snapshot()['scales'] == dict(rpm=.5, tpm=1.0, concurrency=1.0)
    with pytest.raises(TimeoutError):
        with other.request_slot(time.monotonic() + .1, input_tokens=1):
            pytest.fail('429 cooldown did not cross process boundaries')


def test_orphan_lease_preserves_token_charge_after_releasing_concurrency(redis_url):
    group = uuid4().hex
    quota = RedisQuotaPool(group, limits(redis_url))
    # Simulate a worker dying after atomic admission, without running finally.
    data = quota._transaction(lambda state, now: state.attempt(now, 10, 90, lease_seconds=.05)[0].data())
    time.sleep(.08)
    snapshot = RedisQuotaPool(group, limits(redis_url)).snapshot()
    assert snapshot['active'] == 0
    assert snapshot['reservations'][0]['total_tokens'] == 100
    assert snapshot['reservations'][0]['finished'] == data['lease_until']
    with pytest.raises(TimeoutError):
        with quota.request_slot(time.monotonic() + .1, input_tokens=1):
            pytest.fail('Expired worker lease erased unknown token usage')


def test_live_stream_renews_lease_until_consumer_finishes(redis_url):
    group = uuid4().hex
    quota = RedisQuotaPool(group, limits(redis_url))
    with quota.request_slot(time.monotonic() + 1, input_tokens=10, max_output_tokens=90,
                            lease_seconds=.15):
        time.sleep(.35)
        snapshot = RedisQuotaPool(group, limits(redis_url)).snapshot()
        assert snapshot['active'] == 1
        assert snapshot['reservations'][0]['finished'] is None
        assert snapshot['reservations'][0]['total_tokens'] == 100
    assert quota.snapshot()['active'] == 0


def test_unavailable_redis_never_falls_back_to_process_local_quota():
    quota = RedisQuotaPool(uuid4().hex, limits('unix:///tmp/qasystem-missing-redis.sock'))
    with pytest.raises(SharedQuotaUnavailable, match='unavailable'):
        with quota.request_slot(time.monotonic() + 1, input_tokens=1):
            pytest.fail('Unavailable shared quota admitted a request')


@pytest.mark.parametrize('known', [True, False])
def test_long_response_uses_the_same_completed_token_window_across_clients(redis_url, known):
    group = uuid4().hex
    quota = RedisQuotaPool(group, limits(redis_url))
    with quota.request_slot(time.monotonic() + 2, input_tokens=10, max_output_tokens=90,
                            lease_seconds=10) as first:
        # Advance the logical age in Redis, without a minute-long integration
        # sleep. The live lease and actual completion still use Redis time.
        def age(state, now):
            next(record for record in state.reservations if record.id == first.id).started = now - 80
        quota._transaction(age)
        first.settle({'usage': {'prompt_tokens': 10, 'completion_tokens': 90}} if known else {})
        other = RedisQuotaPool(group, limits(redis_url))
        assert other.snapshot()['active'] == 1
        with pytest.raises(TimeoutError):
            with other.request_slot(time.monotonic() + .1, input_tokens=1):
                pytest.fail('A live long response released token reservation')
    other = RedisQuotaPool(group, limits(redis_url))
    if known:
        with other.request_slot(time.monotonic() + .5, input_tokens=1):
            assert all(record['id'] != first.id for record in other.snapshot()['reservations'])
    else:
        assert other.snapshot()['reservations'][0]['total_tokens'] == 100
        with pytest.raises(TimeoutError):
            with other.request_slot(time.monotonic() + .1, input_tokens=1):
                pytest.fail('An unknown long response escaped the completion-based retention')


def test_known_orphan_usage_releases_at_lease_expiry_without_another_minute(redis_url):
    group = uuid4().hex
    quota = RedisQuotaPool(group, limits(redis_url))
    def vanished_after_usage(state, now):
        record, _ = state.attempt(now - 80, 10, 90, lease_seconds=80.05)
        state.settle_state(record, {'usage': {'prompt_tokens': 10, 'completion_tokens': 90}}, now)
    quota._transaction(vanished_after_usage)
    assert quota.snapshot()['active'] == 1
    time.sleep(.08)
    other = RedisQuotaPool(group, limits(redis_url))
    with other.request_slot(time.monotonic() + .5, input_tokens=1):
        assert len(other.snapshot()['reservations']) == 1
