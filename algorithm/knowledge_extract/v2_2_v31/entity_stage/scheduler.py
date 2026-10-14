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
        self._semaphore = threading.BoundedSemaphore(limits.max_concurrency)
        self._condition = threading.Condition()
        self._second_window: deque[float] = deque()
        self._minute_window: deque[float] = deque()

    @staticmethod
    def _prune(window: deque[float], now: float, seconds: float) -> None:
        while window and now - window[0] >= seconds:
            window.popleft()

    def _wait_for_rate_limit(self, deadline: float | None = None) -> None:
        if self.limits.qps <= 0 and self.limits.qpm <= 0:
            return

        with self._condition:
            while True:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    raise TimeoutError('document deadline exhausted during rate limiting')
                self._prune(self._second_window, now, 1.0)
                self._prune(self._minute_window, now, 60.0)

                qps_full = self.limits.qps > 0 and len(self._second_window) >= self.limits.qps
                qpm_full = self.limits.qpm > 0 and len(self._minute_window) >= self.limits.qpm
                if not qps_full and not qpm_full:
                    if self.limits.qps > 0:
                        self._second_window.append(now)
                    if self.limits.qpm > 0:
                        self._minute_window.append(now)
                    return

                waits: list[float] = []
                if qps_full:
                    waits.append(max(0.001, 1.0 - (now - self._second_window[0])))
                if qpm_full:
                    waits.append(max(0.001, 60.0 - (now - self._minute_window[0])))
                if deadline is not None:
                    waits.append(max(0, deadline - now))
                self._condition.wait(timeout=min(waits))

    @contextmanager
    def request_slot(self, *, deadline: float | None = None) -> Iterator[None]:
        if deadline is None:
            acquired = self._semaphore.acquire()
        else:
            remaining = deadline - time.monotonic()
            acquired = remaining > 0 and self._semaphore.acquire(timeout=remaining)
        if not acquired:
            raise TimeoutError('document deadline exhausted during scheduling')
        try:
            self._wait_for_rate_limit(deadline)
            yield
        finally:
            self._semaphore.release()
            with self._condition:
                self._condition.notify_all()


_scheduler_lock = threading.Lock()
_scheduler: LLMRequestScheduler | None = None


def get_llm_scheduler() -> LLMRequestScheduler:
    global _scheduler
    limits = LLMRateLimits.from_config()
    with _scheduler_lock:
        if _scheduler is None or _scheduler.limits != limits:
            _scheduler = LLMRequestScheduler(limits)
        return _scheduler
