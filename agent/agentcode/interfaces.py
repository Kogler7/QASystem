from __future__ import annotations

from typing import Any, Protocol


class LLMBackend(Protocol):
    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], cancel_token: Any = None
    ) -> dict[str, Any]:
        """Return one assistant message in tool-capable structure."""


class ToolAdapter(Protocol):
    def list_tools(self) -> list[dict[str, Any]]:
        """Return MCP-like tool descriptors."""

    def call_tool(self, name: str, arguments: dict[str, Any], timeout_s: int) -> dict[str, Any]:
        """Execute one tool and return MCP-like payload."""
