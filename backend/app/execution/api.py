from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from starlette.concurrency import run_in_threadpool

from .manager import ExecutionManager, ExecutionUnavailable, get_execution_manager
from .operations import run_operation


@dataclass(slots=True)
class ManagedResult:
    data: Any
    execution: dict[str, Any]
    status_code: int
    accepted: bool = False


class ManagedExecutionError(RuntimeError):
    def __init__(self, cause: Exception, execution: dict[str, Any]):
        super().__init__(str(cause))
        self.cause = cause
        self.execution = execution


def prefer_wait_seconds(value: str | None) -> float | None:
    """Return the explicit wait budget from Prefer.

    No preference means the existing synchronous HTTP contract: submit to the
    shared queue and wait for the business result. ``respond-async`` opts into
    an immediate task handle; ``wait=N`` permits a bounded wait before that
    handle is returned.
    """
    if not value:
        return None
    respond_async = False
    for token in value.split(","):
        name, _, raw = token.strip().partition("=")
        if name.lower() == "respond-async":
            respond_async = True
            continue
        if name.lower() != "wait":
            continue
        try:
            return max(0.0, min(float(raw.strip().strip('"')), 60.0))
        except ValueError:
            return None
    return 0.0 if respond_async else None


async def dispatch_operation(
    *,
    operation: str,
    resource_class: str,
    payload: dict[str, Any],
    tenant_id: str = "default",
    priority: int = 50,
    total_units: int = 1,
    wait_seconds: float | None = None,
    manager: ExecutionManager | None = None,
) -> ManagedResult:
    manager = manager or get_execution_manager()
    submission = await run_in_threadpool(
        manager.submit,
        operation=operation,
        resource_class=resource_class,
        payload=payload,
        tenant_id=tenant_id,
        priority=priority,
        total_units=total_units,
    )
    if not manager.is_async:
        try:
            data = await run_in_threadpool(
                manager.run_inline,
                submission.job_id,
                operation,
                resource_class,
                lambda value: run_operation(operation, value),
            )
        except Exception as exc:
            raise ManagedExecutionError(exc, manager.execution_view(submission.job_id)) from exc
        return ManagedResult(data, manager.execution_view(submission.job_id), 200)

    if wait_seconds is None or wait_seconds > 0:
        execution = await asyncio.to_thread(manager.wait_blocking, submission.job_id, wait_seconds)
        if execution["status"] == "succeeded":
            return ManagedResult(manager.result(submission.job_id), execution, 200)
        if execution["status"] in {"failed", "cancelled"}:
            job = manager.store.get_job(submission.job_id) or {}
            error = job.get("error") or {}
            cause = RuntimeError(error.get("message") or f"execution {execution['status']}")
            raise ManagedExecutionError(cause, execution)
    execution = manager.execution_view(submission.job_id)
    return ManagedResult({"job_id": submission.job_id, "status": execution["status"]}, execution, 202, accepted=True)


__all__ = ["ManagedExecutionError", "ManagedResult", "ExecutionUnavailable", "dispatch_operation", "prefer_wait_seconds"]
