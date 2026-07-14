from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

from .routers.confidence import router as confidence_router
from .routers.knowledge_precipitation import router as knowledge_router
from .routers.vectorization import router as vectorization_router
from mcp_services.server import mcp
from .responses import ErrorCode, payload


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
app.include_router(knowledge_router)
app.include_router(vectorization_router)
app.include_router(confidence_router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=payload(
            message="请求参数校验失败",
            code=ErrorCode.INVALID_REQUEST,
            error={"stage": "validation", "details": jsonable_encoder(exc.errors())},
        ),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=payload(
            message=str(exc.detail),
            code=ErrorCode.INVALID_REQUEST,
            error={"stage": "http", "details": exc.detail},
        ),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=payload(
            message="服务处理异常",
            code=ErrorCode.INTERNAL_ERROR,
            error={"stage": "application", "details": {"type": type(exc).__name__, "message": str(exc)}},
        ),
    )


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.mount("/", mcp_app)
