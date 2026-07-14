from __future__ import annotations

import threading
import time
from collections.abc import Callable


class CancellationToken:
    """Thread-safe cooperative cancellation token.

    A remote predicate can be supplied for task stores such as Redis.  The
    local event keeps cancellation cheap for in-process callers.
    """

    def __init__(
        self,
        remote_check: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> None:
        self._event = threading.Event()
        self._remote_check = remote_check
        self._deadline = deadline

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        if self.deadline_exceeded():
            return True
        if self._event.is_set():
            return True
        if self._remote_check is None:
            return False
        try:
            return bool(self._remote_check())
        except Exception:
            # A transient task-store failure must not cancel useful work.
            return False

    def set_deadline(self, deadline: float) -> None:
        self._deadline = min(self._deadline, deadline) if self._deadline else deadline

    def deadline_exceeded(self) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline

    def remaining_seconds(self) -> float | None:
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())


class AgentCancelled(RuntimeError):
    """Raised by blocking adapters when cooperative cancellation is observed."""
