"""Real Redis admission integration, including separate spawned processes."""
from __future__ import annotations
import multiprocessing
import threading
import time
import uuid

import pytest
import redis

from external_services.llm.distributed import RedisAdmission, ModelSchedulingError


@pytest.fixture
def real_redis():
    client = redis.Redis(host='127.0.0.1', port=16389, socket_connect_timeout=.2)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip('Real Redis integration requires localhost:16389')
    return client


def record(*, account=None, concurrency=1, qps=0):
    result = {'service_id': uuid.uuid4().hex, 'limits': {'max_concurrency': concurrency, 'qps': qps, 'qpm': 0}}
    if account:
        result.update(account_id=account, account_limits={'max_concurrency': 1, 'qps': 0, 'qpm': 0})
    return result


def child_call(service, output, duration=.12):
    client = redis.Redis(host='127.0.0.1', port=16389)
    with RedisAdmission(service, redis_client=client, lease_seconds=.3).request_slot(time.monotonic()+5):
        started = time.monotonic()
        time.sleep(duration)
        output.put((started, time.monotonic()))


def test_multiple_processes_share_account_and_service(real_redis):
    ctx = multiprocessing.get_context('spawn')
    output = ctx.Queue()
    account = uuid.uuid4().hex
    first, second = record(account=account), record(account=account)
    children = [ctx.Process(target=child_call, args=(service, output)) for service in (first, first, second)]
    for child in children:
        child.start()
    intervals = sorted(output.get(timeout=10) for _ in children)
    for child in children:
        child.join(5)
        assert child.exitcode == 0
    assert all(b[0] >= a[1] for a, b in zip(intervals, intervals[1:]))


def test_fifo_and_long_call_renews_lease(real_redis):
    pool = RedisAdmission(record(), redis_client=real_redis, lease_seconds=.2)
    order, threads = [], []
    with pool.request_slot(time.monotonic()+3):
        def wait(number):
            with pool.request_slot(time.monotonic()+3):
                order.append(number)
        for number in range(3):
            thread = threading.Thread(target=wait, args=(number,))
            thread.start()
            threads.append(thread)
            time.sleep(.04)
        time.sleep(.5)
        assert order == []
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert order == [0, 1, 2]


def test_wait_timeout_cancel_and_exception_release(real_redis):
    pool = RedisAdmission(record(), redis_client=real_redis)
    with pool.request_slot():
        with pytest.raises(TimeoutError):
            with pool.request_slot(time.monotonic()+.05):
                pytest.fail('must not acquire')
        with pytest.raises(ModelSchedulingError, match='cancelled'):
            with pool.request_slot(cancelled=lambda: True):
                pytest.fail('must not acquire')
    with pytest.raises(ValueError):
        with pool.request_slot():
            raise ValueError('downstream error')
    with pool.request_slot(time.monotonic()+.2):
        pass


def test_crashed_process_recovers_and_qps_shared(real_redis):
    ctx = multiprocessing.get_context('spawn')
    service = record(qps=1)
    output = ctx.Queue()
    child = ctx.Process(target=child_call, args=(service, output, 20))
    child.start()
    pool = RedisAdmission(service, redis_client=real_redis, lease_seconds=.3)
    limit = time.monotonic()+5
    while not real_redis.zcard(pool.keys[2]) and time.monotonic() < limit:
        time.sleep(.02)
    assert real_redis.zcard(pool.keys[2]) == 1
    child.terminate()
    child.join(3)
    with pool.request_slot(time.monotonic()+3):
        first = time.monotonic()
    with pool.request_slot(time.monotonic()+3):
        assert time.monotonic()-first >= .95


def test_redis_failure_does_not_run_unlimited():
    pool = RedisAdmission(record(), redis_client=redis.Redis(host='127.0.0.1', port=16388,
                          socket_connect_timeout=.1, retry_on_timeout=False))
    with pytest.raises(ModelSchedulingError, match='unavailable'):
        with pool.request_slot(time.monotonic()+.5):
            pytest.fail('must not run downstream')
