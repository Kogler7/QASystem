"""Compatibility wrapper for the refactored MCP server location."""

from agentcode.mcp_servers.time.server import TOOLS, main, serve

__all__ = ["TOOLS", "serve", "main"]

if __name__ == "__main__":
    main()
