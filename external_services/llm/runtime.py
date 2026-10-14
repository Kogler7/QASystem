"""Process-local quota pools and request deadlines shared by LLM callers.

Pools are keyed by caller-controlled quota groups. They do not coordinate
across processes or require a Backend/Redis service.
"""
from __future__ import annotations
from collections import deque
from contextlib import contextmanager
import threading
import time
from types import SimpleNamespace
from typing import Any

from algorithm.token_budget import check_input_budget

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
        # SDK-internal HTTP retries would bypass per-attempt QPS/QPM counting.
        # Registered callers retry through the algorithm, acquiring each time.
        self.client = client.with_options(max_retries=0) if config.get('service_snapshot') and hasattr(client, 'with_options') else client
        self.service_snapshot = config.get('service_snapshot')
        connection = (self.service_snapshot or {}).get('connection') or {}
        verify = connection.get('ca_bundle') or connection.get('verify_ssl', True)
        if verify is not True and hasattr(self.client, 'with_options'):
            import httpx
            self.client = self.client.with_options(http_client=httpx.Client(verify=verify, trust_env=False))
        if config.get('service_snapshot'):
            from .distributed import service_pool
            self.pool = service_pool(config['service_snapshot'])
        else:
            self.pool = get_pool(config["quota_group"], config)
        self.timeout = min(float(config["request_timeout"]), float(connection.get('timeout_seconds', config['request_timeout'])))
        self.thinking_parameter = config.get("thinking_parameter", "thinking")
        self.disable_thinking = config["disable_thinking"]
        self.response_format = config.get("response_format", "auto")
        self.max_input_tokens = int(config.get("max_input_tokens", 65536))
        budget = config.get("document_timeout")
        self.deadline = time.monotonic() + float(budget) if budget is not None else None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat))
        self.responses = SimpleNamespace(create=self._responses)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _call(self, method, **kwargs):
        from algorithm.extraction_trace import ExtractionTrace
        trace = self.__dict__.get("extraction_trace")
        started = time.monotonic()
        acquired = None
        error_type = None
        try:
            check_input_budget(kwargs, self.max_input_tokens)
            from .service import check_service_budget
            check_service_budget(kwargs, self.__dict__.get('service_snapshot'))
            call_deadline = started + self.timeout
            if self.deadline is not None:
                call_deadline = min(call_deadline, self.deadline)
            with self.pool.request_slot(call_deadline):
                acquired = time.monotonic()
                remaining = call_deadline - acquired
                if remaining <= 0:
                    raise TimeoutError("Extraction request budget exhausted")
                kwargs["timeout"] = remaining
                request_record = None
                if isinstance(trace, ExtractionTrace):
                    request_record = trace.event("http-request", {
                        "call_id": getattr(trace.local, "call_id", None),
                        "request": {key: value for key, value in kwargs.items() if key in {
                            "model", "messages", "input", "instructions", "response_format",
                            "text", "temperature", "max_tokens", "max_output_tokens", "extra_body"}},
                    })
                try:
                    response = method(**kwargs)
                except Exception as exc:
                    if request_record:
                        trace.write(request_record + "-response", {"status": "failed", "exception_type": type(exc).__name__})
                    raise
                if request_record:
                    try:
                        body = response if isinstance(response, dict) else response.model_dump(mode="json")
                        trace.write(request_record + "-response", {"status": "returned", "body": body})
                    except Exception:
                        # Trace serialization must never change the generation result.
                        trace.write(request_record + "-response", {"status": "unserializable"})
                if time.monotonic() >= call_deadline:
                    raise TimeoutError("Extraction response arrived after deadline")
                return response
        except Exception as exc:
            error_type = getattr(exc, "error_type", None) or type(exc).__name__
            raise
        finally:
            if isinstance(trace, ExtractionTrace):
                finished = time.monotonic()
                trace.event("http", {"call_id": getattr(trace.local, "call_id", None),
                            "error_type": error_type, "elapsed_seconds": finished - started,
                            "queue_seconds": (acquired - started) if acquired is not None else None,
                            "http_seconds": (finished - acquired) if acquired is not None else None})

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
