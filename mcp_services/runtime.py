from docker.startup_contract import INTERNAL_MCP_BIND, INTERNAL_MCP_PORT
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
        "host": INTERNAL_MCP_BIND,
        "port": INTERNAL_MCP_PORT,
    }),
)


def streamable_http_app():
    # Preserve the deployed host binding/security behavior across SDK versions.
    return mcp.streamable_http_app(
        **({"host": INTERNAL_MCP_BIND} if MCP_V2 else {})
    )
