from __future__ import annotations

from agentcode.identity import caller_identity, identity_matches
from agentcode.task_store import _MemoryQueue


def test_memory_queue_cancel_queued_task() -> None:
    store = _MemoryQueue()
    store.enqueue("t1", {"user_message": "x"})
    info = store.cancel("t1")
    assert info is not None
    assert info["status"] == "cancelled"
    assert store.dequeue(timeout=0.01) is None


def test_memory_queue_events_are_cursor_based() -> None:
    store = _MemoryQueue()
    store.enqueue("t1", {"user_message": "x"})
    store.publish_event("t1", {"type": "token", "text": "a"})
    first, cursor = store.get_events("t1")
    assert first == [("1", {"type": "token", "text": "a"})]
    assert store.get_events("t1", cursor)[0] == []
    store.publish_event("t1", {"type": "done"})
    assert store.get_events("t1", cursor)[0] == [("2", {"type": "done"})]


def test_memory_queue_completes_claimed_task() -> None:
    store = _MemoryQueue()
    store.enqueue("t1", {"user_message": "x"})
    assert store.dequeue(timeout=0.01) is not None
    store.update("t1", "completed", result={"payload": "ok"})
    info = store.get("t1")
    assert info is not None
    assert info["status"] == "completed"
    assert info["result"] == {"payload": "ok"}


def test_memory_auth_context_is_isolated_from_task_snapshot() -> None:
    store = _MemoryQueue()
    store.enqueue("t1", {"user_message": "x"})
    auth_id = store.save_auth_context("t1", {"authorization": "Bearer secret"})
    snapshot = store.get("t1")
    assert snapshot is not None
    assert "secret" not in str(snapshot)
    assert store.load_auth_context(auth_id, "t1") == {"authorization": "Bearer secret"}
    assert store.load_auth_context(auth_id, "other") == {}
    store.delete_auth_context(auth_id)
    assert store.load_auth_context(auth_id, "t1") == {}


def test_memory_owner_is_not_exposed_in_snapshot() -> None:
    store = _MemoryQueue()
    store.enqueue("t1", {"user_message": "x"}, owner={"tenant_id": "a", "auth_fp": "abc"})
    snapshot = store.get("t1")
    assert snapshot is not None
    assert "auth_fp" not in str(snapshot)
    assert store.get_owner("t1") == {"tenant_id": "a", "auth_fp": "abc"}


def test_identity_matches_requires_same_tenant_and_token() -> None:
    owner = caller_identity({
        "authorization": "Bearer secret",
        "x-tenant-id": "tenant-a",
    })
    assert identity_matches(owner, caller_identity({
        "authorization": "Bearer secret",
        "x-tenant-id": "tenant-a",
    }))
    assert not identity_matches(owner, caller_identity({
        "authorization": "Bearer secret",
        "x-tenant-id": "tenant-b",
    }))
    assert not identity_matches(owner, caller_identity({
        "authorization": "Bearer other",
        "x-tenant-id": "tenant-a",
    }))
    assert identity_matches({}, caller_identity({}))
