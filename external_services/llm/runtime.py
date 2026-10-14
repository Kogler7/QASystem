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
from typing import Any, Iterable

from algorithm.token_budget import check_input_budget

# DeepSeek-style account tiers: (max input tokens, RPM, TPM for input+output).
# Applied only when the matching configured QPM/TPM limit is enabled (>0).
DEFAULT_RATE_TIERS: tuple[tuple[int, int, int], ...] = (
    (8192, 56, 460_000),
    (16_384, 26, 420_000),
    (32_768, 11, 350_000),
    (65_536, 4, 280_000),
)


def select_rate_tier(
    input_tokens: int,
    tiers: Iterable[tuple[int, int, int]] = DEFAULT_RATE_TIERS,
) -> tuple[int, int]:
    """Return (rpm, tpm) for the smallest tier that covers ``input_tokens``."""
    ordered = tuple(tiers) or DEFAULT_RATE_TIERS
    for limit, rpm, tpm in ordered:
        if input_tokens <= limit:
            return rpm, tpm
    return ordered[-1][1], ordered[-1][2]


def effective_rate_limits(
    *,
    qpm: int,
    tpm: int,
    input_tokens: int,
    tiers: Iterable[tuple[int, int, int]] = DEFAULT_RATE_TIERS,
) -> tuple[int, int]:
    """Tighten configured QPM/TPM by the input-size tier when that limit is on."""
    tier_rpm, tier_tpm = select_rate_tier(max(0, int(input_tokens)), tiers)
    effective_qpm = min(qpm, tier_rpm) if qpm > 0 else 0
    effective_tpm = min(tpm, tier_tpm) if tpm > 0 else 0
    return effective_qpm, effective_tpm


class QuotaPool:
    def __init__(self, limits: dict[str, Any]):
        self.condition = threading.Condition()
        self.active = 0
        self.starts: deque[float] = deque()
        self.token_starts: deque[tuple[float, int]] = deque()
        self.limits = {
            "max_concurrency": int(limits["max_concurrency"]),
            "qps": int(limits["qps"]),
            "qpm": int(limits["qpm"]),
            "tpm": max(0, int(limits.get("tpm", 0))),
        }
        tiers = limits.get("rate_tiers")
        self.rate_tiers = tuple(tiers) if tiers else DEFAULT_RATE_TIERS

    def tighten(self, limits: dict[str, Any]) -> None:
        # Aliases sharing a quota group cannot reset counters or loosen its cap.
        with self.condition:
            for key in ("max_concurrency", "qps", "qpm", "tpm"):
                if key not in limits and key == "tpm":
                    continue
                old = self.limits[key]
                new = int(limits[key]) if key in limits else old
                self.limits[key] = min(old, new) if old and new else old or new
            tiers = limits.get("rate_tiers")
            if tiers:
                self.rate_tiers = tuple(tiers)
            self.condition.notify_all()

    def _prune(self, now: float) -> None:
        while self.starts and now - self.starts[0] >= 60:
            self.starts.popleft()
        while self.token_starts and now - self.token_starts[0][0] >= 60:
            self.token_starts.popleft()

    @contextmanager
    def request_slot(
        self,
        deadline: float | None = None,
        *,
        tokens: int = 0,
        input_tokens: int | None = None,
    ):
        reserved = max(0, int(tokens))
        sized = max(0, int(input_tokens if input_tokens is not None else reserved))
        with self.condition:
            while True:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    raise TimeoutError("Extraction queue wait budget exhausted")
                self._prune(now)
                qpm_cap, tpm_cap = effective_rate_limits(
                    qpm=self.limits["qpm"],
                    tpm=self.limits["tpm"],
                    input_tokens=sized,
                    tiers=self.rate_tiers,
                )
                waits = []
                for key, period, cap in (
                    ("qps", 1, self.limits["qps"]),
                    ("qpm", 60, qpm_cap),
                ):
                    recent = [t for t in self.starts if now - t < period]
                    if cap and len(recent) >= cap:
                        waits.append(recent[-cap] + period - now)
                if tpm_cap:
                    recent_tokens = sum(count for stamp, count in self.token_starts if now - stamp < 60)
                    if recent_tokens + reserved > tpm_cap and self.token_starts:
                        waits.append(self.token_starts[0][0] + 60 - now)
                    elif recent_tokens + reserved > tpm_cap:
                        waits.append(0.05)
                if self.active < self.limits["max_concurrency"] and not waits:
                    self.active += 1
                    self.starts.append(now)
                    if reserved:
                        self.token_starts.append((now, reserved))
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
        if config.get('service_snapshot'):
            from .distributed import service_pool
            self.pool = service_pool(config['service_snapshot'])
        else:
            self.pool = get_pool(config["quota_group"], config)
        self.timeout = float(config["request_timeout"])
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
            estimate = check_input_budget(kwargs, self.max_input_tokens)
            output_reserve = int(kwargs.get("max_tokens") or kwargs.get("max_output_tokens") or 0)
            from .service import check_service_budget
            check_service_budget(kwargs, self.__dict__.get('service_snapshot'))
            call_deadline = started + self.timeout
            if self.deadline is not None:
                call_deadline = min(call_deadline, self.deadline)
            # Distributed service pools only accept deadline/cancelled; local QuotaPool also meters TPM.
            if self.__dict__.get("service_snapshot"):
                slot = self.pool.request_slot(call_deadline)
            else:
                slot = self.pool.request_slot(
                    call_deadline,
                    tokens=estimate + max(0, output_reserve),
                    input_tokens=estimate,
                )
            with slot:
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
