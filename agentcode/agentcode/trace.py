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
        os.makedirs(os.path.dirname(path), exist_ok=True)

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
