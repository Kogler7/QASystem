from __future__ import annotations

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
    # get_events 按 Redis Stream 同一契约返回 (event_id, event) 列表
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
