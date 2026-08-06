import os

from .runtime import mcp
from .domains.data import sql as _sql
from .domains.knowledge import extraction as _extraction
from .domains.graph import locator as _graph_locator
from .domains.graph import retrieval as _graph_retrieval
from .domains.visualization import charts as _charts
from .domains import execution as _execution


def main() -> None:
    mcp.run(transport=os.getenv("MCP_TRANSPORT", "streamable-http"))


if __name__ == "__main__":
    main()
