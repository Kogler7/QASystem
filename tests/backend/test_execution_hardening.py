"""执行层加固的契约测试：协议感知重试、PEL 重投、协作式取消与错误码映射。"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import backend.app.execution.worker as worker_module
from backend.app.execution.artifacts import ArtifactStore
from backend.app.execution.document_sharding import PageShardError, _merge_results
from backend.app.execution.hub import ExecutionEventHub
from backend.app.execution.manager import ExecutionCancelled, ExecutionManager
from backend.app.execution.queue import MemoryTaskQueue, RedisTaskQueue
from backend.app.execution.store import MemoryExecutionStore
from backend.app.execution.worker import WorkerRuntime, _error_retryable
from backend.app.responses import ErrorCode, error_code_for_exception


class _CodedError(RuntimeError):
    """与 CapabilityError / PipelineError 同构的带协议错误码业务异常。"""

    def __init__(self, code: ErrorCode):
        super().__init__(code.name)
        self.code = code


class _FakeQueue:
    """记录 enqueue/ack/reclaim 的内存队列假件。"""

    def __init__(self, reclaimable: dict[str, list] | None = None):
        self._reclaimable = {key: list(value) for key, value in (reclaimable or {}).items()}
        self.enqueued: list[tuple[str, dict, object]] = []
        self.acked: list[tuple[str, str]] = []
        self.reclaim_calls: list[dict] = []

    def enqueue(self, resource_class, envelope, target_worker=None):
        self.enqueued.append((resource_class, envelope, target_worker))
        return "new-message-id"

    def ack(self, resource_class, message_id, target_worker=None):
        self.acked.append((resource_class, message_id))

    def reclaim(self, resource_class, consumer, *, min_idle_seconds, target_worker=None, count=32):
        self.reclaim_calls.append(
            {"resource_class": resource_class, "consumer": consumer, "min_idle_seconds": min_idle_seconds, "target_worker": target_worker}
        )
        return self._reclaimable.pop(resource_class, [])


def _bare_worker(monkeypatch, *, reclaimable: dict[str, list] | None = None, max_attempts: str = "3"):
    """不经 __init__ 构造 WorkerRuntime，仅装配本组测试需要的最小面。"""
    monkeypatch.setenv("EXECUTION_MAX_ATTEMPTS", max_attempts)
    monkeypatch.setattr(worker_module.time, "sleep", lambda *args, **kwargs: None)
    worker = object.__new__(WorkerRuntime)
    worker.worker_id = "cpu-test"
    worker.resource_classes = ("cpu.chunk",)
    worker._reaper_enabled = True
    worker._reaper_min_idle_seconds = 600.0
    worker._reaper_interval_seconds = 60.0
    worker._last_reap = 0.0
    store = MemoryExecutionStore()
    queue = _FakeQueue(reclaimable)
    released: list = []
    worker.manager = SimpleNamespace(
        store=store,
        hub=ExecutionEventHub(store),
        queue=queue,
        release_allocation=lambda allocation: released.append(allocation),
    )
    worker.queue = queue
    return worker, store, queue, released


# ---------------------------------------------------------------- 重试分类


def test_error_retryable_uses_protocol_table():
    # 协议 retryable=false：参数类、解析/切片结果类、本体无效。
    for code in (
        ErrorCode.INVALID_REQUEST,
        ErrorCode.DOCUMENT_CONTENT_EMPTY,
        ErrorCode.CHUNK_RESULT_EMPTY,
        ErrorCode.ONTOLOGY_INVALID,
        ErrorCode.VECTOR_ID_COUNT_MISMATCH,
    ):
        assert _error_retryable(_CodedError(code)) is False, code
    # 协议 retryable=true：网络/依赖/限流类。
    for code in (
        ErrorCode.LLM_TIMEOUT,
        ErrorCode.LLM_RATE_LIMITED,
        ErrorCode.PARSER_UNAVAILABLE,
        ErrorCode.PARTIAL_EXTRACTION_FAILED,
    ):
        assert _error_retryable(_CodedError(code)) is True, code


def test_unknown_errors_stay_retryable():
    assert _error_retryable(RuntimeError("connection reset")) is True
    assert _error_retryable(_CodedError(ErrorCode.LLM_TIMEOUT)) is True


def test_worker_skips_retry_for_non_retryable_error_code(monkeypatch):
    worker, store, queue, _released = _bare_worker(monkeypatch)
    store.create_job(job_id="job-1", operation="knowledge.ingestion", resource_class="workflow.knowledge")

    worker._handle_failure(
        "workflow.knowledge",
        {"job_id": "job-1", "attempt": 1, "operation": "knowledge.ingestion"},
        _CodedError(ErrorCode.DOCUMENT_CONTENT_EMPTY),
    )

    assert queue.enqueued == []
    job = store.get_job("job-1")
    assert job["status"] == "failed"
    assert job["error"]["retryable"] is False
    assert job["error"]["reason"] == "non_retryable_error_code"


def test_worker_retries_retryable_error_code(monkeypatch):
    worker, store, queue, _released = _bare_worker(monkeypatch)
    store.create_job(job_id="job-1", operation="knowledge.ingestion", resource_class="workflow.knowledge")

    worker._handle_failure(
        "workflow.knowledge",
        {"job_id": "job-1", "attempt": 1, "operation": "knowledge.ingestion"},
        _CodedError(ErrorCode.LLM_TIMEOUT),
    )

    assert len(queue.enqueued) == 1
    _resource_class, envelope, _target = queue.enqueued[0]
    assert envelope["attempt"] == 2
    assert store.get_job("job-1")["status"] == "queued"


def test_worker_fails_when_attempts_exhausted(monkeypatch):
    worker, store, queue, _released = _bare_worker(monkeypatch)
    store.create_job(job_id="job-1", operation="knowledge.ingestion", resource_class="workflow.knowledge")

    worker._handle_failure(
        "workflow.knowledge",
        {"job_id": "job-1", "attempt": 3, "operation": "knowledge.ingestion"},
        _CodedError(ErrorCode.LLM_TIMEOUT),
    )

    assert queue.enqueued == []
    assert store.get_job("job-1")["status"] == "failed"


def test_worker_marks_cancelled_without_retry(monkeypatch):
    worker, store, queue, _released = _bare_worker(monkeypatch)
    store.create_job(job_id="job-1", operation="document.parse", resource_class="cpu.document_orchestrator")

    worker._handle_failure(
        "cpu.document_orchestrator",
        {"job_id": "job-1", "attempt": 1, "operation": "document.parse"},
        ExecutionCancelled("文档页段任务已取消"),
    )

    assert queue.enqueued == []
    job = store.get_job("job-1")
    assert job["status"] == "cancelled"
    assert job["stage"] == "cancel"


# ---------------------------------------------------------------- PEL 重投


def test_reaper_requeues_stale_message_with_incremented_attempt(monkeypatch):
    envelope = {
        "job_id": "job-1",
        "attempt": 1,
        "operation": "document.parse",
        "reserved_allocation": {"gpu_allocations": [{"lease_id": "lease-x"}]},
    }
    worker, store, queue, released = _bare_worker(
        monkeypatch,
        reclaimable={"cpu.chunk": [("mid-9", envelope)]},
    )
    store.create_job(job_id="job-1", operation="document.parse", resource_class="cpu.chunk")

    worker._reap("cpu.chunk")

    # 共享队列与定向队列各收割一次；第一次即取回滞留消息。
    assert len(queue.reclaim_calls) == 2
    assert len(queue.enqueued) == 1
    _resource_class, next_envelope, _target = queue.enqueued[0]
    assert next_envelope["attempt"] == 2
    # 崩溃 worker 的 GPU 租约已随重投释放。
    assert released == [{"gpu_allocations": [{"lease_id": "lease-x"}]}]
    # 旧消息 ack 清除，job 回到 queued。
    assert ("cpu.chunk", "mid-9") in queue.acked
    job = store.get_job("job-1")
    assert job["status"] == "queued"
    assert job["stage"] == "requeued"


def test_reaper_fails_message_beyond_max_attempts(monkeypatch):
    envelope = {"job_id": "job-1", "attempt": 3, "operation": "document.parse"}
    worker, store, queue, _released = _bare_worker(
        monkeypatch,
        reclaimable={"cpu.chunk": [("mid-9", envelope)]},
    )
    store.create_job(job_id="job-1", operation="document.parse", resource_class="cpu.chunk")

    worker._reap("cpu.chunk")

    assert queue.enqueued == []
    assert ("cpu.chunk", "mid-9") in queue.acked
    job = store.get_job("job-1")
    assert job["status"] == "failed"
    assert job["stage"] == "reap"
    assert job["error"]["type"] == "ExecutionReapLimitExceeded"


def test_reaper_drops_orphan_message_without_job(monkeypatch):
    worker, _store, queue, _released = _bare_worker(
        monkeypatch,
        reclaimable={"cpu.chunk": [("mid-9", {"job_id": "gone", "attempt": 1, "operation": "document.parse"})]},
    )

    worker._reap("cpu.chunk")

    assert queue.enqueued == []
    assert ("cpu.chunk", "mid-9") in queue.acked


def test_reaper_honours_cancelled_job(monkeypatch):
    envelope = {"job_id": "job-1", "attempt": 1, "operation": "document.parse"}
    worker, store, queue, _released = _bare_worker(
        monkeypatch,
        reclaimable={"cpu.chunk": [("mid-9", envelope)]},
    )
    store.create_job(job_id="job-1", operation="document.parse", resource_class="cpu.chunk", cancel_requested=True)

    worker._reap("cpu.chunk")

    assert queue.enqueued == []
    assert ("cpu.chunk", "mid-9") in queue.acked
    assert store.get_job("job-1")["status"] == "cancelled"


def test_maybe_reap_respects_interval_and_enabled_switch(monkeypatch):
    worker, store, queue, _released = _bare_worker(monkeypatch)
    store.create_job(job_id="job-1", operation="document.parse", resource_class="cpu.chunk")
    worker._reaper_interval_seconds = 60.0

    worker._last_reap = time.monotonic()
    worker._maybe_reap()
    assert queue.reclaim_calls == []

    worker._last_reap = 0.0
    worker._maybe_reap()
    assert len(queue.reclaim_calls) == 2  # 共享 + 定向各一次

    worker._reaper_enabled = False
    worker._last_reap = 0.0
    worker._maybe_reap()
    assert len(queue.reclaim_calls) == 2


# ---------------------------------------------------------------- 队列 reclaim


class _FakeRedis:
    def __init__(self, entries):
        self.entries = entries
        self.calls: list[dict] = []

    def xgroup_create(self, *_args, **_kwargs):
        raise Exception("BUSYGROUP consumer group name already exists")

    def xpending_range(self, name, groupname, minimum, maximum, count, idle):
        self.calls.append({'name': name, 'group': groupname, 'min_idle_time': idle})
        return [{'message_id': mid, 'consumer': 'dead-worker'} for mid, _ in self.entries]

    def eval(self, script, key_count, stream, consumer_key, worker_key, group, consumer, message_id, owner, idle):
        self.calls.append({'consumer': consumer, 'worker_key': worker_key, 'owner': owner})
        return [[mid, ['payload', fields['payload']]] for mid, fields in self.entries if mid == message_id]


def test_redis_queue_reclaim_parses_guarded_claim_entries():
    redis = _FakeRedis([("1700000000000-0", {"payload": '{"job_id":"j1","attempt":2}'})])
    task_queue = RedisTaskQueue(SimpleNamespace(redis=redis, prefix="qa"))
    reaped = task_queue.reclaim("cpu.chunk", "cpu-0", min_idle_seconds=600)
    assert reaped == [("1700000000000-0", {"job_id": "j1", "attempt": 2})]
    assert redis.calls[0]['name'] == 'qa:queue:cpu.chunk'
    assert redis.calls[0]['group'] == 'workers'
    assert redis.calls[0]['min_idle_time'] == 600000
    assert redis.calls[1]['consumer'] == 'cpu-0'
    assert redis.calls[1]['worker_key'] == 'qa:workers:dead-worker'


def test_memory_queue_reclaim_is_empty():
    assert MemoryTaskQueue().reclaim("cpu.chunk", "cpu-0", min_idle_seconds=600) == []


# ---------------------------------------------------------------- 错误码映射


def test_merge_empty_results_map_to_document_content_empty():
    results = [{"markdown": "   ", "page_range": {"shard_index": 0}, "parser": {"final_engine": "vlm"}}]
    with pytest.raises(PageShardError) as captured:
        _merge_results("demo.pdf", b"pdf-bytes", results, [(0, 0)])

    assert captured.value.code == ErrorCode.DOCUMENT_CONTENT_EMPTY
    assert error_code_for_exception(captured.value) == ErrorCode.DOCUMENT_CONTENT_EMPTY


def test_error_code_for_exception_prefers_carried_code():
    assert error_code_for_exception(RuntimeError("boom")) == ErrorCode.INTERNAL_ERROR
    assert error_code_for_exception(_CodedError(ErrorCode.LLM_TIMEOUT)) == ErrorCode.LLM_TIMEOUT


# ---------------------------------------------------------------- 协作式取消


def _manager(tmp_path, monkeypatch):
    from backend.app.execution.resources import ResourceScheduler

    ResourceScheduler._local_leases.clear()
    ResourceScheduler._local_workers.clear()
    monkeypatch.setenv("EXECUTION_MODE", "inline")
    store = MemoryExecutionStore()
    manager = ExecutionManager(
        store=store,
        hub=ExecutionEventHub(store),
        artifacts=ArtifactStore(tmp_path / "execution"),
    )
    return manager, store


def test_wait_blocking_unblocks_on_cancel_request(tmp_path, monkeypatch):
    manager, store = _manager(tmp_path, monkeypatch)
    store.create_job(job_id="job-cancel", operation="knowledge.ingestion", status="running")

    store.update_job("job-cancel", cancel_requested=True)
    view = manager.wait_blocking("job-cancel", None)

    # 任务仍在后台运行（非终态），但等待方拿到 cancel_requested 后应及时松开。
    assert view["status"] == "running"
    assert view["cancel_requested"] is True


def test_is_cancel_requested_treats_missing_job_as_not_cancelled(tmp_path, monkeypatch):
    manager, _store = _manager(tmp_path, monkeypatch)
    assert manager.is_cancel_requested("missing-job") is False
    assert manager.is_cancel_requested(None) is False


def test_run_attempt_records_cancelled_execution(tmp_path, monkeypatch):
    manager, store = _manager(tmp_path, monkeypatch)
    submission = manager.submit(operation="unit.cancel", resource_class="cpu.default", payload={})

    def cancel(_payload):
        raise ExecutionCancelled("用户取消")

    with pytest.raises(ExecutionCancelled):
        manager.run_inline(submission.job_id, "unit.cancel", "cpu.default", cancel)

    execution = store.list_executions(submission.job_id)[0]
    assert execution["status"] == "cancelled"
    assert manager.execution_view(submission.job_id)["status"] == "cancelled"


def test_knowledge_ingestion_entry_honours_cancel_request(protocol_module):
    from backend.app.execution.manager import ExecutionCancelled, get_execution_manager
    from backend.app.execution.operations import run_operation

    manager = get_execution_manager()
    manager.store.create_job(
        job_id="job-entry-cancel",
        operation="knowledge.ingestion",
        resource_class="workflow.knowledge",
        cancel_requested=True,
    )

    with pytest.raises(ExecutionCancelled):
        run_operation(
            "knowledge.ingestion",
            {
                "__execution": {"job_id": "job-entry-cancel"},
                "ontology": "",
                "options": {},
                "files": [{"file_name": "demo.txt", "content": "hello"}],
                "batch": False,
            },
        )
