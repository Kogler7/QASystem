from __future__ import annotations

import math
import os
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from backend.app.schemas import ParserConfig

from .artifacts import ArtifactStore
from .manager import ExecutionManager, get_execution_manager


class PageShardError(RuntimeError):
    def __init__(self, message: str, *, failed: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.failed = failed or []


def _read_content(payload: dict[str, Any]) -> bytes:
    reference = payload.get("content_ref")
    if reference:
        return ArtifactStore.read_blob(str(reference))
    value = payload.get("content")
    if isinstance(value, bytes):
        return value
    raise ValueError("document payload content or content_ref is required")


def _count_pages(content: bytes) -> int:
    from pypdf import PdfReader

    return len(PdfReader(BytesIO(content)).pages)


def build_page_ranges(page_count: int, shard_count: int) -> list[tuple[int, int]]:
    """Return inclusive, contiguous page ranges balanced across workers."""
    if page_count <= 0:
        return []
    count = max(1, min(page_count, shard_count))
    base, remainder = divmod(page_count, count)
    ranges: list[tuple[int, int]] = []
    start = 0
    for index in range(count):
        size = base + (1 if index < remainder else 0)
        end = start + size - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def _min_pages() -> int:
    try:
        return max(1, int(os.getenv("EXECUTION_VLM_SHARD_MIN_PAGES", "8")))
    except (TypeError, ValueError):
        return 8


def _configured_shard_count(config: ParserConfig, page_count: int, worker_count: int) -> int:
    try:
        configured = max(1, int(os.getenv("EXECUTION_VLM_SHARD_COUNT", "4")))
    except (TypeError, ValueError):
        configured = 4
    available_workers = worker_count if worker_count > 0 else configured
    count = min(config.max_page_shards, configured, available_workers, page_count)
    if config.page_shard_size:
        count = max(count, math.ceil(page_count / config.page_shard_size))
        count = min(count, config.max_page_shards, page_count)
    return max(1, count)


def _requires_sharding(file_name: str, config: ParserConfig, content: bytes, worker_count: int) -> tuple[int, list[tuple[int, int]]]:
    if Path(file_name).suffix.lower() != ".pdf":
        return 0, []
    if config.page_sharding == "off" or config.engine != "vlm":
        return 0, []
    page_count = _count_pages(content)
    if config.page_sharding == "auto" and page_count < _min_pages():
        return page_count, []
    shard_count = _configured_shard_count(config, page_count, worker_count)
    return page_count, build_page_ranges(page_count, shard_count)


def _inline_parse(payload: dict[str, Any], config: ParserConfig, ranges: list[tuple[int, int]]) -> dict[str, Any]:
    from backend.app.services.algorithm_capabilities import document_parsing_service

    content = _read_content(payload)
    if not ranges:
        return document_parsing_service.parse(payload.get("file_name", "upload.bin"), content, config)
    results = [
        document_parsing_service.parse(
            payload.get("file_name", "upload.bin"),
            content,
            config.model_copy(update={"page_sharding": "off"}),
            page_range=page_range,
            shard_index=index,
            total_shards=len(ranges),
        )
        for index, page_range in enumerate(ranges)
    ]
    return _merge_results(payload.get("file_name", "upload.bin"), content, results, ranges)


def _merge_results(
    file_name: str,
    content: bytes,
    results: list[dict[str, Any]],
    ranges: list[tuple[int, int]],
) -> dict[str, Any]:
    if not results:
        raise PageShardError("没有可合并的页段结果")
    ordered = sorted(results, key=lambda item: (item.get("page_range") or {}).get("shard_index", 0))
    markdown = "\n\n".join(str(item.get("markdown", "")).strip() for item in ordered).strip()
    if not markdown:
        raise PageShardError("页段合并后内容为空")
    parser = dict(ordered[0].get("parser") or {})
    shard_count = len(ranges) or len(ordered)
    parser["sharding"] = {
        "enabled": shard_count > 1,
        "shard_count": shard_count,
        "page_ranges": [[start, end] for start, end in ranges],
    }
    return {
        "file_name": file_name,
        "file_size": len(content),
        "file_type": Path(file_name).suffix.lower().lstrip("."),
        "markdown": markdown,
        "content_length": len(markdown),
        "parser": parser,
        "sharding": {
            "enabled": shard_count > 1,
            "page_count": sum(end - start + 1 for start, end in ranges) if ranges else None,
            "shard_count": shard_count,
            "page_ranges": [[start, end] for start, end in ranges],
        },
    }


def _emit_child_state(manager: ExecutionManager, parent_job_id: str, spec: dict[str, Any], state: str) -> None:
    execution = spec.get("execution") or {}
    allocation = execution.get("allocation") or {}
    manager.hub.emit(
        parent_job_id,
        f"shard.{state}",
        workflow_id=spec.get("workflow_id"),
        task_id=execution.get("task_id") or spec.get("task_id"),
        attempt_id=execution.get("attempt_id"),
        operation="document.parse.shard",
        stage="document.parse.shard",
        source=allocation,
        details={
            "child_job_id": spec["job_id"],
            "shard_index": spec["shard_index"],
            "page_range": spec["page_range"],
            "status": state,
            "execution_id": execution.get("execution_id"),
            "attempt": execution.get("attempt"),
            "timestamps": execution.get("timestamps"),
            "durations_ms": execution.get("durations_ms"),
        },
    )


def _monitor_children(manager: ExecutionManager, parent_job_id: str, specs: list[dict[str, Any]]) -> None:
    pending = {spec["job_id"] for spec in specs}
    seen: dict[str, tuple[Any, ...]] = {}
    by_id = {spec["job_id"]: spec for spec in specs}
    deadline = time.monotonic() + float(os.getenv("EXECUTION_SHARD_TIMEOUT_SECONDS", "3600"))
    while pending:
        if time.monotonic() >= deadline:
            for job_id in pending:
                manager.cancel(job_id)
            raise PageShardError(
                "文档页段任务等待超时",
                failed=[{"child_job_id": job_id, "status": "timeout"} for job_id in sorted(pending)],
            )
        parent = manager.store.get_job(parent_job_id) or {}
        if parent.get("cancel_requested"):
            for job_id in pending:
                manager.cancel(job_id)
            raise PageShardError("文档页段任务已取消")
        for job_id in list(pending):
            spec = by_id[job_id]
            view = manager.execution_view(job_id)
            executions = manager.store.list_executions(job_id)
            latest = executions[-1] if executions else {}
            job_status = view.get("status") or "queued"
            state = latest.get("status") or job_status
            signature = (
                state,
                latest.get("execution_id"),
                latest.get("attempt"),
                latest.get("allocation", {}).get("worker_id") if isinstance(latest.get("allocation"), dict) else None,
            )
            if seen.get(job_id) != signature:
                seen[job_id] = signature
                spec["execution"] = latest
                _emit_child_state(manager, parent_job_id, spec, state)
            if job_status in {"succeeded", "failed", "cancelled"}:
                pending.remove(job_id)
        if pending:
            time.sleep(0.2)


def _submit_child(
    manager: ExecutionManager,
    parent: dict[str, Any],
    payload: dict[str, Any],
    config: ParserConfig,
    blob_ref: str,
    page_range: tuple[int, int] | None,
    shard_index: int,
    total_shards: int,
    exclude_workers: set[str] | None = None,
    exclude_devices: set[str] | None = None,
) -> dict[str, Any]:
    scheduler = manager.scheduler
    reservation = scheduler.reserve_worker(
        "gpu.vlm",
        exclude_workers=exclude_workers,
        exclude_devices=exclude_devices,
    )
    if reservation is None and (exclude_workers or exclude_devices):
        reservation = scheduler.reserve_worker("gpu.vlm")
    worker = reservation.get("worker") if reservation else None
    allocation = reservation.get("allocation") if reservation else None
    child_payload = {
        "file_name": payload.get("file_name", "upload.bin"),
        "content_ref": blob_ref,
        "config": config.model_copy(update={"page_sharding": "off"}).model_dump(),
        "page_range": list(page_range) if page_range else None,
        "shard_index": shard_index,
        "total_shards": total_shards,
    }
    submission = manager.submit(
        operation="document.parse.shard",
        resource_class="gpu.vlm",
        payload=child_payload,
        tenant_id=str(parent.get("tenant_id", "default")),
        priority=int(parent.get("priority", 50)),
        workflow_id=parent.get("workflow_id"),
        parent_job_id=parent.get("job_id"),
        parent_task_id=(payload.get("__execution") or {}).get("task_id"),
        shard_index=shard_index,
        page_range=list(page_range) if page_range else None,
        target_worker=(worker or {}).get("worker_id") if worker else None,
        reserved_allocation=allocation,
    )
    return {
        "job_id": submission.job_id,
        "task_id": f"task-{submission.job_id}",
        "workflow_id": parent.get("workflow_id"),
        "shard_index": shard_index,
        "page_range": list(page_range) if page_range else None,
        "allocation": allocation,
        "worker_id": (worker or {}).get("worker_id") if worker else None,
        "physical_index": (
            ((allocation or {}).get("gpu_allocations") or [{}])[0].get("physical_index")
            if allocation
            else None
        ),
    }


def orchestrate_document_parse(payload: dict[str, Any]) -> dict[str, Any]:
    manager = get_execution_manager()
    parent_execution = payload.get("__execution") or {}
    parent_job_id = parent_execution.get("job_id")
    content = _read_content(payload)
    config = ParserConfig.model_validate(payload.get("config") or {})
    worker_count = len(manager.scheduler.list_workers("gpu.vlm"))
    page_count, ranges = _requires_sharding(payload.get("file_name", "upload.bin"), config, content, worker_count)
    if not ranges:
        ranges = []
    if not parent_job_id or not manager.is_async:
        return _inline_parse(payload, config, ranges)

    blob_ref = manager.artifacts.write_blob(parent_job_id, content)
    parent = manager.store.get_job(parent_job_id) or {"job_id": parent_job_id}
    shard_ranges = ranges or [None]
    specs: list[dict[str, Any]] = []
    try:
        manager.store.update_job(
            parent_job_id,
            total_units=len(shard_ranges),
            sharding={
                "enabled": len(shard_ranges) > 1,
                "page_count": page_count or None,
                "shard_count": len(shard_ranges),
                "page_ranges": [list(item) for item in ranges],
            },
        )
        for shard_index, page_range in enumerate(shard_ranges):
            assigned_workers = {item["worker_id"] for item in specs if item.get("worker_id")}
            assigned_devices = {
                str(item["physical_index"])
                for item in specs
                if item.get("physical_index") is not None
            }
            specs.append(
                _submit_child(
                    manager,
                    parent,
                    payload,
                    config,
                    blob_ref,
                    page_range,
                    shard_index,
                    len(shard_ranges),
                    assigned_workers,
                    assigned_devices,
                )
            )
        manager.store.update_job(parent_job_id, child_job_ids=[item["job_id"] for item in specs])
        _monitor_children(manager, parent_job_id, specs)
        failed = []
        results = []
        for spec in specs:
            child_job = manager.store.get_job(spec["job_id"]) or {}
            if child_job.get("status") != "succeeded":
                failed.append({
                    "child_job_id": spec["job_id"],
                    "shard_index": spec["shard_index"],
                    "page_range": spec["page_range"],
                    "status": child_job.get("status"),
                    "error": child_job.get("error"),
                })
                continue
            results.append(manager.result(spec["job_id"]))
        if failed:
            raise PageShardError("一个或多个文档页段解析失败", failed=failed)
        merged = _merge_results(payload.get("file_name", "upload.bin"), content, results, ranges)
        manager.store.update_job(parent_job_id, completed_units=len(specs))
        return merged
    finally:
        manager.artifacts.remove_blob(blob_ref)


def parse_document_shard(payload: dict[str, Any]) -> dict[str, Any]:
    from backend.app.services.algorithm_capabilities import document_parsing_service

    content = _read_content(payload)
    page_range = payload.get("page_range")
    parsed_range = tuple(int(item) for item in page_range) if page_range else None
    config = ParserConfig.model_validate(payload.get("config") or {})
    return document_parsing_service.parse(
        payload.get("file_name", "upload.bin"),
        content,
        config,
        page_range=parsed_range,
        shard_index=payload.get("shard_index"),
        total_shards=payload.get("total_shards"),
    )


__all__ = ["PageShardError", "build_page_ranges", "orchestrate_document_parse", "parse_document_shard"]
