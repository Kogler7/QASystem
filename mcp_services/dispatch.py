from __future__ import annotations

import os
from typing import Any

from .result import ToolCode, tool_error, tool_success


def _execution_mode() -> str:
    value = os.getenv("MCP_EXECUTION_MODE", "inline").strip().lower()
    if value in {"async", "queued", "durable"}:
        return "async"
    if value in {"sync", "wait", "blocking"}:
        return "sync"
    return "inline"


def maybe_submit(
    operation: str,
    payload: dict[str, Any],
    *,
    resource_class: str,
    tenant_id: str = "default",
    priority: int = 50,
) -> dict[str, Any] | None:
    """Dispatch heavy MCP work without changing its default result contract.

    Deployment uses ``sync`` so work runs in the shared Worker layer while the
    caller receives the original tool result. ``async`` is an explicit opt-in
    for a durable task handle. ``inline`` is reserved for tests and debugging.
    """
    mode = _execution_mode()
    if mode == "inline":
        return None
    try:
        from backend.app.execution.manager import ExecutionUnavailable, get_execution_manager

        manager = get_execution_manager()
        if not manager.is_async:
            return None
        submission = manager.submit(
            operation=operation,
            resource_class=resource_class,
            payload=payload,
            tenant_id=tenant_id,
            priority=priority,
        )
        if mode == "sync":
            execution = manager.wait_blocking(submission.job_id, None)
            if execution["status"] == "succeeded":
                return manager.result(submission.job_id)
            job = manager.store.get_job(submission.job_id) or {}
            error = job.get("error") or {}
            return tool_error(
                ToolCode.INTERNAL_ERROR,
                "任务执行失败",
                {
                    "type": error.get("type", "ExecutionError"),
                    "message": error.get("message", f"execution {execution['status']}"),
                    "execution": execution,
                },
            )

        execution = manager.execution_view(submission.job_id)
        return tool_success(
            {
                "accepted": True,
                "job_id": submission.job_id,
                "status": execution["status"],
                "status_url": f"/api/v1/jobs/{submission.job_id}",
                "result_url": f"/api/v1/jobs/{submission.job_id}/result",
                "events_url": execution["events_url"],
                "executions_url": execution["executions_url"],
                "execution": execution,
            },
            "任务已提交",
        )
    except ExecutionUnavailable as exc:
        return tool_error(ToolCode.EXECUTION_UNAVAILABLE, "执行队列不可用", str(exc))
    except Exception as exc:
        return tool_error(
            ToolCode.INTERNAL_ERROR,
            "任务提交失败",
            {"type": type(exc).__name__, "message": str(exc)},
        )


__all__ = ["maybe_submit"]
