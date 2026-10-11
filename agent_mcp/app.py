"""Agent 专属 MCP 服务入口（FastAPI 组装）。

结构照搬 backend/app/main.py 的成熟模式：
- FastMCP 实例在 runtime.py，工具在 graph.py（import 触发注册）
- streamable_http_app 挂载在 "/"，MCP 端点即 /mcp
- 中间件捕获用户上下文请求头 → ContextVar → KnowledgeMapClient 随查询透传，
  前端接口按权限过滤（与旧 MCP 同一条权限链路）
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from external_services.application import capture_user_headers, reset_user_headers

import agent_mcp.graph  # noqa: F401  导入即注册工具
import agent_mcp.spaces  # noqa: F401  导入即注册工具
import agent_mcp.data  # noqa: F401  导入即注册工具
import agent_mcp.charts  # noqa: F401  导入即注册工具
from agent_mcp.runtime import mcp

mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="QASystem Agent MCP", lifespan=lifespan)


@app.middleware("http")
async def capture_application_user_context(request: Request, call_next):
    token = capture_user_headers(dict(request.headers))
    try:
        return await call_next(request)
    finally:
        reset_user_headers(token)


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


app.mount("/", mcp_app)
