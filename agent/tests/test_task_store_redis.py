from __future__ import annotations

import os

import pytest

redis = pytest.importorskip("redis")

from agentcode.task_store import _RedisQueue


@pytest.fixture()
def store():
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL is not configured")
    client = redis.Redis.from_url(url, decode_responses=True)
    client.flushdb()
    yield _RedisQueue(client)
    client.flushdb()


def test_redis_claim_complete_and_event_replay(store) -> None:
    store.enqueue("t1", {"user_message": "x"})
    claimed = store.dequeue(timeout=1)
    assert claimed == ("t1", {"user_message": "x"})
    assert store.get("t1")["status"] == "running"

    event_id = store.publish_event("t1", {"type": "token", "text": "a"})
    events, cursor = store.get_events("t1")
    assert events == [(event_id, {"type": "token", "text": "a"})]
    assert cursor == event_id
    assert store.get_events("t1", cursor)[0] == []

    store.update("t1", "completed", result={"payload": "ok"})
    assert store.get("t1")["status"] == "completed"
    assert store._client.llen(store._processing_key) == 0


def test_redis_cancel_queued_task(store) -> None:
    store.enqueue("t1", {"user_message": "x"})
    info = store.cancel("t1")
    assert info["status"] == "cancelled"
    assert store.dequeue(timeout=1) is None


def test_redis_events_are_visible_across_store_instances(store) -> None:
    other = _RedisQueue(store._client)
    store.enqueue("t1", {"user_message": "x"})
    store.publish_event("t1", {"type": "status", "status": "running"})
    events, _ = other.get_events("t1")
    assert events == [(events[0][0], {"type": "status", "status": "running"})]


def test_redis_stale_claim_is_requeued(store) -> None:
    store.enqueue("t1", {"user_message": "x"})
    assert store.dequeue(timeout=1) is not None
    key = store._hash_key("t1")
    store._client.hset(key, "claimed_at", "1")
    store._client.delete(f"{store._hash_prefix.rstrip(':')}:requeue-lock")
    store._requeue_stale()
    assert store._client.lrem(store._processing_key, 0, "missing") == 0
    assert store._client.lrange(store._list_key, 0, -1) == ["t1"]
