from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv
from qasystem_config.startup_contract import EXECUTION_LIVENESS

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

    def reclaim(
        self,
        _resource_class: str,
        _consumer: str,
        *,
        min_idle_seconds: float,
        target_worker: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        # 内存队列随进程消亡，不存在跨进程滞留消息。
        return []


class RedisTaskQueue:
    def __init__(self, store: RedisExecutionStore):
        self.store = store
        self.redis = store.redis
        self.prefix = f"{store.prefix}:queue"
        self.consumer_group = config_getenv("EXECUTION_CONSUMER_GROUP", "workers")

    def _stream(self, resource_class: str, target_worker: str | None = None) -> str:
        suffix = f":worker:{target_worker}" if target_worker else ""
        return f"{self.prefix}:{resource_class}{suffix}"

    def heartbeat_consumer(self, consumer: str) -> None:
        self.redis.set(f'{self.store.prefix}:queue-consumers:{consumer}', 'alive',
                       ex=EXECUTION_LIVENESS['heartbeat_ttl_seconds'])

    def remove_consumer_heartbeat(self, consumer: str) -> None:
        self.redis.delete(f'{self.store.prefix}:queue-consumers:{consumer}')

    def enqueue(self, resource_class: str, envelope: dict[str, Any], target_worker: str | None = None) -> str:
        return self.redis.xadd(
            self._stream(resource_class, target_worker),
            {"payload": json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))},
            maxlen=int(config_getenv("EXECUTION_QUEUE_MAXLEN", "100000")),
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

    def reclaim(
        self,
        resource_class: str,
        consumer: str,
        *,
        min_idle_seconds: float,
        count: int = 32,
        target_worker: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Reclaim only idle messages whose actual consumer has lost its heartbeat."""
        self.ensure_group(resource_class, target_worker)
        stream = self._stream(resource_class, target_worker)
        count = max(1, int(count))
        cursor = getattr(self, '_reap_cursors', {}).get(stream, '-')
        pending = self.redis.xpending_range(stream, self.consumer_group, cursor, '+', count,
                                           idle=max(0, int(min_idle_seconds * 1000)))
        if not hasattr(self, '_reap_cursors'):
            self._reap_cursors = {}
        self._reap_cursors[stream] = '(' + str(pending[-1]['message_id']) if len(pending) == count else '-'
        script = """
        if redis.call('exists', KEYS[2]) == 1 then return {} end
        local legacy = redis.call('get', KEYS[3])
        if legacy then
            local ok, record = pcall(cjson.decode, legacy)
            if not ok or not record.queue_consumer_id then return {} end
        end
        local current = redis.call('xpending', KEYS[1], ARGV[1], ARGV[3], ARGV[3], 1)
        if #current == 0 or current[1][2] ~= ARGV[4] then return {} end
        return redis.call('xclaim', KEYS[1], ARGV[1], ARGV[2], ARGV[5], ARGV[3])
        """
        reclaimed = []
        for entry in pending:
            owner, message_id = entry['consumer'], entry['message_id']
            entries = self.redis.eval(script, 3, stream,
                                      f'{self.store.prefix}:queue-consumers:{owner}',
                                      f'{self.store.prefix}:workers:{owner}',
                                      self.consumer_group, consumer, message_id, owner,
                                      max(0, int(min_idle_seconds * 1000)))
            for reclaimed_id, fields in entries:
                values = dict(zip(fields[::2], fields[1::2]))
                reclaimed.append((reclaimed_id, json.loads(values['payload'])))
        return reclaimed


_MEMORY_QUEUE = MemoryTaskQueue()


def get_task_queue(store: Any):
    if isinstance(store, RedisExecutionStore):
        return RedisTaskQueue(store)
    return _MEMORY_QUEUE
