from __future__ import annotations

import json
import os
import queue as std_queue
import threading
import time
import uuid
from typing import Any

from .store import RedisExecutionStore


class MemoryTaskQueue:
    _queues: dict[str, std_queue.Queue[dict[str, Any]]] = {}
    _lock = threading.Lock()

    def enqueue(self, resource_class: str, envelope: dict[str, Any], target_worker: str | None = None) -> str:
        queue_name = f"{resource_class}:worker:{target_worker}" if target_worker else resource_class
        with self._lock:
            target = self._queues.setdefault(queue_name, std_queue.Queue())
        message_id = uuid.uuid4().hex
        target.put({"message_id": message_id, **envelope})
        return message_id

    def consume(self, resource_class: str, timeout: float = 1.0, target_worker: str | None = None) -> dict[str, Any] | None:
        queue_name = f"{resource_class}:worker:{target_worker}" if target_worker else resource_class
        with self._lock:
            target = self._queues.setdefault(queue_name, std_queue.Queue())
        try:
            return target.get(timeout=timeout)
        except std_queue.Empty:
            return None

    def ack(self, _resource_class: str, _message: dict[str, Any], target_worker: str | None = None) -> None:
        return None


class RedisTaskQueue:
    def __init__(self, store: RedisExecutionStore):
        self.store = store
        self.redis = store.redis
        self.prefix = f"{store.prefix}:queue"
        self.consumer_group = os.getenv("EXECUTION_CONSUMER_GROUP", "workers")

    def _stream(self, resource_class: str, target_worker: str | None = None) -> str:
        suffix = f":worker:{target_worker}" if target_worker else ""
        return f"{self.prefix}:{resource_class}{suffix}"

    def enqueue(self, resource_class: str, envelope: dict[str, Any], target_worker: str | None = None) -> str:
        return self.redis.xadd(
            self._stream(resource_class, target_worker),
            {"payload": json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))},
            maxlen=int(os.getenv("EXECUTION_QUEUE_MAXLEN", "100000")),
            approximate=True,
        )

    def ensure_group(self, resource_class: str, target_worker: str | None = None) -> None:
        stream = self._stream(resource_class, target_worker)
        try:
            self.redis.xgroup_create(stream, self.consumer_group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def consume(
        self,
        resource_class: str,
        consumer: str,
        block_ms: int = 1000,
        target_worker: str | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        self.ensure_group(resource_class, target_worker)
        entries = self.redis.xreadgroup(
            self.consumer_group,
            consumer,
            {self._stream(resource_class, target_worker): ">"},
            count=1,
            block=block_ms,
        )
        if not entries:
            return None
        _stream, messages = entries[0]
        message_id, values = messages[0]
        return message_id, json.loads(values["payload"])

    def ack(self, resource_class: str, message_id: str, target_worker: str | None = None) -> None:
        self.redis.xack(self._stream(resource_class, target_worker), self.consumer_group, message_id)


_MEMORY_QUEUE = MemoryTaskQueue()


def get_task_queue(store: Any):
    if isinstance(store, RedisExecutionStore):
        return RedisTaskQueue(store)
    return _MEMORY_QUEUE
