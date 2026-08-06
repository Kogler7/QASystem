from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from backend.app.execution.artifacts import ArtifactStore
from backend.app.execution.api import dispatch_operation, prefer_wait_seconds
from backend.app.execution.hub import ExecutionEventHub
from backend.app.execution.manager import ExecutionManager, ExecutionUnavailable
from backend.app.execution.resources import GpuInventory, GpuSnapshot, ResourceScheduler
from backend.app.execution.store import MemoryExecutionStore, _decode, _json
from backend.app.routers.execution import _parse_cursor


def _manager(tmp_path, monkeypatch, *, mode: str = "inline") -> tuple[ExecutionManager, MemoryExecutionStore]:
    ResourceScheduler._local_leases.clear()
    ResourceScheduler._local_workers.clear()
    monkeypatch.setenv("EXECUTION_MODE", mode)
    monkeypatch.setenv("EXECUTION_GPU_SCHEDULING", "true")
    monkeypatch.setenv("EXECUTION_GPU_DEVICE_IDS", "0")
    store = MemoryExecutionStore()
    manager = ExecutionManager(
        store=store,
        hub=ExecutionEventHub(store),
        artifacts=ArtifactStore(tmp_path / "execution"),
    )
    return manager, store


def test_async_mode_fails_closed_without_redis(tmp_path, monkeypatch):
    manager, _store = _manager(tmp_path, monkeypatch, mode="async")
    with pytest.raises(ExecutionUnavailable):
        manager.submit(operation="unit.echo", resource_class="cpu.default", payload={"value": 1})


def test_prefer_header_keeps_sync_default_and_supports_async_opt_in():
    assert prefer_wait_seconds(None) is None
    assert prefer_wait_seconds("") is None
    assert prefer_wait_seconds("respond-async") == 0.0
    assert prefer_wait_seconds("wait=12") == 12.0
    assert prefer_wait_seconds("respond-async, wait=3") == 3.0
    assert prefer_wait_seconds("wait=999") == 60.0


def test_async_runtime_waits_for_original_business_result_by_default():
    class FakeStore:
        @staticmethod
        def get_job(_job_id):
            return {"status": "succeeded"}

    class FakeManager:
        is_async = True
        store = FakeStore()

        @staticmethod
        def submit(**_kwargs):
            return SimpleNamespace(job_id="job-sync")

        @staticmethod
        def wait_blocking(job_id, timeout_seconds):
            assert job_id == "job-sync"
            assert timeout_seconds is None
            return {"job_id": job_id, "status": "succeeded", "summary": {}}

        @staticmethod
        def result(job_id):
            assert job_id == "job-sync"
            return {"original": "business-data"}

        @staticmethod
        def execution_view(job_id):
            return {"job_id": job_id, "status": "queued"}

    result = asyncio.run(
        dispatch_operation(
            operation="unit.echo",
            resource_class="cpu.default",
            payload={"value": 1},
            manager=FakeManager(),
        )
    )

    assert result.status_code == 200
    assert result.data == {"original": "business-data"}
    assert result.execution["job_id"] == "job-sync"


def test_respond_async_returns_task_handle_without_waiting():
    class FakeManager:
        is_async = True

        @staticmethod
        def submit(**_kwargs):
            return SimpleNamespace(job_id="job-async")

        @staticmethod
        def wait_blocking(_job_id, _timeout_seconds):
            raise AssertionError("respond-async must not block")

        @staticmethod
        def execution_view(job_id):
            return {"job_id": job_id, "status": "queued", "summary": {}}

    result = asyncio.run(
        dispatch_operation(
            operation="unit.echo",
            resource_class="cpu.default",
            payload={"value": 1},
            wait_seconds=prefer_wait_seconds("respond-async"),
            manager=FakeManager(),
        )
    )

    assert result.status_code == 202
    assert result.accepted is True
    assert result.data == {"job_id": "job-async", "status": "queued"}


def test_async_gpu_submission_reserves_and_targets_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTION_ALLOW_MEMORY_QUEUE", "true")
    manager, store = _manager(tmp_path, monkeypatch, mode="async")
    manager.scheduler.register_worker(
        worker_id="embedding-0",
        resource_classes=["gpu.embedding"],
        gpu_devices=["0"],
        snapshots=[GpuSnapshot("0", "GPU-0", 48000, 1000, 5, "test")],
    )

    submission = manager.submit(operation="unit.echo", resource_class="gpu.embedding", payload={"value": 1})

    job = store.get_job(submission.job_id)
    assert job["target_worker"] == "embedding-0"
    assert job["reserved_allocation"]["gpu_allocations"][0]["physical_index"] == "0"
    assert job["reserved_allocation"]["gpu_allocations"][0]["lease_id"]
    manager.release_allocation(job["reserved_allocation"])
    assert not ResourceScheduler._local_leases


def test_async_gpu_submission_fails_without_schedulable_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTION_ALLOW_MEMORY_QUEUE", "true")
    manager, _store = _manager(tmp_path, monkeypatch, mode="async")

    with pytest.raises(ExecutionUnavailable, match="没有可用"):
        manager.submit(operation="unit.echo", resource_class="gpu.embedding", payload={"value": 1})


def test_inline_attempt_records_allocation_timeline_and_releases_lease(tmp_path, monkeypatch):
    manager, store = _manager(tmp_path, monkeypatch)
    submission = manager.submit(operation="unit.echo", resource_class="gpu.embedding", payload={"value": 1})

    result = manager.run_inline(
        submission.job_id,
        "unit.echo",
        "gpu.embedding",
        lambda payload: {"value": payload["value"]},
    )

    execution = store.list_executions(submission.job_id)[0]
    assert result == {"value": 1}
    assert execution["status"] == "succeeded"
    assert execution["allocation"]["gpu_allocations"][0]["lease_id"]
    assert execution["timestamps"]["execution_started_at"]
    assert execution["timestamps"]["execution_finished_at"]
    assert execution["durations_ms"]["queue"] >= 0
    assert not ResourceScheduler._local_leases
    assert manager.execution_view(submission.job_id)["result_url"] == (
        f"/api/v1/jobs/{submission.job_id}/result"
    )


def test_failed_attempt_is_persisted_before_error_bubbles(tmp_path, monkeypatch):
    manager, store = _manager(tmp_path, monkeypatch)
    submission = manager.submit(operation="unit.fail", resource_class="cpu.default", payload={})

    def fail(_payload):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        manager.run_inline(submission.job_id, "unit.fail", "cpu.default", fail)

    execution = store.list_executions(submission.job_id)[0]
    assert execution["status"] == "failed"
    assert execution["error"]["message"] == "boom"
    assert manager.execution_view(submission.job_id)["status"] == "failed"


def test_gpu_scheduler_prefers_free_and_less_utilized_device(monkeypatch):
    monkeypatch.setenv("EXECUTION_GPU_SCHEDULING", "true")
    store = MemoryExecutionStore()
    inventory = GpuInventory(cache_seconds=0)
    inventory.snapshots = lambda force=False: [
        GpuSnapshot("0", "GPU-0", 48000, 40000, 10, "test"),
        GpuSnapshot("1", "GPU-1", 48000, 10000, 70, "test"),
    ]
    scheduler = ResourceScheduler(store, inventory=inventory)
    leases = scheduler.acquire("gpu.embedding", memory_request_mb=1000)
    assert leases[0].device.physical_index == "1"
    assert scheduler.release(leases[0]) is True


def test_last_event_id_cursor_accepts_sequence_and_bad_input():
    assert _parse_cursor(None) == 0
    assert _parse_cursor("job-1:42") == 42
    assert _parse_cursor("invalid") == 0


def test_redis_scalar_encoding_preserves_cancellation_boolean():
    assert _decode(_json(False)) is False
    assert _decode("False") is False


def test_mcp_heavy_tool_can_return_durable_handle(monkeypatch):
    monkeypatch.setenv("MCP_EXECUTION_MODE", "async")

    class FakeManager:
        is_async = True

        def submit(self, **kwargs):
            assert kwargs["operation"] == "mcp.visualization.draw"
            return SimpleNamespace(job_id="job-1")

        def execution_view(self, job_id):
            return {
                "job_id": job_id,
                "status": "queued",
                "events_url": "/api/v1/jobs/job-1/events",
                "executions_url": "/api/v1/jobs/job-1/executions",
            }

    import backend.app.execution.manager as execution_manager
    from mcp_services.domains.visualization import charts

    monkeypatch.setattr(execution_manager, "get_execution_manager", lambda: FakeManager())
    result = charts.draw_bar_chart(["A"], [1])
    assert result["code"] == 0
    assert result["data"]["accepted"] is True
    assert result["data"]["job_id"] == "job-1"


def test_mcp_sync_mode_queues_but_preserves_original_tool_result(monkeypatch):
    monkeypatch.setenv("MCP_EXECUTION_MODE", "sync")

    class FakeStore:
        @staticmethod
        def get_job(_job_id):
            return {"status": "succeeded"}

    class FakeManager:
        is_async = True
        store = FakeStore()

        @staticmethod
        def submit(**kwargs):
            assert kwargs["operation"] == "mcp.visualization.draw"
            return SimpleNamespace(job_id="job-sync-mcp")

        @staticmethod
        def wait_blocking(job_id, timeout_seconds):
            assert job_id == "job-sync-mcp"
            assert timeout_seconds is None
            return {"job_id": job_id, "status": "succeeded"}

        @staticmethod
        def result(job_id):
            assert job_id == "job-sync-mcp"
            return {"code": 0, "info": "图表生成成功", "data": {"path": "result.png"}}

    import backend.app.execution.manager as execution_manager
    from mcp_services.domains.visualization import charts

    monkeypatch.setattr(execution_manager, "get_execution_manager", lambda: FakeManager())
    result = charts.draw_bar_chart(["A"], [1])
    assert result == {"code": 0, "info": "图表生成成功", "data": {"path": "result.png"}}
