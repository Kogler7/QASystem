"""任务存储 — Redis 队列 + 状态持久化，Redis 不可用时自动降级为内存队列。

提供统一的 enqueue / dequeue / update / get 接口，调用方无需关心底层是 Redis 还是内存。

环境变量:
    TASK_STORE_REDIS_URL     Redis 连接 URL，默认 redis://localhost:6379/0
    TASK_STORE_KEY_PREFIX    Redis key 前缀，默认 agentcode:task
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("agentcode.task_store")

_REDIS_URL = os.getenv("TASK_STORE_REDIS_URL", "redis://localhost:6379/0")
_KEY_PREFIX = os.getenv("TASK_STORE_KEY_PREFIX", "agentcode:task")
_TASK_TTL = int(os.getenv("TASK_STORE_TASK_TTL", 86400))
_MAX_QUEUE_SIZE = int(os.getenv("TASK_STORE_MAX_QUEUE", 50000))

_QUEUED = "queued"
_RUNNING = "running"
_COMPLETED = "completed"
_FAILED = "failed"


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
        self._lock = threading.Lock()
        self._maxsize = maxsize
        self._not_empty = threading.Condition(self._lock)

    def enqueue(self, task_id: str, data: dict[str, Any]) -> int:
        with self._lock:
            if len(self._tasks) >= self._maxsize:
                raise RuntimeError("task_store: 内存队列已满")
            now = _now_iso()
            self._tasks[task_id] = {
                "status": _QUEUED,
                "created_at": now,
                "updated_at": now,
                "request": data,
                "result": None,
            }
            self._queue.append(task_id)
            self._not_empty.notify()
            return len(self._queue)

    def dequeue(self, timeout: float = 5.0) -> tuple[str, dict[str, Any]] | None:
        deadline = time.time() + timeout
        with self._lock:
            while not self._queue:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._not_empty.wait(timeout=min(remaining, 1.0))
            task_id = self._queue.pop(0)
            task = self._tasks[task_id]
            task["status"] = _RUNNING
            task["updated_at"] = _now_iso()
            return task_id, task["request"]

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
            }

    def queue_length(self) -> int:
        with self._lock:
            return len(self._queue)


class _RedisQueue:
    """Redis 队列实现，多进程共享。"""

    def __init__(self, client: Any):
        self._client = client
        self._list_key = f"{_KEY_PREFIX}:queue"
        self._hash_prefix = f"{_KEY_PREFIX}:"

    def _hash_key(self, task_id: str) -> str:
        return f"{self._hash_prefix}{task_id}"

    def enqueue(self, task_id: str, data: dict[str, Any]) -> int:
        now = _now_iso()
        pipe = self._client.pipeline()
        pipe.hset(self._hash_key(task_id), mapping={
            "status": _QUEUED,
            "created_at": now,
            "updated_at": now,
            "request": json.dumps(data, ensure_ascii=False),
        })
        pipe.expire(self._hash_key(task_id), _TASK_TTL)
        # 在 rpush 前后各查一次 LLEN，用"入队前的长度"判断是否超限
        # 注意：pipeline 保证本连接的命令顺序执行，但多 API 实例并发
        # 时可能有 1~2 个任务的超限误差，对队列来说可以接受
        pipe.llen(self._list_key)
        pipe.rpush(self._list_key, task_id)
        pipe.llen(self._list_key)
        results = pipe.execute()
        before_length = results[2]
        after_length = results[-1]
        if before_length >= _MAX_QUEUE_SIZE:
            self._client.lpop(self._list_key)
            self._client.delete(self._hash_key(task_id))
            raise RuntimeError(
                f"task_store: Redis 队列已满 (max={_MAX_QUEUE_SIZE})"
            )
        return after_length

    def dequeue(self, timeout: float = 5.0) -> tuple[str, dict[str, Any]] | None:
        result = self._client.blpop(self._list_key, timeout=int(timeout))
        if result is None:
            return None
        _, task_id = result
        now = _now_iso()
        raw = self._client.hget(self._hash_key(task_id), "request")
        self._client.hset(self._hash_key(task_id), mapping={
            "status": _RUNNING,
            "updated_at": now,
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
        }

    def queue_length(self) -> int:
        return self._client.llen(self._list_key)


_QUEUE: _MemoryQueue | _RedisQueue | None = None


def _ensure_queue() -> _MemoryQueue | _RedisQueue:
    """延迟初始化队列，Redis 可用用 Redis，否则用内存。"""
    global _QUEUE
    if _QUEUE is not None:
        return _QUEUE
    redis_client = _try_connect_redis()
    if redis_client is not None:
        _QUEUE = _RedisQueue(redis_client)
    else:
        _QUEUE = _MemoryQueue()
    return _QUEUE


def enqueue(task_id: str, data: dict[str, Any]) -> int:
    """入队一个任务，返回当前队列长度。"""
    return _ensure_queue().enqueue(task_id, data)


def dequeue(timeout: float = 5.0) -> tuple[str, dict[str, Any]] | None:
    """从队列取一个任务，超时返回 None。"""
    return _ensure_queue().dequeue(timeout)


def update(task_id: str, status: str, result: Any = None, error: str | None = None) -> None:
    """更新任务状态和结果。"""
    _ensure_queue().update(task_id, status, result=result, error=error)


def get(task_id: str) -> dict[str, Any] | None:
    """查询任务详情。"""
    return _ensure_queue().get(task_id)


def queue_length() -> int:
    """获取当前队列长度。"""
    return _ensure_queue().queue_length()
