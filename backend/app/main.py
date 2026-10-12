from docker.configuration import getenv as config_getenv
import os
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from .environment import configure_runtime_environment
from .health import full_health


configure_runtime_environment()

from .routers.confidence import router as confidence_router
from .routers.algorithm_capabilities import router as capabilities_router
from .routers.knowledge_precipitation import router as knowledge_router
from .routers.execution import router as execution_router
from mcp_services.server import mcp
from mcp_services.runtime import streamable_http_app
from external_services.application import capture_user_headers, reset_user_headers
from . import api_recording, observability
from .responses import ErrorCode, error_response


mcp_app = streamable_http_app()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时先清一轮过期 API 请求记录；每次新建记录前还会再清（见 api_recording）。
    api_recording.cleanup_old_records()
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="QASystem Knowledge Services", lifespan=lifespan)
app.include_router(capabilities_router)
app.include_router(knowledge_router)
app.include_router(confidence_router)
app.include_router(execution_router)


@app.middleware("http")
async def capture_application_user_context(request: Request, call_next):
    token = capture_user_headers(dict(request.headers))
    try:
        return await call_next(request)
    finally:
        reset_user_headers(token)


async def _proxy_agent(request: Request, path: str = "") -> StreamingResponse:
    """Expose the isolated Agent service through the Backend public endpoint."""
    upstream = config_getenv("AGENT_INTERNAL_URL", "http://agent:9180").rstrip("/")
    target = f"{upstream}/{path.lstrip('/')}"
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "connection", "content-length"}
    }
    client = httpx.AsyncClient(timeout=None)
    try:
        response = await client.send(
            client.build_request(
                request.method,
                target,
                params=request.query_params,
                headers=headers,
                content=request.stream(),
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        return JSONResponse(
            status_code=503,
            content={"detail": f"Agent service unavailable: {exc}"},
        )

    response_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"connection", "content-length", "transfer-encoding"}
    }
    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        headers=response_headers,
        background=BackgroundTask(client.aclose),
    )


app.add_api_route("/agent", _proxy_agent, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
app.add_api_route("/agent/{path:path}", _proxy_agent, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(
        ErrorCode.INVALID_REQUEST,
        "请求参数校验失败",
        stage="validation",
        details=jsonable_encoder(exc.errors()),
        status_code=422,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    code = {
        404: ErrorCode.RESOURCE_NOT_FOUND,
        413: ErrorCode.PAYLOAD_TOO_LARGE,
        415: ErrorCode.UNSUPPORTED_FILE_TYPE,
        429: ErrorCode.LLM_RATE_LIMITED,
    }.get(exc.status_code, ErrorCode.INVALID_REQUEST)
    return error_response(
        code,
        str(exc.detail),
        stage="http",
        details=exc.detail,
        status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    return error_response(
        ErrorCode.INTERNAL_ERROR,
        "服务处理异常",
        stage="application",
        details={"exception_type": type(exc).__name__, "message": str(exc)},
    )


@app.get("/health", include_in_schema=False, response_model=None)
async def health(
    full: bool = Query(default=False),
    validate_models: bool = Query(default=False),
) -> dict | JSONResponse:
    ready_file = config_getenv("EXECUTION_RUNTIME_READY_FILE", "").strip()
    if ready_file and not Path(ready_file).is_file():
        return JSONResponse(status_code=503, content={"status": "starting", "reason": "runtime warmup pending"})
    if full or validate_models:
        result = full_health()
        if result["status"] != "ok":
            return JSONResponse(status_code=503, content=result)
        return result
    return {"status": "ok"}


@app.get("/api/v1/observability/metrics", include_in_schema=False, response_model=None)
async def observability_metrics() -> dict[str, Any]:
    """进程内指标：队列深度、job 状态计数、空抽取/慢阶段计数、阶段耗时分位数。

    暴露方式与 /health 一致（不鉴权）；执行层不可用时对应字段降级为空值。
    """
    return observability.metrics_payload()


app.mount("/", mcp_app)
