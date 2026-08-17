from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from .config import get_llm_config


@dataclass(frozen=True, slots=True)
class LLMRateLimits:
    max_concurrency: int = 4
    qps: int = 0
    qpm: int = 0

    @classmethod
    def from_config(cls) -> "LLMRateLimits":
        config = get_llm_config()
        return cls(
            max_concurrency=max(1, int(config.get("max_concurrency", 4))),
            qps=max(0, int(config.get("qps", 0))),
            qpm=max(0, int(config.get("qpm", 0))),
        )


class LLMRequestScheduler:
    """Coordinate in-flight requests and rolling QPS/QPM limits across threads."""

    def __init__(self, limits: LLMRateLimits):
        self.limits = limits
        self._condition = threading.Condition()
        self._active = 0
        self._second_window: deque[float] = deque()
        self._minute_window: deque[float] = deque()

    def update_limits(self, limits: LLMRateLimits) -> None:
        """Apply new limits without recreating this shared scheduler instance."""
        with self._condition:
            self.limits = limits
            self._condition.notify_all()

    @staticmethod
    def _prune(window: deque[float], now: float, seconds: float) -> None:
        while window and now - window[0] >= seconds:
            window.popleft()

    def _rate_limit_delay(self, now: float) -> float | None:
        """Return how long until a rate-limit slot is free, or ``None`` if free."""
        self._prune(self._second_window, now, 1.0)
        self._prune(self._minute_window, now, 60.0)
        qps_full = self.limits.qps > 0 and len(self._second_window) >= self.limits.qps
        qpm_full = self.limits.qpm > 0 and len(self._minute_window) >= self.limits.qpm
        if not qps_full and not qpm_full:
            return None
        waits: list[float] = []
        if qps_full:
            waits.append(max(0.001, 1.0 - (now - self._second_window[0])))
        if qpm_full:
            waits.append(max(0.001, 60.0 - (now - self._minute_window[0])))
        return min(waits) if waits else None

    def _reserve_rate_limit(self, now: float) -> None:
        if self.limits.qps > 0:
            self._second_window.append(now)
        if self.limits.qpm > 0:
            self._minute_window.append(now)

    @contextmanager
    def request_slot(self) -> Iterator[None]:
        with self._condition:
            while True:
                now = time.monotonic()
                rate_delay = self._rate_limit_delay(now)
                if self._active < self.limits.max_concurrency and rate_delay is None:
                    self._active += 1
                    self._reserve_rate_limit(now)
                    break

                waits: list[float] = []
                if rate_delay is not None:
                    waits.append(rate_delay)
                if self._active >= self.limits.max_concurrency and not waits:
                    self._condition.wait()
                else:
                    self._condition.wait(timeout=min(waits) if waits else None)
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()


_scheduler_lock = threading.Lock()
_scheduler: LLMRequestScheduler | None = None


def get_llm_scheduler() -> LLMRequestScheduler:
    global _scheduler
    limits = LLMRateLimits.from_config()
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = LLMRequestScheduler(limits)
        else:
            _scheduler.update_limits(limits)
        return _scheduler
