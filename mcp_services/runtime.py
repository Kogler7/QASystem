from docker.configuration import getenv as config_getenv
import os
from importlib.metadata import version

MCP_V2 = int(version("mcp").split(".")[0]) >= 2
if MCP_V2:
    from mcp.server.mcpserver import MCPServer
else:
    from mcp.server.fastmcp import FastMCP as MCPServer


mcp = MCPServer(
    "qasystem-tools",
    instructions="QASystem unified knowledge, graph retrieval, data, and visualization tools",
    **({} if MCP_V2 else {
        "host": config_getenv("MCP_HOST", "0.0.0.0"),
        "port": int(config_getenv("MCP_PORT", "8890")),
    }),
)


def streamable_http_app():
    # Preserve the deployed host binding/security behavior across SDK versions.
    return mcp.streamable_http_app(
        **({"host": config_getenv("MCP_HOST", "0.0.0.0")} if MCP_V2 else {})
    )
