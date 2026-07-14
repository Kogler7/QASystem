import os

from mcp.server.fastmcp import FastMCP


mcp = FastMCP(
    "qasystem-tools",
    instructions="QASystem unified knowledge, graph retrieval, data, and visualization tools",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "8890")),
)
