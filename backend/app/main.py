from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from .environment import configure_runtime_environment
from .health import full_health


configure_runtime_environment()

from .routers.confidence import router as confidence_router
from .routers.algorithm_capabilities import router as capabilities_router
from .routers.knowledge_precipitation import router as knowledge_router
from mcp_services.server import mcp
from .responses import ErrorCode, error_response


mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        from algorithm.confidence_calculate.calculator import close_confidence_resources

        close_confidence_resources()


app = FastAPI(title="QASystem Knowledge Services", lifespan=lifespan)
app.include_router(capabilities_router)
app.include_router(knowledge_router)
app.include_router(confidence_router)


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
    if full or validate_models:
        result = full_health()
        if result["status"] != "ok":
            return JSONResponse(status_code=503, content=result)
        return result
    return {"status": "ok"}


app.mount("/", mcp_app)
