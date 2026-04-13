import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .db import AsyncSessionLocal, init_db
from .deps.auth import require_admin
from .routers import admin_users, chat, knowledge, users
from .modules.component import COMPONENT_ROUTERS
from .modules.component.document_preproc import service as doc_preproc_service

#冲突检测new
from app.routers import conclictfix

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化数据库，关闭时可选清理。"""
    from .config import settings
    
    if settings.ENVIRONMENT == "production" and settings.JWT_SECRET == "change-me":
        raise RuntimeError(
            "生产环境必须设置 JWT_SECRET 环境变量，不能使用默认值 'change-me'"
        )
    try:
        await init_db()
        async with AsyncSessionLocal() as session:
            from .services.bootstrap_users import ensure_bootstrap_admin

            await ensure_bootstrap_admin(session)
    except Exception as e:
        logger.warning("数据库初始化失败（/health 仍可用）: %s", e)
    yield


app = FastAPI(title="轨道交通知识服务系统", lifespan=lifespan)

def _cors_origins() -> list[str]:
    """CORS_ORIGINS 逗号分隔，例如: http://localhost:5173,http://192.168.1.10:5173"""
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5178",
        "http://127.0.0.1:5178",
    ]


origins = _cors_origins()

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 添加请求日志中间件（仅记录异常）
@app.middleware("http")
async def log_requests(request: Request, call_next):
    try:
        response = await call_next(request)
        return response
    except Exception as e:
        logger.error(f"请求处理异常: {request.method} {request.url.path} - {e}", exc_info=True)
        raise

# 全局异常处理器
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.warning("请求验证失败: %s %s - %s", request.method, request.url.path, exc.errors())
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors()},
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error(f"未处理的异常: {request.method} {request.url.path}", exc_info=True)
    # 记录完整异常到日志，对客户端只返回通用信息，避免泄露内部细节
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "内部服务器错误，请稍后重试", "type": type(exc).__name__},
    )

app.include_router(chat.router)
app.include_router(knowledge.router)
app.include_router(users.router)
app.include_router(admin_users.router)
app.include_router(conclictfix.router) #new
for r in COMPONENT_ROUTERS:
    app.include_router(r)


@app.get("/api/available_models", dependencies=[Depends(require_admin)])
async def get_available_models() -> dict[str, Any]:
    """获取可用的文档处理模型信息。"""
    return doc_preproc_service.get_available_models()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

