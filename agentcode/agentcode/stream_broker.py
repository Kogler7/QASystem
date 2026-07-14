"""Task event broker backed by the configured task store.

Redis deployments use a per-task Stream, so publishers and SSE subscribers
may live in different processes and clients can reconnect without losing
events.  Development mode uses the memory task store with the same cursor
contract.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator

logger = logging.getLogger("agentcode.stream_broker")


class StreamBroker:
    """Sync publisher and async cursor-based subscriber facade."""

    def __init__(self) -> None:
        pass

    def create_task(self, task_id: str) -> None:
        """Compatibility no-op; task storage is created during enqueue."""
        # Event storage is created lazily by task_store.publish_event().
        return None

    def publish(self, task_id: str, event: dict) -> None:
        """Persist an event for current and future subscribers."""
        from . import task_store

        task_store.publish_event(task_id, event)

    async def subscribe(self, task_id: str) -> AsyncIterator[dict]:
        """Read persisted events in order and emit periodic heartbeats."""
        from . import task_store

        cursor = "0"
        last_heartbeat = time.monotonic()
        while True:
            events, cursor = await asyncio.to_thread(
                task_store.get_events, task_id, cursor
            )
            if events:
                for event in events:
                    event_type = event.get("type", "")
                    yield event
                    if event_type in ("done", "error"):
                        return
                last_heartbeat = time.monotonic()
                continue
            if time.monotonic() - last_heartbeat >= 15:
                yield {"type": "heartbeat"}
                last_heartbeat = time.monotonic()
            await asyncio.sleep(0.25)

    def close_task(self, task_id: str) -> None:
        """主动关闭指定任务的订阅。"""
        return None


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
