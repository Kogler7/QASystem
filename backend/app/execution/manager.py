from __future__ import annotations

import base64
import copy
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .artifacts import ArtifactStore
from .hub import ExecutionEventHub, get_event_hub
from .models import TERMINAL_STATUSES, iso_now, summarize_executions
from .queue import get_task_queue
from .resources import ResourceLeaseUnavailable, ResourceScheduler
from .store import MemoryExecutionStore, RedisExecutionStore, get_execution_store


class ExecutionUnavailable(RuntimeError):
    pass


class ExecutionNotFound(KeyError):
    pass


@dataclass(slots=True)
class JobSubmission:
    job_id: str
    status_code: int
    execution: dict[str, Any]
    data: Any = None


def _resource_queue(resource_class: str) -> str:
    return resource_class.replace("/", ".").replace(" ", "_")


class ExecutionManager:
    def __init__(
        self,
        store: MemoryExecutionStore | RedisExecutionStore | None = None,
        artifacts: ArtifactStore | None = None,
        hub: ExecutionEventHub | None = None,
    ):
        self.store = store or get_execution_store()
        self.artifacts = artifacts or ArtifactStore()
        self.hub = hub or get_event_hub()
        self.queue = get_task_queue(self.store)
        self.scheduler = ResourceScheduler(self.store)
        self.mode = os.getenv("EXECUTION_MODE", "async").strip().lower()
        self.allow_memory_queue = os.getenv("EXECUTION_ALLOW_MEMORY_QUEUE", "false").lower() in {
            "1", "true", "yes", "on"
        }
        if self.mode not in {"async", "inline"}:
            raise ValueError("EXECUTION_MODE must be async or inline")

    @property
    def is_async(self) -> bool:
        return self.mode == "async"

    def _ensure_queue_available(self) -> None:
        if self.is_async and isinstance(self.store, MemoryExecutionStore) and not self.allow_memory_queue:
            raise ExecutionUnavailable(
                "执行队列未配置：生产/默认模式需要 EXECUTION_REDIS_URL 和可用 Redis"
            )

    def _write_payload(self, job_id: str, payload: Any) -> str:
        # Bytes are encoded here only as a compatibility bridge. Large uploads
        # should use the object-store adapter once it is deployed.
        def normalize(value: Any) -> Any:
            if isinstance(value, bytes):
                return {"__bytes_base64__": base64.b64encode(value).decode("ascii")}
            if isinstance(value, dict):
                return {str(key): normalize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [normalize(item) for item in value]
            return value

        return self.artifacts.write_payload(job_id, normalize(payload))

    def read_payload(self, reference: str | None) -> Any:
        def restore(value: Any) -> Any:
            if isinstance(value, dict) and set(value) == {"__bytes_base64__"}:
                return base64.b64decode(value["__bytes_base64__"])
            if isinstance(value, dict):
                return {key: restore(item) for key, item in value.items()}
            if isinstance(value, list):
                return [restore(item) for item in value]
            return value

        return restore(self.artifacts.read_payload(reference))

    def _allocation(self, resource_class: str, envelope: dict[str, Any] | None = None) -> dict[str, Any]:
        reserved = (envelope or {}).get("reserved_allocation")
        if isinstance(reserved, dict):
            allocation = copy.deepcopy(reserved)
            allocation.setdefault("worker_id", os.getenv("EXECUTION_WORKER_ID", "gateway-inline"))
            allocation.setdefault("worker_instance_id", os.getenv("EXECUTION_WORKER_INSTANCE_ID", f"pid-{os.getpid()}"))
            allocation.setdefault("process_id", os.getpid())
            allocation.setdefault("runtime", os.getenv("HOSTNAME", socket.gethostname()))
            allocation.setdefault("host_id", socket.gethostname())
            allocation.setdefault("resource_class", resource_class)
            allocation.setdefault("model", os.getenv("EXECUTION_MODEL"))
            allocation.setdefault("endpoint", os.getenv("MINERU_VLM_SERVER_URL") if resource_class == "gpu.vlm" else None)
            return allocation
        external_device = os.getenv("EXECUTION_GPU_DEVICE", "").strip()
        visible = external_device or os.getenv("CUDA_VISIBLE_DEVICES", "").strip()
        physical = [item.strip() for item in visible.split(",") if item.strip()]
        allocation: dict[str, Any] = {
            "worker_id": os.getenv("EXECUTION_WORKER_ID", "gateway-inline"),
            "worker_instance_id": os.getenv("EXECUTION_WORKER_INSTANCE_ID", f"pid-{os.getpid()}"),
            "process_id": os.getpid(),
            "runtime": os.getenv("HOSTNAME", socket.gethostname()),
            "host_id": socket.gethostname(),
            "model": os.getenv("EXECUTION_MODEL", None),
            "resource_class": resource_class,
            "gpu_allocations": [],
        }
        if resource_class.startswith("gpu"):
            lease_mode = os.getenv("EXECUTION_GPU_LEASE_MODE", "auto").strip().lower()
            if not external_device and lease_mode not in {"external", "disabled", "none"}:
                try:
                    leases = self.scheduler.acquire(resource_class)
                except ResourceLeaseUnavailable:
                    if os.getenv("EXECUTION_GPU_SCHEDULING_REQUIRED", "false").lower() in {"1", "true", "yes", "on"}:
                        raise
                    leases = []
                allocation["gpu_allocations"] = [lease.as_dict() for lease in leases]
            if not allocation["gpu_allocations"]:
                allocation["gpu_allocations"] = [
                    {
                        "physical_index": item,
                        "logical_index": index,
                        "device_uuid": None,
                        "lease_id": None,
                        "memory_budget_mb": None,
                    }
                    for index, item in enumerate(physical)
                ]
            allocation["model"] = os.getenv("EXECUTION_MODEL") or allocation["model"]
            allocation["model_version"] = os.getenv("EXECUTION_MODEL_VERSION")
            allocation["model_instance_id"] = os.getenv("EXECUTION_MODEL_INSTANCE_ID")
            allocation["endpoint"] = os.getenv("MINERU_VLM_SERVER_URL") if resource_class == "gpu.vlm" else None
        return allocation

    @staticmethod
    def _duration_ms(start: str | None, end: str | None) -> int:
        if not start or not end:
            return 0
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            return max(0, int((end_dt - start_dt).total_seconds() * 1000))
        except (TypeError, ValueError):
            return 0

    def _release_allocation(self, job_id: str, execution: dict[str, Any], allocation: dict[str, Any] | None) -> None:
        if not allocation:
            return
        released: list[str] = []
        for item in allocation.get("gpu_allocations", []):
            lease_id = item.get("lease_id")
            if not lease_id:
                continue
            if self.scheduler.release(item, worker_id=allocation.get("worker_id")):
                released.append(lease_id)
        if released:
            self.hub.emit(
                job_id,
                "gpu.lease_released",
                workflow_id=(self.store.get_job(job_id) or {}).get("workflow_id"),
                task_id=execution.get("task_id"),
                attempt_id=execution.get("attempt_id"),
                operation=execution.get("operation"),
                stage="release",
                source=allocation,
                details={"lease_ids": released},
            )

    def submit(
        self,
        *,
        operation: str,
        resource_class: str,
        payload: Any,
        tenant_id: str = "default",
        priority: int = 50,
        total_units: int = 1,
        workflow_id: str | None = None,
        parent_job_id: str | None = None,
        parent_task_id: str | None = None,
        shard_index: int | None = None,
        page_range: list[int] | None = None,
        target_worker: str | None = None,
        reserved_allocation: dict[str, Any] | None = None,
    ) -> JobSubmission:
        self._ensure_queue_available()
        job_id = uuid.uuid4().hex
        try:
            payload_ref = self._write_payload(job_id, payload)
        except Exception:
            self.release_allocation(reserved_allocation)
            raise
        job_fields: dict[str, Any] = {
            "tenant_id": tenant_id,
            "operation": operation,
            "resource_class": resource_class,
            "queue": _resource_queue(resource_class),
            "priority": priority,
            "total_units": total_units,
            "completed_units": 0,
            "payload_ref": payload_ref,
            "workflow_id": workflow_id or f"workflow-{job_id}",
            "mode": self.mode,
            "cancel_requested": False,
        }
        optional_fields = {
            "parent_job_id": parent_job_id,
            "parent_task_id": parent_task_id,
            "shard_index": shard_index,
            "page_range": page_range,
            "target_worker": target_worker,
            "reserved_allocation": reserved_allocation,
        }
        job_fields.update({key: value for key, value in optional_fields.items() if value is not None})
        try:
            job = self.store.create_job(
                job_id=job_id,
                **job_fields,
            )
        except Exception:
            self.release_allocation(reserved_allocation)
            raise
        self.hub.emit(
            job_id,
            "job.accepted",
            workflow_id=job["workflow_id"],
            operation=operation,
            stage="accepted",
            details={"resource_class": resource_class, "priority": priority},
        )
        self.store.update_job(job_id, status="queued", stage="queued")
        self.hub.emit(job_id, "task.queued", workflow_id=job["workflow_id"], operation=operation, stage="queued")
        if self.is_async:
            envelope = {
                "job_id": job_id,
                "task_id": f"task-{job_id}",
                "operation": operation,
                "resource_class": resource_class,
                "payload_ref": payload_ref,
                "workflow_id": job["workflow_id"],
                "attempt": 1,
            }
            for key in ("parent_job_id", "parent_task_id", "shard_index", "page_range", "target_worker", "reserved_allocation"):
                if key in job_fields:
                    envelope[key] = job_fields[key]
            try:
                self.queue.enqueue(
                    _resource_queue(resource_class),
                    envelope,
                    target_worker=target_worker,
                )
            except Exception as exc:
                self.release_allocation(reserved_allocation)
                self.store.update_job(job_id, status="failed", stage="queue", error={"type": type(exc).__name__, "message": str(exc)})
                self.hub.emit(job_id, "job.failed", level="ERROR", operation=operation, stage="queue", details={"type": type(exc).__name__, "message": str(exc)})
                raise ExecutionUnavailable(f"任务入队失败：{exc}") from exc
        return JobSubmission(job_id, 202 if self.is_async else 200, self.execution_view(job_id))

    def _start_attempt(self, job_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise ExecutionNotFound(job_id)
        attempt = int(envelope.get("attempt", 1))
        execution = self.store.create_execution(
            job_id,
            task_id=envelope.get("task_id") or f"task-{job_id}",
            parent_task_id=envelope.get("parent_task_id"),
            shard_index=envelope.get("shard_index"),
            page_range=envelope.get("page_range"),
            attempt_id=uuid.uuid4().hex,
            attempt=attempt,
            operation=job.get("operation"),
            resource_class=job.get("resource_class"),
            queue=job.get("queue"),
            status="queued",
            allocation=None,
            timestamps={"created_at": iso_now(), "queued_at": iso_now()},
            durations_ms={"queue": 0, "dispatch": 0, "execution": None, "persist": None, "total": None},
            error=None,
        )
        return execution

    def run_attempt(
        self,
        job_id: str,
        envelope: dict[str, Any],
        runner: Callable[[Any], Any],
        *,
        mark_job_failed: bool = True,
    ) -> Any:
        execution = self._start_attempt(job_id, envelope)
        execution_id = execution["execution_id"]
        job = self.store.get_job(job_id) or {}
        operation = str(job.get("operation", envelope.get("operation", "unknown")))
        resource_class = str(job.get("resource_class", envelope.get("resource_class", "cpu")))
        workflow_id = job.get("workflow_id")
        allocation: dict[str, Any] | None = None
        timestamps = execution["timestamps"].copy()
        started = time.monotonic()
        try:
            allocation = self._allocation(resource_class, envelope)
            scheduled_at = iso_now()
            timestamps.update({"scheduled_at": scheduled_at, "lease_acquired_at": scheduled_at})
            self.store.update_execution(job_id, execution_id, status="scheduled", allocation=allocation, timestamps=timestamps)
            self.store.update_job(job_id, status="running", stage=operation)
            self.hub.emit(job_id, "task.scheduled", workflow_id=workflow_id, task_id=execution["task_id"], attempt_id=execution["attempt_id"], operation=operation, stage=operation, source=allocation, details={"resource_class": resource_class})
            if allocation.get("gpu_allocations"):
                self.hub.emit(job_id, "gpu.lease_acquired", workflow_id=workflow_id, task_id=execution["task_id"], attempt_id=execution["attempt_id"], operation=operation, stage=operation, source=allocation, details={"lease_ids": [gpu.get("lease_id") for gpu in allocation.get("gpu_allocations", [])]})
            started_at = iso_now()
            timestamps["worker_started_at"] = started_at
            timestamps["execution_started_at"] = started_at
            self.store.update_execution(job_id, execution_id, status="running", timestamps=timestamps)
            self.hub.emit(job_id, "task.started", workflow_id=workflow_id, task_id=execution["task_id"], attempt_id=execution["attempt_id"], operation=operation, stage=operation, source=allocation)
            task_payload = self.read_payload(envelope.get("payload_ref") or job.get("payload_ref"))
            if isinstance(task_payload, dict):
                task_payload = {**task_payload, "__execution": {"allocation": allocation, "job_id": job_id, "task_id": execution["task_id"], "attempt_id": execution["attempt_id"]}}
            result = runner(task_payload)
            persist_started = time.monotonic()
            result_ref = self.artifacts.write_result(job_id, result)
            finished_at = iso_now()
            duration = int((time.monotonic() - started) * 1000)
            persist_ms = int((time.monotonic() - persist_started) * 1000)
            durations = {
                "queue": self._duration_ms(timestamps.get("queued_at"), timestamps.get("scheduled_at")),
                "dispatch": self._duration_ms(timestamps.get("scheduled_at"), timestamps.get("worker_started_at")),
                "execution": duration,
                "persist": persist_ms,
                "total": duration + persist_ms,
            }
            timestamps["execution_finished_at"] = finished_at
            timestamps["result_persisted_at"] = finished_at
            self.store.update_execution(job_id, execution_id, status="succeeded", timestamps=timestamps, durations_ms=durations, result_ref=result_ref)
            self.store.update_job(job_id, status="succeeded", stage="completed", result_ref=result_ref, completed_units=job.get("total_units", 1))
            self.hub.emit(job_id, "task.completed", workflow_id=workflow_id, task_id=execution["task_id"], attempt_id=execution["attempt_id"], operation=operation, stage=operation, source=allocation, details={"duration_ms": duration})
            self.hub.emit(job_id, "job.completed", workflow_id=workflow_id, operation=operation, stage="completed")
            return result
        except Exception as exc:
            finished_at = iso_now()
            duration = int((time.monotonic() - started) * 1000)
            error = {"type": type(exc).__name__, "message": str(exc)}
            timestamps["execution_finished_at"] = finished_at
            self.store.update_execution(
                job_id,
                execution_id,
                status="failed",
                allocation=allocation,
                timestamps=timestamps,
                durations_ms={
                    "queue": self._duration_ms(timestamps.get("queued_at"), timestamps.get("scheduled_at")),
                    "dispatch": self._duration_ms(timestamps.get("scheduled_at"), timestamps.get("worker_started_at")),
                    "execution": duration if timestamps.get("execution_started_at") else None,
                    "persist": None,
                    "total": duration,
                },
                error=error,
            )
            self.hub.emit(job_id, "task.failed", level="ERROR", workflow_id=workflow_id, task_id=execution["task_id"], attempt_id=execution["attempt_id"], operation=operation, stage=operation, source=allocation, details=error)
            if mark_job_failed:
                self.store.update_job(job_id, status="failed", stage=operation, error=error)
                self.hub.emit(job_id, "job.failed", level="ERROR", workflow_id=workflow_id, operation=operation, stage=operation, details=error)
            raise
        finally:
            self._release_allocation(job_id, execution, allocation)

    def run_inline(self, job_id: str, operation: str, resource_class: str, runner: Callable[[Any], Any]) -> Any:
        envelope = {"job_id": job_id, "task_id": f"task-{job_id}", "operation": operation, "resource_class": resource_class, "attempt": 1}
        return self.run_attempt(job_id, envelope, runner)

    def execution_view(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise ExecutionNotFound(job_id)
        executions = self.store.list_executions(job_id)
        view = {
            "mode": job.get("mode", self.mode),
            "job_id": job_id,
            "status": job.get("status"),
            "stage": job.get("stage"),
            "latest_event_id": job.get("latest_event_id"),
            "summary": summarize_executions(job, executions),
            "events_url": f"/api/v1/jobs/{job_id}/events",
            "executions_url": f"/api/v1/jobs/{job_id}/executions",
            "result_url": f"/api/v1/jobs/{job_id}/result",
        }
        child_job_ids = job.get("child_job_ids") or []
        if child_job_ids:
            subtasks = []
            for child_job_id in child_job_ids:
                child = self.store.get_job(child_job_id) or {}
                child_executions = self.store.list_executions(child_job_id)
                latest_allocation = (child_executions[-1].get("allocation") or {}) if child_executions else {}
                subtasks.append(
                    {
                        "job_id": child_job_id,
                        "task_id": f"task-{child_job_id}",
                        "status": child.get("status"),
                        "stage": child.get("stage"),
                        "shard_index": child.get("shard_index"),
                        "page_range": child.get("page_range"),
                        "target_worker": latest_allocation.get("worker_id") or child.get("target_worker"),
                        "executions": child_executions,
                    }
                )
            view["subtasks"] = subtasks
        if job.get("sharding") is not None:
            view["sharding"] = job.get("sharding")
        return view

    def release_allocation(self, allocation: dict[str, Any] | None) -> None:
        if not allocation:
            return
        for item in allocation.get("gpu_allocations", []):
            if item.get("lease_id"):
                self.scheduler.release(item)

    def result(self, job_id: str) -> Any:
        job = self.store.get_job(job_id)
        if not job:
            raise ExecutionNotFound(job_id)
        return self.artifacts.read_result(job.get("result_ref"))

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise ExecutionNotFound(job_id)
        if job.get("status") in TERMINAL_STATUSES:
            return self.execution_view(job_id)
        self.store.update_job(job_id, status="cancelled" if job.get("status") == "queued" else "cancelling", cancel_requested=True)
        self.hub.emit(job_id, "job.cancelled" if job.get("status") == "queued" else "job.cancelling", operation=job.get("operation"), stage="cancel")
        return self.execution_view(job_id)

    def wait_blocking(self, job_id: str, timeout_seconds: float | None) -> dict[str, Any]:
        deadline = None if timeout_seconds is None else time.monotonic() + max(timeout_seconds, 0)
        while True:
            view = self.execution_view(job_id)
            if view["status"] in TERMINAL_STATUSES or (deadline is not None and time.monotonic() >= deadline):
                return view
            delay = 0.2 if deadline is None else min(0.2, max(deadline - time.monotonic(), 0.01))
            time.sleep(delay)


_MANAGER: ExecutionManager | None = None
_MANAGER_KEY: tuple[str, str, str] | None = None


def get_execution_manager() -> ExecutionManager:
    global _MANAGER, _MANAGER_KEY
    key = (
        os.getenv("EXECUTION_MODE", "async").strip().lower(),
        os.getenv("EXECUTION_REDIS_URL", "").strip(),
        os.getenv("EXECUTION_DATA_DIR", "").strip(),
    )
    if _MANAGER is None or _MANAGER_KEY != key:
        _MANAGER = ExecutionManager()
        _MANAGER_KEY = key
    return _MANAGER
