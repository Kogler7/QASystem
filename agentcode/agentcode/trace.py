from __future__ import annotations

import json
import os
import time
from typing import Any, Protocol


class TraceSink(Protocol):
    def write(self, event: dict[str, Any]) -> str:
        """Write one trace event and return trace reference."""


class NoopTraceSink:
    def write(self, event: dict[str, Any]) -> str:
        return ""


class JsonlTraceSink:
    def __init__(self, path: str, redact_keys: set[str] | None = None) -> None:
        self.path = path
        self.redact_keys = redact_keys or {"authorization", "api_key", "token", "password"}
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, nested in value.items():
                if key.lower() in self.redact_keys:
                    out[key] = "***REDACTED***"
                else:
                    out[key] = self._redact(nested)
            return out
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def write(self, event: dict[str, Any]) -> str:
        payload = {"ts": time.time(), **self._redact(event)}
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return self.path


class TaskEventTraceSink:
    """Persist turn-level traces alongside task events.

    Redis-backed deployments keep these records across API processes and SSE
    reconnects; the memory store provides the same contract for development.
    """

    def __init__(self, task_id: str, redact_keys: set[str] | None = None) -> None:
        self.task_id = task_id
        self.redact_keys = redact_keys or {"authorization", "api_key", "token", "password"}

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "***REDACTED***" if key.lower() in self.redact_keys else self._redact(nested)
                for key, nested in value.items()
            }
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def write(self, event: dict[str, Any]) -> str:
        from . import task_store

        task_store.publish_event(self.task_id, {"type": "trace", **self._redact(event)})
        return f"task://{self.task_id}"
