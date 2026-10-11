import os

from .runtime import mcp, MCP_V2
from .domains.data import sql as _sql
from .domains.knowledge import extraction as _extraction
from .domains.graph import locator as _graph_locator
from .domains.graph import retrieval as _graph_retrieval
from .domains.visualization import charts as _charts
from .domains import execution as _execution


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    binding = {"host": os.getenv("MCP_HOST", "0.0.0.0"),
               "port": int(os.getenv("MCP_PORT", "8890"))}
    mcp.run(transport=transport, **(binding if MCP_V2 and transport != "stdio" else {}))


if __name__ == "__main__":
    main()
