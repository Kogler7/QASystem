"""启动入口：python -m agent_mcp（本地与容器通用）。"""
from docker.configuration import getenv as config_getenv

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "agent_mcp.app:app",
        host=config_getenv("AGENT_MCP_HOST", "0.0.0.0"),
        port=int(config_getenv("AGENT_MCP_PORT", "9181")),
    )
