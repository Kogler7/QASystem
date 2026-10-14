"""
agentcode HTTP API — 高并发异步任务模型。

核心架构:
    请求入队 → 返回 task_id → 内嵌工作线程消费执行 → 客户端轮询/SSE 获取结果

并发模型（单条命令即完整部署）:
    agentcode-api --workers W --concurrency C

    ┌──────────────────────────────────────────────────────┐
    │  uvicorn 主进程                                       │
    │  ├── API worker 进程 0  ── lifespan ── C 个工作线程    │
    │  ├── API worker 进程 1  ── lifespan ── C 个工作线程    │
    │  └── API worker 进程 W-1 ─ lifespan ── C 个工作线程   │
    └──────────────────────────────────────────────────────┘

    所有线程共享同一个 Redis 队列（BLPOP 自然分发）。
    总并发 = W 个 uvicorn worker × C 个工作线程。

    MemoryQueue 仅支持 W=1（单进程），多 worker 需 Redis。

启动:
    agentcode-api --port 9180                         # 1 进程 × 4 线程
    agentcode-api --workers 2 --concurrency 8         # 2 进程 × 8 线程 = 16 并发

端点:
    GET  /health                 — 健康检查（含队列深度、worker 状态）
    POST /v1/query               — 提交任务，返回 task_id
    GET  /v1/task/{task_id}      — 查询任务状态
    GET  /v1/task/{task_id}/stream — SSE 流式结果
"""

from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv

import asyncio
import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import task_store
from .adapter import _default_mcp_path
from .identity import caller_identity, identity_matches
from .models import HistoryMessage, SessionConfig, StreamEvent, StreamFirstEvent
from .pool import ToolManagerPool
from .skillsets.loader import SkillLoader
from .tools.mcp_tools.mcp_loader import build_config_from_mcp_json

logger = logging.getLogger("agentcode.api")

# agentcode 各模块 logger 默认无 handler，INFO 级日志（worker 就绪、MCP 连接等）
# 会整体丢失，docker logs 里只剩 WARNING 以上。服务入口统一配置 root logger；
# uvicorn 只给自己的 logger 挂 handler，不冲突。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# ═══════════════════════════════════════════════════════
# Worker 内嵌 — 从 worker 模块导入核心函数
# ═══════════════════════════════════════════════════════

from .worker import _process_one as _worker_process_one

# ═══════════════════════════════════════════════════════
# Worker 线程管理（模块级，由 lifespan 初始化/销毁）
#
# 并发层级:
#   uvicorn --workers W   →  W 个 OS 进程（多进程）
#   --concurrency C       →  每进程 C 个工作线程
#   --pool-size P         →  每进程 P 个 ToolManager 连接
#
#   总并发 = W × C
#   建议 C == P，避免线程抢连接或连接闲置
#
#   本模块管理的是「单个进程内」的线程和连接池。
#   多进程由 uvicorn 内部 multiprocessing 负责，本模块不感知。
# ═══════════════════════════════════════════════════════

_worker_stop_event: threading.Event | None = None
_worker_threads: list[threading.Thread] = []
_worker_pool: ToolManagerPool | None = None

# 每进程的工作线程数（--concurrency / AGENT_WORKER_CONCURRENCY）
_WORKER_CONCURRENCY = int(config_getenv("AGENT_WORKER_CONCURRENCY", "4"))

# 每进程的 ToolManager 连接池大小（--pool-size / AGENT_WORKER_POOL_SIZE）
# 默认等于 _WORKER_CONCURRENCY，保证每个线程配一个连接，无需排队
_WORKER_POOL_SIZE = _WORKER_CONCURRENCY
_API_WORKERS = 1


def _start_workers() -> None:
    """FastAPI 启动时回调（在本进程内只执行一次）。

    1. 创建 ToolManagerPool × _WORKER_POOL_SIZE 个连接
    2. 预扫描 skills/ 目录加载所有 Skill
    3. 拉起 _WORKER_CONCURRENCY 个工作线程，共享上面的连接池

    线程数 vs 连接数:
      _WORKER_CONCURRENCY == _WORKER_POOL_SIZE  → 理想，一对一
      _WORKER_CONCURRENCY  >  _WORKER_POOL_SIZE  → 多余线程在 borrow() 上排队等
      _WORKER_CONCURRENCY  <  _WORKER_POOL_SIZE  → 多余连接闲置，浪费
    """
    global _worker_stop_event, _worker_threads, _worker_pool

    api_workers = _API_WORKERS
    if api_workers > 1 and not task_store.is_distributed():
        raise RuntimeError("Redis is required when AGENT_API_WORKERS > 1")

    mcp_path = _default_mcp_path()
    logger.info("API worker: 初始化 MCP 路径=%s", mcp_path)

    # ── 创建 ToolManager 连接池 ──
    base_config: SessionConfig = build_config_from_mcp_json(mcp_path)
    _worker_pool = ToolManagerPool(size=_WORKER_POOL_SIZE, config=base_config, include_shell=False)
    _worker_pool.start()
    logger.info("API worker: pool %d/%d 就绪", _worker_pool.stats()["total"], _WORKER_POOL_SIZE)

    # ── 预加载 Skills ──
    skill_loader = SkillLoader()
    discovered_refs = skill_loader.discover_skills()
    logger.info("API worker: 发现 %d 个 skill", len(discovered_refs))

    # ── 启动工作线程 ──
    _worker_stop_event = threading.Event()
    _worker_threads = []
    for i in range(_WORKER_CONCURRENCY):
        t = threading.Thread(
            target=_worker_process_one,
            args=(_worker_pool, skill_loader, discovered_refs, mcp_path, _worker_stop_event, i),
            daemon=False,
            name=f"api-worker-{i}",
        )
        t.start()
        _worker_threads.append(t)

    logger.info("API worker: %d 个工作线程已启动, pool_size=%d",
                _WORKER_CONCURRENCY, _worker_pool.stats()["total"])


def _stop_workers() -> None:
    """FastAPI 关闭时优雅停止所有工作线程并释放连接池。"""
    import time as _time
    global _worker_stop_event, _worker_threads, _worker_pool

    if _worker_stop_event is not None:
        logger.info("API worker: 发送停止信号...")
        _worker_stop_event.set()

    deadline = _time.time() + 60
    for t in _worker_threads:
        remaining = max(1.0, deadline - _time.time())
        t.join(timeout=remaining)
        if t.is_alive():
            logger.warning("API worker: 线程 %s 未能在超时内退出", t.name)

    if _worker_pool is not None:
        _worker_pool.shutdown()
        _worker_pool = None

    _worker_threads.clear()
    _worker_stop_event = None
    logger.info("API worker: 已全部停止")


# ═══════════════════════════════════════════════════════
# 请求 / 响应模型
# ═══════════════════════════════════════════════════════

class QueryRequest(BaseModel):
    """HTTP API 请求体。所有字段均有默认值，只需提供 user_message 即可。"""
    user_message: str = Field(min_length=1, max_length=100_000)
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    mcp_config_path: str | None = None
    skill_ids: list[str] = Field(default_factory=list, max_length=32)
    tool_allowlist: list[str] = Field(default_factory=lambda: ["*"], max_length=256)
    max_turns: int = Field(default=50, ge=1, le=500)
    max_tool_calls: int = Field(default=70, ge=0, le=2000)
    max_time_s: int = Field(default=1200, ge=1, le=86_400)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=40)


_SENSITIVE_REQUEST_KEYS = {"authorization", "headers", "cookie", "cookies"}
_USER_CONTEXT_HEADERS = {
    "authorization",
    "x-user-id",
    "x-user-name",
    "x-tenant-id",
    "x-user-roles",
}


def _request_data(req: QueryRequest) -> dict[str, Any]:
    if req.mcp_config_path:
        raise HTTPException(400, "per-request mcp_config_path is unsupported; HTTP uses built-in tools, CLI extensions use --mcp-config")
    data = req.model_dump(exclude={"mcp_config_path"})
    return {key: value for key, value in data.items() if key.lower() not in _SENSITIVE_REQUEST_KEYS}


def _user_headers(request: Request) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in request.headers.items()
        if key.lower() in _USER_CONTEXT_HEADERS and value
    }


def _enqueue_query(req: QueryRequest, request: Request) -> tuple[str, int]:
    task_id = uuid.uuid4().hex
    request_data = _request_data(req)
    headers = _user_headers(request)
    auth_id = None
    if headers:
        auth_id = task_store.save_auth_context(task_id, headers)
        request_data["auth_context_id"] = auth_id
    try:
        position = task_store.enqueue(
            task_id, request_data, owner=caller_identity(headers)
        )
    except Exception:
        if auth_id:
            task_store.delete_auth_context(auth_id)
        raise
    from .stream_broker import get_broker
    get_broker().create_task(task_id)
    return task_id, position


def _sse_event(event: dict[str, Any]) -> str:
    event_id = event.pop("_event_id", None)
    event_type = event.get("type", "")
    if event_type == "heartbeat":
        return str(StreamFirstEvent(type="heartbeat"))
    event_data = {k: v for k, v in event.items() if k != "type"}
    return str(StreamEvent(type=event_type, data=event_data, event_id=event_id))


def _require_task_access(task_id: str, request: Request) -> dict[str, Any]:
    info = task_store.get(task_id)
    if info is None:
        raise HTTPException(404, f"task not found: {task_id}")
    owner = task_store.get_owner(task_id)
    incoming = caller_identity(_user_headers(request))
    if not identity_matches(owner, incoming):
        raise HTTPException(404, f"task not found: {task_id}")
    return info

class TaskResponse(BaseModel):
    task_id: str
    status: str
    queue_position: int = 0
    created_at: str = ""
    result: Any = None
    error: str | None = None


# ═══════════════════════════════════════════════════════
# FastAPI 应用（带 lifespan）
# ═══════════════════════════════════════════════════════

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """FastAPI lifespan：启动时拉起工作线程，关闭时优雅停止。"""
    _start_workers()
    yield
    _stop_workers()

app = FastAPI(title="agentcode API", version="0.3.0", lifespan=_lifespan)


# ═══════════════════════════════════════════════════════
# 端点
# ═══════════════════════════════════════════════════════

@app.get("/health")
async def health():
    qlen = task_store.queue_length()
    pool_stats = _worker_pool.stats() if _worker_pool else {}
    return {
        "status": "ok",
        "pid": os.getpid(),
        "queue_depth": qlen,
        "concurrency": _WORKER_CONCURRENCY,
        "pool": pool_stats,
    }

@app.post("/v1/query")
async def query(req: QueryRequest, request: Request):
    """提交异步任务：入队后立刻返回 task_id，不等待 Agent 执行完成。

    返回 202 Accepted + task_id，客户端后续轮询 GET /v1/task/{task_id}。
    """
    try:
        task_id, position = _enqueue_query(req, request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"task_store unavailable: {exc}")
    return JSONResponse(
        status_code=202,
        content={"task_id": task_id, "status": "queued", "queue_position": position},
    )

@app.post("/v1/query/wait")
async def query_wait(req: QueryRequest, request: Request, stream: bool = False):
    """提交任务并等待结果。

    默认行为（stream=false）：入队后每 2 秒轮询，完成/失败后一次性返回完整 JSON。
    流式模式（stream=true）：入队后立即返回 SSE 流，实时推送 token、工具调用、状态变更。

    查询参数: ?stream=true  开启 SSE 流式
    """
    try:
        task_id, position = _enqueue_query(req, request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"task_store unavailable: {exc}")

    if stream:
        from .stream_broker import get_broker
        broker = get_broker()

        async def _generator():
            yield str(StreamFirstEvent(type="status", task_id=task_id, status="queued", queue_position=position))
            async for event in broker.subscribe(task_id):
                yield _sse_event(event)

        return StreamingResponse(
            _generator(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    import time as _time
    deadline = _time.time() + req.max_time_s + 120
    while _time.time() < deadline:
        info = task_store.get(task_id)
        if info is None:
            raise HTTPException(500, "task lost from store")
        if info["status"] in ("completed", "failed", "cancelled"):
            return JSONResponse(status_code=200, content=info)
        await asyncio.sleep(2)

    raise HTTPException(504, f"task timed out after {req.max_time_s + 120}s")

@app.get("/v1/task/{task_id}")
async def get_task(task_id: str, request: Request):
    """查询任务当前状态和结果。"""
    return _require_task_access(task_id, request)


@app.post("/v1/task/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    """Request cooperative cancellation for a queued or running task."""
    _require_task_access(task_id, request)
    info = task_store.cancel(task_id)
    if info is None:
        raise HTTPException(404, f"task not found: {task_id}")
    from .stream_broker import get_broker
    get_broker().publish(task_id, {"type": "status", "status": info["status"]})
    if info["status"] == "cancelled":
        get_broker().publish(task_id, {"type": "done"})
    return info

@app.get("/v1/task/{task_id}/stream")
async def stream_task(task_id: str, request: Request, cursor: str | None = None):
    """SSE 流式订阅任务事件 —— LLM token、工具调用、状态变更实时推送。

    可用 ``cursor`` 查询参数或 ``Last-Event-ID`` 从头之后续传。
    """
    info = _require_task_access(task_id, request)
    last_event_id = cursor or request.headers.get("last-event-id")

    async def _generator():
        if not last_event_id:
            yield str(StreamFirstEvent(type="status", **info))
            if info["status"] in ("completed", "failed", "cancelled"):
                return

        from .stream_broker import get_broker
        broker = get_broker()
        async for event in broker.subscribe(task_id, last_event_id or "0"):
            yield _sse_event(event)

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ═══════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════

def main():
    global _WORKER_CONCURRENCY, _WORKER_POOL_SIZE, _API_WORKERS
    import argparse
    parser = argparse.ArgumentParser(
        description="agentcode HTTP API server (内嵌 worker，支持多进程)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
并发说明:
  总并发 = --workers × --concurrency

  单进程模式 (--workers 1):
    所有工作线程在同一个进程内，可使用 MemoryQueue 或 Redis。

  多进程模式 (--workers >= 2):
    利用 uvicorn 多 worker 机制，每个 worker 进程独立运行 lifespan 拉起工作线程。
    必须使用 Redis 作为任务队列（MemoryQueue 不支持跨进程）。

示例:
  agentcode-api                                              # 1 进程 × 4 线程
  agentcode-api --concurrency 8                              # 1 进程 × 8 线程
  agentcode-api --workers 2 --concurrency 8                  # 2 进程 × 8 线程 = 16 并发
  agentcode-api --workers 4 --concurrency 16                 # 4 进程 × 16 线程 = 64 并发
""",
    )
    parser.add_argument("--host", default="0.0.0.0", help="绑定地址 (默认: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9180, help="端口 (默认: 9180)")
    parser.add_argument("--workers", type=int, default=0,
                        help="独立开发启动进程数，标准部署固定为1")
    parser.add_argument("--concurrency", type=int, default=0,
                        help="每进程工作线程数 (默认: env AGENT_WORKER_CONCURRENCY or 1)")
    parser.add_argument("--pool-size", type=int, default=0,
                        help="ToolManager 连接池大小 (默认: =concurrency)")
    args = parser.parse_args()

    # 将 CLI 参数写入环境变量，以便 uvicorn fork 后子进程继承
    if args.concurrency:
        from qasystem_config.configuration import prepare_dev_agent_startup
        prepare_dev_agent_startup(args.concurrency)
        _WORKER_CONCURRENCY = args.concurrency
    if args.pool_size:
        _WORKER_POOL_SIZE = args.pool_size
    elif args.concurrency:
        _WORKER_POOL_SIZE = args.concurrency

    api_workers = args.workers or 1
    _API_WORKERS = api_workers

    import uvicorn

    if api_workers > 1:
        # 多 worker 模式：必须用字符串引用，uvicorn 在每个子进程中重新导入 app
        logger.info("启动 API: host=%s port=%s api_workers=%d concurrency=%s pool_size=%s",
                    args.host, args.port, api_workers,
                    config_getenv("AGENT_WORKER_CONCURRENCY", "1"),
                    _WORKER_POOL_SIZE)
        uvicorn.run(
            "agentcode.api:app",
            host=args.host,
            port=args.port,
            workers=api_workers,
            log_level="info",
        )
    else:
        # 单 worker 模式：直接传 app 实例
        logger.info("启动 API: host=%s port=%s api_workers=1 concurrency=%s pool_size=%s",
                    args.host, args.port,
                    config_getenv("AGENT_WORKER_CONCURRENCY", "1"),
                    _WORKER_POOL_SIZE)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
