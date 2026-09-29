from __future__ import annotations

from typing import Any

from mcp_services import execution_bridge
from mcp_services.result import ToolCode, tool_error, tool_success
from mcp_services.runtime import mcp


def _manager():
    return execution_bridge.get_execution_manager()


@mcp.tool(name="get_job")
def get_job(job_id: str) -> dict[str, Any]:
    """Return durable execution state and the current execution summary."""
    try:
        manager = _manager()
        job = manager.store.get_job(job_id)
        if not job:
            return tool_error(ToolCode.INVALID_ARGUMENT, f"任务不存在: {job_id}")
        return tool_success(
            {"job": job, "execution": manager.execution_view(job_id)},
            "任务状态查询完成",
        )
    except Exception as exc:
        return tool_error(ToolCode.INTERNAL_ERROR, "任务状态查询失败", {"type": type(exc).__name__, "message": str(exc)})


@mcp.tool(name="get_job_result")
def get_job_result(job_id: str) -> dict[str, Any]:
    """Return the result of a succeeded durable execution."""
    try:
        manager = _manager()
        job = manager.store.get_job(job_id)
        if not job:
            return tool_error(ToolCode.INVALID_ARGUMENT, f"任务不存在: {job_id}")
        execution = manager.execution_view(job_id)
        if job.get("status") != "succeeded":
            return tool_error(
                ToolCode.INVALID_ARGUMENT,
                f"任务尚未成功完成: {job.get('status')}",
                {"execution": execution},
            )
        return tool_success(
            {"result": manager.result(job_id), "execution": execution},
            "任务结果查询完成",
        )
    except Exception as exc:
        return tool_error(ToolCode.INTERNAL_ERROR, "任务结果查询失败", {"type": type(exc).__name__, "message": str(exc)})


@mcp.tool(name="cancel_job")
def cancel_job(job_id: str) -> dict[str, Any]:
    """Request cancellation of a queued or running durable execution."""
    try:
        execution = _manager().cancel(job_id)
        return tool_success(execution, "任务取消请求已提交")
    except KeyError:
        return tool_error(ToolCode.INVALID_ARGUMENT, f"任务不存在: {job_id}")
    except Exception as exc:
        return tool_error(ToolCode.INTERNAL_ERROR, "任务取消失败", {"type": type(exc).__name__, "message": str(exc)})


__all__ = ["cancel_job", "get_job", "get_job_result"]
