import argparse
from qasystem_config.startup_contract import INTERNAL_MCP_BIND, INTERNAL_MCP_PORT

from .runtime import mcp, MCP_V2
from .domains.data import sql as _sql
from .domains.knowledge import extraction as _extraction
from .domains.graph import locator as _graph_locator
from .domains.graph import retrieval as _graph_retrieval
from .domains.visualization import charts as _charts
from .domains import execution as _execution


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Standalone MCP development server; Backend embeds HTTP MCP automatically")
    parser.add_argument("--transport", choices=("streamable-http", "stdio", "sse"), default="streamable-http")
    parser.add_argument("--host", default=INTERNAL_MCP_BIND)
    parser.add_argument("--port", type=int, default=INTERNAL_MCP_PORT)
    args = parser.parse_args(argv)
    transport = args.transport
    binding = {"host": args.host, "port": args.port}
    if not MCP_V2 and transport != "stdio":
        mcp.settings.host, mcp.settings.port = args.host, args.port
    mcp.run(transport=transport, **(binding if MCP_V2 and transport != "stdio" else {}))


if __name__ == "__main__":
    main()
