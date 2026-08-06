from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


LEVELS = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR")
LEVEL_ORDER = {level: index for index, level in enumerate(LEVELS)}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def level_value(level: str) -> int:
    normalized = str(level or "INFO").upper()
    if normalized not in LEVEL_ORDER:
        raise ValueError(f"unsupported execution log level: {level}")
    return LEVEL_ORDER[normalized]


def summarize_executions(job: dict[str, Any], executions: list[dict[str, Any]]) -> dict[str, Any]:
    completed = sum(item.get("status") == "succeeded" for item in executions)
    failed = sum(item.get("status") == "failed" for item in executions)
    retries = sum(max(int(item.get("attempt", 1)) - 1, 0) for item in executions)
    workers = sorted(
        {
            allocation.get("worker_id")
            for item in executions
            for allocation in [item.get("allocation") or {}]
            if allocation.get("worker_id")
        }
    )
    gpus = sorted(
        {
            gpu.get("device_uuid") or gpu.get("physical_index")
            for item in executions
            for allocation in [item.get("allocation") or {}]
            for gpu in allocation.get("gpu_allocations") or []
        },
        key=str,
    )
    queue_ms = sum(int((item.get("durations_ms") or {}).get("queue") or 0) for item in executions)
    execution_ms = sum(
        int((item.get("durations_ms") or {}).get("execution") or 0)
        for item in executions
    )
    return {
        "total_tasks": len(executions),
        "completed_tasks": completed,
        "failed_tasks": failed,
        "retry_count": retries,
        "queue_ms": queue_ms,
        "execution_ms": execution_ms or None,
        "workers": workers,
        "gpus": gpus,
        "status": job.get("status"),
    }
