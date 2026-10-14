from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from .models import LEVELS, LEVEL_ORDER, iso_now, level_value
from .store import MemoryExecutionStore, RedisExecutionStore, get_execution_store


_LOGGER = logging.getLogger("qasystem.execution")
if not _LOGGER.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _LOGGER.addHandler(_handler)
    _LOGGER.propagate = False


class ExecutionEventHub:
    def __init__(self, store: MemoryExecutionStore | RedisExecutionStore):
        self.store = store
        self.default_level = config_getenv("EXECUTION_LOG_LEVEL", "INFO").upper()
        if self.default_level not in LEVEL_ORDER:
            self.default_level = "INFO"

    def _setting_key(self, scope: str, key: str) -> str:
        return f"level:{scope}:{key}"

    def _configured_level(self, *, job_id: str | None = None, scope: str = "global", key: str = "*") -> str:
        settings = self.store.get_settings()
        candidates = []
        if job_id:
            candidates.append(settings.get(self._setting_key("job", job_id)))
        candidates.append(settings.get(self._setting_key(scope, key)))
        candidates.append(settings.get(self._setting_key("global", "*")))
        for value in candidates:
            if not value:
                continue
            expires = value.get("expires_at")
            if expires and expires <= time.time():
                continue
            configured = str(value.get("level", "")).upper()
            if configured in LEVEL_ORDER:
                return configured
        return self.default_level

    def set_level(self, *, scope: str, key: str, level: str, ttl_seconds: int = 3600) -> dict[str, Any]:
        normalized = str(level).upper()
        if normalized not in LEVEL_ORDER:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        value = {
            "scope": scope,
            "key": key,
            "level": normalized,
            "ttl_seconds": ttl_seconds,
            "expires_at": time.time() + ttl_seconds,
        }
        self.store.set_setting(self._setting_key(scope, key), value)
        return value

    def levels(self) -> dict[str, Any]:
        return {"default": self.default_level, "levels": self.store.get_settings()}

    def emit(
        self,
        job_id: str,
        event: str,
        *,
        level: str = "INFO",
        message: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        workflow_id: str | None = None,
        source: dict[str, Any] | None = None,
        operation: str | None = None,
        stage: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = str(level).upper()
        if normalized not in LEVEL_ORDER:
            normalized = "INFO"
        event_data = {
            "event_id": str(uuid.uuid4()),
            "timestamp": iso_now(),
            "level": normalized,
            "event": event,
            "source": source or {},
            "job_id": job_id,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "operation": operation,
            "stage": stage,
            "message": message,
            "details": details or {},
        }
        persisted = self.store.append_event(job_id, event_data)
        configured = self._configured_level(job_id=job_id)
        if level_value(normalized) >= level_value(configured):
            _LOGGER.log(
                logging.ERROR if normalized == "ERROR" else logging.WARNING if normalized == "WARN" else logging.INFO,
                json.dumps(persisted, ensure_ascii=False, separators=(",", ":")),
            )
        return persisted

    def events(self, job_id: str, *, after_sequence: int = 0, limit: int = 100, min_level: str = "INFO") -> list[dict[str, Any]]:
        threshold = level_value(min_level)
        return [
            event
            for event in self.store.list_events(job_id, after_sequence=after_sequence, limit=limit)
            if level_value(str(event.get("level", "INFO"))) >= threshold
        ]

    async def subscribe(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        min_level: str = "INFO",
        poll_seconds: float = 0.5,
    ) -> AsyncIterator[dict[str, Any]]:
        cursor = after_sequence
        while True:
            events = self.events(job_id, after_sequence=cursor, min_level=min_level)
            for event in events:
                cursor = max(cursor, int(event.get("sequence", 0)))
                yield event
            job = self.store.get_job(job_id)
            if job is None:
                return
            if job.get("status") in {"succeeded", "failed", "cancelled"} and not events:
                return
            await asyncio.sleep(poll_seconds)


_HUBS: dict[int, ExecutionEventHub] = {}


def get_event_hub() -> ExecutionEventHub:
    store = get_execution_store()
    key = id(store)
    hub = _HUBS.get(key)
    if hub is None:
        hub = ExecutionEventHub(store)
        _HUBS[key] = hub
    return hub
