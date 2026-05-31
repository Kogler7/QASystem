"""StreamBroker — 线程安全的消息桥，连接 worker 线程与 SSE 异步端点。

Worker 线程（同步）调用 publish() 推送事件。
SSE 端点（async）调用 subscribe() 接收事件。

单例模式：同一进程内 api.py 和 worker.py 通过 get_broker() 共享同一实例。
多进程模式下每个 uvicorn worker 有自己独立的 broker 实例。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import AsyncIterator

logger = logging.getLogger("agentcode.stream_broker")


class StreamBroker:
    """线程安全的事件发布/订阅桥。

    publish() 由 worker 线程调用，subscribe() 由 SSE async generator 调用。
    """

    def __init__(self) -> None:
        self._queues: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()

    def publish(self, task_id: str, event: dict) -> None:
        """Worker 线程调用：推送一个事件到指定任务的订阅者。"""
        with self._lock:
            q = self._queues.get(task_id)
        if q is not None:
            q.put(event)

    async def subscribe(self, task_id: str) -> AsyncIterator[dict]:
        """SSE async generator 调用：订阅指定任务的事件流。

        使用 run_in_executor 阻塞在 queue.Queue.get() 上，
        而非轮询，保证事件到达后立即唤醒。
        """
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._queues[task_id] = q

        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    event = await loop.run_in_executor(
                        None, lambda: q.get(timeout=30)
                    )
                    event_type = event.get("type", "")
                    yield event
                    if event_type in ("done", "error"):
                        break
                except queue.Empty:
                    # 30s 无消息 → 发心跳保活 SSE 连接
                    yield {"type": "heartbeat"}
        finally:
            with self._lock:
                self._queues.pop(task_id, None)

    def close_task(self, task_id: str) -> None:
        """主动关闭指定任务的订阅。"""
        with self._lock:
            self._queues.pop(task_id, None)


# ═══════════════════════════════════════════════════════
# 模块级单例
# ═══════════════════════════════════════════════════════

_broker: StreamBroker | None = None


def get_broker() -> StreamBroker:
    """获取进程内共享的 StreamBroker 单例。"""
    global _broker
    if _broker is None:
        _broker = StreamBroker()
    return _broker
