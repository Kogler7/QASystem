"""启动入口：python -m agent_mcp（本地与容器通用）。"""
from docker.startup_contract import INTERNAL_MCP_BIND, INTERNAL_AGENT_MCP_PORT

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "agent_mcp.app:app",
        host=INTERNAL_MCP_BIND,
        port=INTERNAL_AGENT_MCP_PORT,
    )
