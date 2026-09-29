from __future__ import annotations

from collections.abc import Callable
from typing import Any


class ExecutionUnavailable(RuntimeError):
    """Raised when durable execution is required but cannot be initialized."""


_FACTORY: Callable[[], Any] | None = None


def register_execution_manager(factory: Callable[[], Any]) -> None:
    """Register the callable that returns the shared execution manager.

    This breaks the package-level import cycle between ``backend`` (which owns
    the concrete ``ExecutionManager``) and ``mcp_services`` (which needs to
    submit/query durable tasks).  The factory is stored as a callable so tests
    can monkeypatch ``backend.app.execution.manager.get_execution_manager`` and
    have the bridge pick up the new implementation automatically.
    """
    global _FACTORY
    _FACTORY = factory


def get_execution_manager() -> Any:
    """Return the registered execution manager, or raise if none is set."""
    if _FACTORY is None:
        raise ExecutionUnavailable("Execution manager has not been registered")
    return _FACTORY()


def execution_manager_registered() -> bool:
    return _FACTORY is not None
