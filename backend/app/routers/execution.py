from __future__ import annotations
from backend.app.services.extraction_config import ExtractionConfigurationError

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.app.responses import ErrorCode, error_response, payload
from backend.app.execution.hub import get_event_hub
from backend.app.execution.manager import ExecutionNotFound, get_execution_manager
from backend.app.execution.models import LEVEL_ORDER
from backend.app.execution.resources import ResourceScheduler
from backend.app.execution.api import ExecutionUnavailable, dispatch_operation, prefer_wait_seconds


router = APIRouter(tags=["execution"])


def _parse_cursor(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(value.rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return 0


@router.post("/api/v1/jobs")
async def submit_job(request: Request, body: dict[str, Any]):
    operation = str(body.get("operation", "")).strip()
    resource_class = str(body.get("resource_class", "cpu.default")).strip()
    task_payload = body.get("payload")
    if not operation or not isinstance(task_payload, dict):
        raise HTTPException(status_code=400, detail="operation 和对象类型 payload 不能为空")
    try:
        requested_wait = prefer_wait_seconds(request.headers.get("Prefer"))
        result = await dispatch_operation(
            operation=operation,
            resource_class=resource_class,
            payload=task_payload,
            tenant_id=str(body.get("tenant_id", "default")),
            priority=int(body.get("priority", 50)),
            total_units=max(int(body.get("total_units", 1)), 1),
            wait_seconds=0.0 if requested_wait is None else requested_wait,
        )
    except ExtractionConfigurationError as exc:
        return error_response(ErrorCode.INVALID_REQUEST, str(exc), stage="configuration", status_code=422)
    except ExecutionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse(
        status_code=result.status_code,
        content=payload(result.data, "任务提交完成" if result.status_code == 200 else "任务已入队", execution=result.execution),
    )


@router.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str):
    manager = get_execution_manager()
    job = manager.store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    return payload(job, "任务状态查询完成", execution=manager.execution_view(job_id))


@router.get("/api/v1/jobs/{job_id}/result")
async def get_job_result(job_id: str):
    manager = get_execution_manager()
    try:
        job = manager.store.get_job(job_id)
        if not job:
            raise ExecutionNotFound(job_id)
        if job.get("status") != "succeeded":
            raise HTTPException(status_code=409, detail=f"任务尚未成功完成: {job.get('status')}")
        return payload(manager.result(job_id), "任务结果查询完成", execution=manager.execution_view(job_id))
    except ExecutionNotFound:
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}") from None


@router.delete("/api/v1/jobs/{job_id}")
async def cancel_job(job_id: str):
    try:
        execution = get_execution_manager().cancel(job_id)
    except ExecutionNotFound:
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}") from None
    return payload(execution, "任务取消请求已提交", execution=execution)


@router.get("/api/v1/jobs/{job_id}/executions")
async def list_job_executions(
    job_id: str,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
):
    manager = get_execution_manager()
    if not manager.store.get_job(job_id):
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    records = manager.store.list_executions(job_id)
    selected = records[cursor : cursor + limit]
    next_cursor = cursor + len(selected) if cursor + len(selected) < len(records) else None
    data = {"items": selected, "next_cursor": next_cursor, "total": len(records)}
    return payload(data, "任务执行明细查询完成", execution=manager.execution_view(job_id))


@router.get("/api/v1/jobs/{job_id}/events")
async def stream_job_events(
    job_id: str,
    request: Request,
    min_level: str = Query(default="INFO"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    normalized = min_level.upper()
    if normalized not in LEVEL_ORDER:
        raise HTTPException(status_code=400, detail="min_level 必须是 TRACE、DEBUG、INFO、WARN 或 ERROR")
    manager = get_execution_manager()
    if not manager.store.get_job(job_id):
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    cursor = _parse_cursor(last_event_id)
    hub = get_event_hub()

    async def events() -> AsyncIterator[str]:
        async for event in hub.subscribe(job_id, after_sequence=cursor, min_level=normalized):
            if await request.is_disconnected():
                return
            yield f"id: {event['event_id']}\nevent: execution\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
        yield ": stream-complete\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/v1/observability/levels")
async def get_log_levels():
    return payload(get_event_hub().levels(), "日志级别查询完成")


@router.put("/api/v1/observability/levels")
async def set_log_level(body: dict[str, Any]):
    try:
        value = get_event_hub().set_level(
            scope=str(body.get("scope", "global")),
            key=str(body.get("key", "*")),
            level=str(body.get("level", "INFO")),
            ttl_seconds=int(body.get("ttl_seconds", 3600)),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return payload(value, "日志级别已更新")


@router.get("/api/v1/resources/gpus")
async def list_gpu_resources():
    manager = get_execution_manager()
    return payload(ResourceScheduler(manager.store).describe(), "GPU 资源状态查询完成")


@router.get("/api/v1/events/stream")
async def stream_events_not_scoped(
    _request: Request,
    scope: str = Query(default="tenant"),
):
    # A global stream requires tenant authorization and fan-out storage. Keep
    # the route explicit until authentication is wired into the gateway.
    raise HTTPException(status_code=501, detail=f"范围事件流尚未启用: {scope}")
