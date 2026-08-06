from __future__ import annotations

from backend.app.execution.artifacts import ArtifactStore
from backend.app.execution.document_sharding import _merge_results, build_page_ranges
from backend.app.execution.queue import MemoryTaskQueue
from backend.app.execution.resources import GpuSnapshot, ResourceScheduler
from backend.app.execution.store import MemoryExecutionStore
from backend.app.execution.supervisor import _auto_cpu_workers, _auto_vlm_workers


def test_page_ranges_are_balanced_contiguous_and_inclusive():
    ranges = build_page_ranges(33, 4)
    assert ranges == [(0, 8), (9, 16), (17, 24), (25, 32)]
    assert [page for start, end in ranges for page in range(start, end + 1)] == list(range(33))


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


def test_supervisor_derives_worker_counts_from_cpu_and_gpu(monkeypatch):
    monkeypatch.setenv("EXECUTION_CPU_WORKERS", "auto")
    monkeypatch.setenv("EXECUTION_VLM_WORKERS_PER_GPU", "auto")
    assert _auto_cpu_workers(32) == 8
    assert _auto_vlm_workers(32, 4) == 2
