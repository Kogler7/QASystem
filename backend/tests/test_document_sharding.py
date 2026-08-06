from __future__ import annotations

from backend.app.execution.artifacts import ArtifactStore
from backend.app.execution.document_sharding import (
    _configured_shard_count,
    _merge_results,
    _requires_sharding,
    build_page_ranges,
)
from backend.app.execution.queue import MemoryTaskQueue
from backend.app.execution.resources import GpuSnapshot, ResourceScheduler
from backend.app.schemas import ParserConfig
from backend.app.execution.store import MemoryExecutionStore
from backend.app.execution.supervisor import _auto_cpu_workers, _auto_vlm_workers


def test_page_ranges_are_balanced_contiguous_and_inclusive():
    ranges = build_page_ranges(33, 4)
    assert ranges == [(0, 8), (9, 16), (17, 24), (25, 32)]
    assert [page for start, end in ranges for page in range(start, end + 1)] == list(range(33))


def test_auto_engine_request_shards_when_execution_plan_selects_vlm(monkeypatch):
    monkeypatch.setattr("backend.app.execution.document_sharding._count_pages", lambda _content: 33)
    config = ParserConfig()
    page_count, ranges = _requires_sharding("sample.pdf", config, b"pdf", 8, "vlm")
    assert page_count == 33
    assert len(ranges) == 16
    assert ranges[0] == (0, 2)
    assert ranges[-1] == (31, 32)


def test_auto_shard_count_creates_work_stealing_backlog(monkeypatch):
    monkeypatch.setenv("EXECUTION_VLM_SHARD_COUNT", "auto")
    assert _configured_shard_count(ParserConfig(), 33, 8) == 16


def test_shard_markdown_is_merged_by_shard_index():
    ranges = [(0, 1), (2, 3)]
    result = _merge_results(
        "sample.pdf",
        b"pdf",
        [
            {"markdown": "second", "parser": {"final_engine": "vlm"}, "page_range": {"shard_index": 1}},
            {"markdown": "first", "parser": {"final_engine": "vlm"}, "page_range": {"shard_index": 0}},
        ],
        ranges,
    )
    assert result["markdown"] == "first\n\nsecond"
    assert result["sharding"]["page_ranges"] == [[0, 1], [2, 3]]


def test_binary_blob_is_written_once_and_removed(tmp_path):
    artifacts = ArtifactStore(tmp_path / "execution")
    reference = artifacts.write_blob("job-1", b"large-pdf")
    assert artifacts.read_blob(reference) == b"large-pdf"
    artifacts.remove_blob(reference)
    assert not (tmp_path / "execution" / "blobs" / "job-1.bin").exists()


def test_targeted_queue_does_not_leak_to_shared_consumers():
    queue = MemoryTaskQueue()
    resource_class = "gpu.vlm.test-target"
    queue.enqueue(resource_class, {"job_id": "job-1"}, target_worker="worker-1")
    assert queue.consume(resource_class, timeout=0.01) is None
    assert queue.consume(resource_class, timeout=0.01, target_worker="worker-1")["job_id"] == "job-1"


def test_scheduler_spreads_shards_across_physical_devices(monkeypatch):
    monkeypatch.setenv("EXECUTION_GPU_MEMORY_REQUEST_MB_GPU_VLM", "1024")
    ResourceScheduler._local_workers.clear()
    ResourceScheduler._local_leases.clear()
    scheduler = ResourceScheduler(MemoryExecutionStore(), worker_id="coordinator")
    scheduler.register_worker(
        worker_id="vlm-0",
        resource_classes=["gpu.vlm"],
        gpu_devices=["0"],
        snapshots=[GpuSnapshot("0", "GPU-0", 46068, 36000, 60, "test")],
        endpoint="http://127.0.0.1:8100",
    )
    scheduler.register_worker(
        worker_id="vlm-1",
        resource_classes=["gpu.vlm"],
        gpu_devices=["1"],
        snapshots=[GpuSnapshot("1", "GPU-1", 46068, 37000, 5, "test")],
        endpoint="http://127.0.0.1:8101",
    )
    first = scheduler.reserve_worker("gpu.vlm")
    assert first["allocation"]["gpu_allocations"][0]["physical_index"] == "0"
    second = scheduler.reserve_worker("gpu.vlm", exclude_devices={"0"})
    assert second["allocation"]["gpu_allocations"][0]["physical_index"] == "1"
    assert second["allocation"]["endpoint"] == "http://127.0.0.1:8101"
    scheduler.release(first["lease"])
    scheduler.release(second["lease"])


def test_scheduler_uses_each_worker_slot_before_queueing(monkeypatch):
    monkeypatch.setenv("EXECUTION_GPU_MEMORY_REQUEST_MB_GPU_VLM", "0")
    monkeypatch.setenv("EXECUTION_GPU_MAX_LEASES_PER_DEVICE", "16")
    monkeypatch.setenv("EXECUTION_GPU_MAX_LEASES_PER_WORKER", "1")
    ResourceScheduler._local_workers.clear()
    ResourceScheduler._local_leases.clear()
    ResourceScheduler._local_worker_leases.clear()
    scheduler = ResourceScheduler(MemoryExecutionStore(), worker_id="coordinator")
    for worker_id in ("vlm-0-0", "vlm-0-1"):
        scheduler.register_worker(
            worker_id=worker_id,
            resource_classes=["gpu.vlm"],
            gpu_devices=["0"],
            snapshots=[GpuSnapshot("0", "GPU-0", 24576, 18145, 0, "test")],
            endpoint="http://127.0.0.1:8100",
        )

    first = scheduler.reserve_worker("gpu.vlm")
    second = scheduler.reserve_worker("gpu.vlm")
    assert {first["worker"]["worker_id"], second["worker"]["worker_id"]} == {"vlm-0-0", "vlm-0-1"}
    assert scheduler.reserve_worker("gpu.vlm") is None
    scheduler.release(first["lease"])
    assert scheduler.reserve_worker("gpu.vlm") is not None


def test_worker_can_reserve_only_its_own_gpu_slot(monkeypatch):
    monkeypatch.setenv("EXECUTION_GPU_MEMORY_REQUEST_MB_GPU_VLM", "0")
    ResourceScheduler._local_workers.clear()
    ResourceScheduler._local_leases.clear()
    ResourceScheduler._local_worker_leases.clear()
    scheduler = ResourceScheduler(MemoryExecutionStore(), worker_id="coordinator")
    for worker_id, device in (("vlm-0", "0"), ("vlm-1", "1")):
        scheduler.register_worker(
            worker_id=worker_id,
            resource_classes=["gpu.vlm"],
            gpu_devices=[device],
            snapshots=[GpuSnapshot(device, f"GPU-{device}", 24576, 18000, 0, "test")],
        )

    reservation = scheduler.reserve_worker("gpu.vlm", required_worker_id="vlm-1")
    assert reservation["worker"]["worker_id"] == "vlm-1"
    assert reservation["allocation"]["gpu_allocations"][0]["physical_index"] == "1"


def test_supervisor_derives_worker_counts_from_cpu_and_gpu(monkeypatch):
    monkeypatch.setenv("EXECUTION_CPU_WORKERS", "auto")
    monkeypatch.setenv("EXECUTION_VLM_WORKERS_PER_GPU", "auto")
    assert _auto_cpu_workers(32) == 8
    assert _auto_vlm_workers(32, 4) == 2
    assert _auto_vlm_workers(32, 0) == 0
