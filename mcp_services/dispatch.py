from __future__ import annotations

import os
from typing import Any

from .result import ToolCode, tool_error, tool_success


def _async_enabled() -> bool:
    value = os.getenv("MCP_EXECUTION_MODE", "inline").strip().lower()
    return value in {"async", "queued", "durable"}


def maybe_submit(
    operation: str,
    payload: dict[str, Any],
    *,
    resource_class: str,
    tenant_id: str = "default",
    priority: int = 50,
) -> dict[str, Any] | None:
    """Return a task handle when MCP durable dispatch is enabled.

    The default remains inline for direct library use and unit tests. Deployment
    Compose sets ``MCP_EXECUTION_MODE=async`` so heavy tools leave the MCP
    process immediately and are executed by the shared Worker layer.
    """
    if not _async_enabled():
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
