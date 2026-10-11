"""启动入口：python -m agent_mcp（本地与容器通用）。"""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "agent_mcp.app:app",
        host=os.getenv("AGENT_MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("AGENT_MCP_PORT", "9181")),
    )
