"""Shared per-process quota pools and per-document HTTP budgets.

Pools are keyed by a server-controlled quota group, never by algorithm version.
They deliberately do not claim to coordinate quotas across worker processes.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import importlib
import os
import threading
import time
from types import SimpleNamespace
from typing import Any


class QuotaPool:
    def __init__(self, limits: dict[str, Any]):
        self.condition = threading.Condition()
        self.active = 0
        self.starts: deque[float] = deque()
        self.limits = {key: int(limits[key]) for key in ("max_concurrency", "qps", "qpm")}

    def tighten(self, limits: dict[str, Any]) -> None:
        # Aliases sharing a quota group cannot reset counters or loosen its cap.
        with self.condition:
            for key, old in self.limits.items():
                new = int(limits[key])
                self.limits[key] = min(old, new) if old and new else old or new
            self.condition.notify_all()

    @contextmanager
    def request_slot(self, deadline: float | None = None):
        with self.condition:
            while True:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    raise TimeoutError("Extraction queue wait budget exhausted")
                while self.starts and now - self.starts[0] >= 60:
                    self.starts.popleft()
                waits = []
                for key, period in (("qps", 1), ("qpm", 60)):
                    cap = self.limits[key]
                    recent = [t for t in self.starts if now - t < period]
                    if cap and len(recent) >= cap:
                        waits.append(recent[-cap] + period - now)
                if self.active < self.limits["max_concurrency"] and not waits:
                    self.active += 1
                    self.starts.append(now)
                    break
                wait = max(waits) if waits else None
                if deadline is not None:
                    wait = min(wait, deadline - now) if wait is not None else deadline - now
                self.condition.wait(timeout=wait)
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()


_lock = threading.Lock()
_pools: dict[str, QuotaPool] = {}


def get_pool(group: str, limits: dict[str, Any]) -> QuotaPool:
    with _lock:
        if group not in _pools:
            _pools[group] = QuotaPool(limits)
        else:
            _pools[group].tighten(limits)
        return _pools[group]


class ScheduledClient:
    """Wrap SDK endpoints, so fallback HTTP attempts also acquire quota."""
    def __init__(self, client: Any, config: dict[str, Any]):
        self.client = client
        self.pool = get_pool(config["quota_group"], config)
        self.timeout = float(config["request_timeout"])
        self.thinking_parameter = config.get("thinking_parameter", "thinking")
        self.disable_thinking = config["disable_thinking"]
        self.response_format = config.get("response_format", "auto")
        budget = config.get("document_timeout")
        self.deadline = time.monotonic() + float(budget) if budget is not None else None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat))
        self.responses = SimpleNamespace(create=self._responses)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _call(self, method, **kwargs):
        call_deadline = time.monotonic() + self.timeout
        if self.deadline is not None:
            call_deadline = min(call_deadline, self.deadline)
        with self.pool.request_slot(call_deadline):
            remaining = call_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Extraction request budget exhausted")
            kwargs["timeout"] = remaining
            response = method(**kwargs)
            if time.monotonic() >= call_deadline:
                raise TimeoutError("Extraction response arrived after deadline")
            return response

    def _chat(self, **kwargs):
        if self.response_format == "json_object" and (kwargs.get("response_format") or {}).get("type") == "json_schema":
            kwargs["response_format"] = {"type": "json_object"}
        if self.disable_thinking and self.thinking_parameter != "thinking":
            extra = dict(kwargs.get("extra_body") or {})
            extra.pop("thinking", None)
            if self.thinking_parameter == "enable_thinking":
                extra["enable_thinking"] = False
            kwargs["extra_body"] = extra
        return self._call(self.client.chat.completions.create, **kwargs)

    def _responses(self, **kwargs):
        return self._call(self.client.responses.create, **kwargs)


def create_request_llm(config: dict[str, Any]):
    version = config["version"]
    package = "v3_1" if version == "v3.1" else version
    module = importlib.import_module(f"algorithm.knowledge_extract.{package}.llm_client")
    factory = module.create_client if version == "v3.1" else module.create_llm_client
    credential = os.getenv(config["api_key_env"], "")
    if config["provider"] != "default" and not credential.strip():
        raise ValueError("Extraction provider credential is not configured")
    llm = factory(base_url=config["base_url"], api_key=credential,
                  model=config["model"], max_tokens=config["max_tokens"],
                  request_timeout=config["request_timeout"], disable_thinking=config["disable_thinking"])
    llm.client = ScheduledClient(llm.client, config)
    llm.api_mode = config.get("api_mode", "auto")
    return llm
