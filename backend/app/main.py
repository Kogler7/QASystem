import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .db import init_db
from .routers import chat, knowledge, users

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化数据库，关闭时可选清理。"""
    try:
        await init_db()
    except Exception as e:
        logger.warning("数据库初始化失败（/health 仍可用）: %s", e)
    yield


app = FastAPI(title="轨道交通知识服务系统", lifespan=lifespan)

# 允许的跨域来源（开发/生产按需收紧）
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(knowledge.router)
app.include_router(users.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

