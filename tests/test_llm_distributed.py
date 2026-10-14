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


def test_account_token_budget_settles_usage_without_a_second_queue(real_redis):
    import json
    account = uuid.uuid4().hex
    first, second = record(account=account, concurrency=16), record(account=account, concurrency=16)
    for service in (first, second):
        service.update(revision=1, account_limits={'max_concurrency':16,'qps':0,'qpm':0,'tpm':100})
    pool1, pool2 = (RedisAdmission(service, redis_client=real_redis) for service in (first, second))
    with pool1.request_slot(time.monotonic()+2, input_tokens=20, max_output_tokens=50) as reservation:
        with pytest.raises(TimeoutError):
            with pool2.request_slot(time.monotonic()+.1,input_tokens=10,max_output_tokens=40):
                pytest.fail('shared token budget exceeded')
        reservation.settle({'usage':{'prompt_tokens':5,'completion_tokens':0}})
        with pool2.request_slot(time.monotonic()+2,input_tokens=10,max_output_tokens=40):
            state = json.loads(real_redis.get(pool2.state_keys[1]))
            assert sum(item['total_tokens'] for item in state['reservations']) == 55
            assert state['active'] == 2


def test_new_revision_relaxes_caps_but_preserves_live_reservation(real_redis):
    import json
    original = {**record(), 'revision':1}
    first = RedisAdmission(original,redis_client=real_redis)
    relaxed = {**original,'revision':2,'limits':{'max_concurrency':2,'qps':0,'qpm':0}}
    second = RedisAdmission(relaxed,redis_client=real_redis)
    with first.request_slot(time.monotonic()+2,input_tokens=40,max_output_tokens=10) as live:
        with second.request_slot(time.monotonic()+2,input_tokens=10):
            state=json.loads(real_redis.get(first.state_keys[0]))
            assert state['active'] == 2 and state['policy_revision'] == 2
            assert next(item for item in state['reservations'] if item['id']==live.id)['total_tokens'] == 50
        # The older snapshot cannot overwrite newer shared scope policy.
        with first.request_slot(time.monotonic()+2,input_tokens=10):
            assert json.loads(real_redis.get(first.state_keys[0]))['active'] == 2
