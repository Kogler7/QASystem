"""任务存储 — Redis 队列 + 状态持久化，Redis 不可用时自动降级为内存队列。

提供统一的 enqueue / dequeue / update / get 接口，调用方无需关心底层是 Redis 还是内存。

环境变量:
    TASK_STORE_REDIS_URL     Redis 连接 URL，默认 redis://localhost:6379/0
    TASK_STORE_KEY_PREFIX    Redis key 前缀，默认 agentcode:task
"""

from __future__ import annotations
from docker.configuration import getenv as config_getenv

import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("agentcode.task_store")

_REDIS_URL = config_getenv("TASK_STORE_REDIS_URL", "redis://localhost:6379/0")
_KEY_PREFIX = config_getenv("TASK_STORE_KEY_PREFIX", "agentcode:task")
_TASK_TTL = int(config_getenv("TASK_STORE_TASK_TTL", 86400))
_MAX_QUEUE_SIZE = int(config_getenv("TASK_STORE_MAX_QUEUE", 50000))
_VISIBILITY_TIMEOUT = int(config_getenv("TASK_STORE_VISIBILITY_TIMEOUT", 1500))
_EVENT_MAXLEN = int(config_getenv("TASK_STORE_EVENT_MAXLEN", 10000))

_QUEUED = "queued"
_RUNNING = "running"
_COMPLETED = "completed"
_FAILED = "failed"
_CANCELLED = "cancelled"
_TERMINAL = {_COMPLETED, _FAILED, _CANCELLED}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _try_connect_redis() -> Any:
    try:
        import redis
        client = redis.Redis.from_url(_REDIS_URL, decode_responses=True)
        client.ping()
        logger.info("task_store: 已连接 Redis (%s)", _REDIS_URL)
        return client
    except Exception as e:
        logger.warning("task_store: Redis 不可用 (%s)，降级为内存队列", e)
        return None


class _MemoryQueue:
    """单进程内存队列，Redis 不可用时的降级方案。

    限制:
      - 不跨进程，仅限单 worker 进程内
      - 进程重启任务丢失
      - maxsize 默认 10000，防止 OOM
    """

    def __init__(self, maxsize: int = 10000):
        self._tasks: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._queue: list[str] = []
        self._auth: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._maxsize = maxsize
        self._not_empty = threading.Condition(self._lock)

    def enqueue(self, task_id: str, data: dict[str, Any], owner: dict[str, str] | None = None) -> int:
        with self._lock:
            if len(self._tasks) >= self._maxsize:
                raise RuntimeError("task_store: 内存队列已满")
            now = _now_iso()
            self._tasks[task_id] = {
                "status": _QUEUED,
                "created_at": now,
                "updated_at": now,
                "request": data,
                "owner": dict(owner or {}),
                "result": None,
                "cancel_requested": False,
                "claimed_at": None,
                "events": [],
            }
            self._queue.append(task_id)
            self._not_empty.notify()
            return len(self._queue)

    def dequeue(self, timeout: float = 5.0) -> tuple[str, dict[str, Any]] | None:
        deadline = time.time() + timeout
        with self._lock:
            while True:
                now_epoch = time.time()
                for stale_id, stale_task in self._tasks.items():
                    claimed_at = stale_task.get("claimed_at")
                    if (
                        stale_task.get("status") == _RUNNING
                        and claimed_at
                        and now_epoch - claimed_at > _VISIBILITY_TIMEOUT
                    ):
                        stale_task["status"] = _QUEUED
                        stale_task["claimed_at"] = None
                        if stale_id not in self._queue:
                            self._queue.append(stale_id)
                while self._queue:
                    task_id = self._queue.pop(0)
                    task = self._tasks[task_id]
                    if task.get("cancel_requested"):
                        task["status"] = _CANCELLED
                        continue
                    task["status"] = _RUNNING
                    task["claimed_at"] = time.time()
                    task["updated_at"] = _now_iso()
                    return task_id, task["request"]
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._not_empty.wait(timeout=min(remaining, 1.0))

    def update(self, task_id: str, status: str, result: Any = None, error: str | None = None) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task["status"] = status
            task["updated_at"] = _now_iso()
            if result is not None:
                task["result"] = result
            if error is not None:
                task["error"] = error
            if status in _TERMINAL:
                task["claimed_at"] = None

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return {
                "task_id": task_id,
                "status": task["status"],
                "created_at": task["created_at"],
                "updated_at": task["updated_at"],
                "result": task.get("result"),
                "error": task.get("error"),
                "queue_position": self._queue.index(task_id) + 1 if task_id in self._queue else 0,
                "cancel_requested": bool(task.get("cancel_requested")),
            }

    def get_owner(self, task_id: str) -> dict[str, str]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return {}
            return dict(task.get("owner") or {})

    def queue_length(self) -> int:
        with self._lock:
            return len(self._queue)

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task["status"] in _TERMINAL:
                return self._snapshot(task_id, task)
            task["cancel_requested"] = True
            if task_id in self._queue:
                self._queue.remove(task_id)
                task["status"] = _CANCELLED
            else:
                task["status"] = "cancelling"
            task["updated_at"] = _now_iso()
            return self._snapshot(task_id, task)

    def _snapshot(self, task_id: str, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "status": task["status"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "result": task.get("result"),
            "error": task.get("error"),
            "queue_position": self._queue.index(task_id) + 1 if task_id in self._queue else 0,
            "cancel_requested": bool(task.get("cancel_requested")),
        }

    def is_cancel_requested(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            return bool(task and task.get("cancel_requested"))

    def touch(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task and task.get("status") in {_RUNNING, "cancelling"}:
                task["claimed_at"] = time.time()
                task["updated_at"] = _now_iso()

    def publish_event(self, task_id: str, event: dict[str, Any]) -> str:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return "0"
            events = task["events"]
            seq = (events[-1][0] + 1) if events else 1
            events.append((seq, event))
            if len(events) > _EVENT_MAXLEN:
                del events[: len(events) - _EVENT_MAXLEN]
            return str(seq)

    def get_events(self, task_id: str, cursor: str = "0") -> tuple[list[tuple[str, dict[str, Any]]], str]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return [], cursor
            after = _memory_cursor(cursor)
            selected = [(seq, event) for seq, event in task["events"] if seq > after]
            if not selected:
                return [], cursor
            return [(str(seq), event) for seq, event in selected], str(selected[-1][0])

    def save_auth_context(self, task_id: str, headers: dict[str, str]) -> str:
        auth_id = uuid.uuid4().hex
        with self._lock:
            self._auth[auth_id] = {"task_id": task_id, "headers": dict(headers)}
        return auth_id

    def load_auth_context(self, auth_context_id: str | None, task_id: str) -> dict[str, str]:
        if not auth_context_id:
            return {}
        with self._lock:
            context = self._auth.get(auth_context_id) or {}
            if context.get("task_id") != task_id:
                return {}
            return dict(context.get("headers") or {})

    def delete_auth_context(self, auth_context_id: str | None) -> None:
        if not auth_context_id:
            return
        with self._lock:
            self._auth.pop(auth_context_id, None)


class _RedisQueue:
    """Redis 队列实现，多进程共享。"""

    def __init__(self, client: Any):
        self._client = client
        self._list_key = f"{_KEY_PREFIX}:queue"
        self._processing_key = f"{_KEY_PREFIX}:processing"
        self._hash_prefix = f"{_KEY_PREFIX}:"

    def _hash_key(self, task_id: str) -> str:
        return f"{self._hash_prefix}{task_id}"

    def enqueue(self, task_id: str, data: dict[str, Any], owner: dict[str, str] | None = None) -> int:
        now = _now_iso()
        pipe = self._client.pipeline()
        pipe.hset(self._hash_key(task_id), mapping={
            "status": _QUEUED,
            "created_at": now,
            "updated_at": now,
            "request": json.dumps(data, ensure_ascii=False),
            "owner": json.dumps(owner or {}, ensure_ascii=False),
            "cancel_requested": "0",
            "queued_at": str(time.time()),
        })
        pipe.expire(self._hash_key(task_id), _TASK_TTL)
        # 在 lpush 前后各查一次 LLEN，用"入队前的长度"判断是否超限
        # 注意：pipeline 保证本连接的命令顺序执行，但多 API 实例并发
        # 时可能有 1~2 个任务的超限误差，对队列来说可以接受
        pipe.llen(self._list_key)
        pipe.lpush(self._list_key, task_id)
        pipe.llen(self._list_key)
        results = pipe.execute()
        before_length = results[2]
        after_length = results[-1]
        if before_length >= _MAX_QUEUE_SIZE:
            self._client.lrem(self._list_key, 0, task_id)
            self._client.delete(self._hash_key(task_id))
            raise RuntimeError(
                f"task_store: Redis 队列已满 (max={_MAX_QUEUE_SIZE})"
            )
        return after_length

    def dequeue(self, timeout: float = 5.0) -> tuple[str, dict[str, Any]] | None:
        self._requeue_stale()
        task_id = self._client.brpoplpush(
            self._list_key, self._processing_key, timeout=max(1, int(timeout))
        )
        if task_id is None:
            return None
        now = _now_iso()
        raw = self._client.hget(self._hash_key(task_id), "request")
        if self.is_cancel_requested(task_id):
            self.update(task_id, _CANCELLED)
            return self.dequeue(timeout)
        self._client.hset(self._hash_key(task_id), mapping={
            "status": _RUNNING,
            "updated_at": now,
            "claimed_at": str(time.time()),
        })
        request = json.loads(raw) if raw else {}
        return task_id, request

    def update(self, task_id: str, status: str, result: Any = None, error: str | None = None) -> None:
        mapping = {"status": status, "updated_at": _now_iso()}
        if result is not None:
            mapping["result"] = json.dumps(result, ensure_ascii=False)
        if error is not None:
            mapping["error"] = error
        self._client.hset(self._hash_key(task_id), mapping=mapping)
        self._client.expire(self._hash_key(task_id), _TASK_TTL)
        if status in _TERMINAL:
            self._client.lrem(self._processing_key, 0, task_id)
            self._client.lrem(self._list_key, 0, task_id)

    def get(self, task_id: str) -> dict[str, Any] | None:
        raw = self._client.hgetall(self._hash_key(task_id))
        if not raw:
            return None
        result_raw = raw.get("result")
        return {
            "task_id": task_id,
            "status": raw.get("status", ""),
            "created_at": raw.get("created_at", ""),
            "updated_at": raw.get("updated_at", ""),
            "result": json.loads(result_raw) if result_raw else None,
            "error": raw.get("error"),
            "queue_position": 0,
            "cancel_requested": raw.get("cancel_requested") == "1",
        }

    def get_owner(self, task_id: str) -> dict[str, str]:
        raw = self._client.hget(self._hash_key(task_id), "owner")
        if not raw:
            return {}
        try:
            owner = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return owner if isinstance(owner, dict) else {}

    def queue_length(self) -> int:
        return self._client.llen(self._list_key)

    def _requeue_stale(self) -> None:
        lock_key = f"{_KEY_PREFIX}:requeue-lock"
        if not self._client.set(lock_key, "1", nx=True, ex=10):
            return
        now = time.time()
        for task_id in self._client.lrange(self._processing_key, 0, -1):
            key = self._hash_key(task_id)
            status, claimed, cancel_requested, queued_at = self._client.hmget(
                key, "status", "claimed_at", "cancel_requested", "queued_at"
            )
            if status in _TERMINAL or not status:
                self._client.lrem(self._processing_key, 0, task_id)
                continue
            if cancel_requested == "1":
                self.update(task_id, _CANCELLED, error="Task cancelled")
                continue
            if status == _QUEUED and queued_at and now - float(queued_at) <= _VISIBILITY_TIMEOUT:
                continue
            if claimed and now - float(claimed) <= _VISIBILITY_TIMEOUT:
                continue
            pipe = self._client.pipeline()
            pipe.lrem(self._processing_key, 0, task_id)
            pipe.rpush(self._list_key, task_id)
            pipe.hset(key, mapping={
                "status": _QUEUED,
                "updated_at": _now_iso(),
                "claimed_at": "",
                "queued_at": str(now),
            })
            pipe.execute()

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        info = self.get(task_id)
        if info is None or info["status"] in _TERMINAL:
            return info
        queued = self._client.lrem(self._list_key, 0, task_id)
        status = _CANCELLED if queued else "cancelling"
        self._client.hset(self._hash_key(task_id), mapping={
            "cancel_requested": "1", "status": status, "updated_at": _now_iso(),
        })
        if status == _CANCELLED:
            self._client.lrem(self._processing_key, 0, task_id)
        return self.get(task_id)

    def is_cancel_requested(self, task_id: str) -> bool:
        return self._client.hget(self._hash_key(task_id), "cancel_requested") == "1"

    def touch(self, task_id: str) -> None:
        key = self._hash_key(task_id)
        status = self._client.hget(key, "status")
        if status in {_RUNNING, "cancelling"}:
            self._client.hset(key, mapping={
                "claimed_at": str(time.time()), "updated_at": _now_iso(),
            })
            self._client.expire(key, _TASK_TTL)

    def publish_event(self, task_id: str, event: dict[str, Any]) -> str:
        event_key = f"{self._hash_key(task_id)}:events"
        event_id = self._client.xadd(
            event_key,
            {"data": json.dumps(event, ensure_ascii=False)},
            maxlen=_EVENT_MAXLEN,
            approximate=True,
        )
        self._client.expire(event_key, _TASK_TTL)
        return event_id

    def get_events(self, task_id: str, cursor: str = "0-0") -> tuple[list[tuple[str, dict[str, Any]]], str]:
        event_key = f"{self._hash_key(task_id)}:events"
        rows = self._client.xread({event_key: cursor or "0-0"}, count=200)
        if not rows:
            return [], cursor
        events: list[tuple[str, dict[str, Any]]] = []
        next_cursor = cursor
        for _, entries in rows:
            for event_id, fields in entries:
                next_cursor = event_id
                events.append((event_id, json.loads(fields["data"])))
        return events, next_cursor

    def _auth_key(self, auth_id: str) -> str:
        return f"{_KEY_PREFIX}:auth:{auth_id}"

    def save_auth_context(self, task_id: str, headers: dict[str, str]) -> str:
        auth_id = uuid.uuid4().hex
        self._client.set(
            self._auth_key(auth_id),
            json.dumps({"task_id": task_id, "headers": headers}, ensure_ascii=False),
            ex=_TASK_TTL,
        )
        return auth_id

    def load_auth_context(self, auth_context_id: str | None, task_id: str) -> dict[str, str]:
        if not auth_context_id:
            return {}
        raw = self._client.get(self._auth_key(auth_context_id))
        if not raw:
            return {}
        context = json.loads(raw)
        if context.get("task_id") != task_id:
            return {}
        return dict(context.get("headers") or {})

    def delete_auth_context(self, auth_context_id: str | None) -> None:
        if auth_context_id:
            self._client.delete(self._auth_key(auth_context_id))


_QUEUE: _MemoryQueue | _RedisQueue | None = None
_INIT_LOCK = threading.Lock()


def _ensure_queue() -> _MemoryQueue | _RedisQueue:
    """延迟初始化队列，Redis 可用用 Redis，否则用内存。"""
    global _QUEUE
    if _QUEUE is not None:
        return _QUEUE
    with _INIT_LOCK:
        if _QUEUE is not None:
            return _QUEUE
        redis_client = _try_connect_redis()
        if redis_client is not None:
            _QUEUE = _RedisQueue(redis_client)
        else:
            _QUEUE = _MemoryQueue()
    return _QUEUE


def enqueue(task_id: str, data: dict[str, Any], owner: dict[str, str] | None = None) -> int:
    """入队一个任务，返回当前队列长度。"""
    return _ensure_queue().enqueue(task_id, data, owner=owner)


def dequeue(timeout: float = 5.0) -> tuple[str, dict[str, Any]] | None:
    """从队列取一个任务，超时返回 None。"""
    return _ensure_queue().dequeue(timeout)


def update(task_id: str, status: str, result: Any = None, error: str | None = None) -> None:
    """更新任务状态和结果。"""
    _ensure_queue().update(task_id, status, result=result, error=error)


def get(task_id: str) -> dict[str, Any] | None:
    """查询任务详情。"""
    return _ensure_queue().get(task_id)


def get_owner(task_id: str) -> dict[str, str]:
    return _ensure_queue().get_owner(task_id)


def queue_length() -> int:
    """获取当前队列长度。"""
    return _ensure_queue().queue_length()


def is_distributed() -> bool:
    return isinstance(_ensure_queue(), _RedisQueue)


def cancel(task_id: str) -> dict[str, Any] | None:
    return _ensure_queue().cancel(task_id)


def is_cancel_requested(task_id: str) -> bool:
    return _ensure_queue().is_cancel_requested(task_id)


def touch(task_id: str) -> None:
    _ensure_queue().touch(task_id)


def publish_event(task_id: str, event: dict[str, Any]) -> str:
    return _ensure_queue().publish_event(task_id, event)


def get_events(task_id: str, cursor: str = "0") -> tuple[list[tuple[str, dict[str, Any]]], str]:
    return _ensure_queue().get_events(task_id, cursor)


def save_auth_context(task_id: str, headers: dict[str, str]) -> str:
    return _ensure_queue().save_auth_context(task_id, headers)


def load_auth_context(auth_context_id: str | None, task_id: str) -> dict[str, str]:
    return _ensure_queue().load_auth_context(auth_context_id, task_id)


def delete_auth_context(auth_context_id: str | None) -> None:
    _ensure_queue().delete_auth_context(auth_context_id)


def _memory_cursor(cursor: str) -> int:
    if not cursor or cursor in {"0", "0-0"}:
        return 0
    try:
        return int(cursor)
    except ValueError:
        return 0
