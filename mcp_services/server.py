import os

from .runtime import mcp
from .domains.data import sql as _sql
from .domains.knowledge import extraction as _extraction
from .domains.reasoning import answer as _answer
from .domains.reasoning import intent as _intent
from .domains.reasoning import locator as _locator
from .domains.reasoning import plan as _plan
from .domains.visualization import charts as _charts


def main() -> None:
    mcp.run(transport=os.getenv("MCP_TRANSPORT", "streamable-http"))


if __name__ == "__main__":
    main()
