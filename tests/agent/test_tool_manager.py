from __future__ import annotations

import pytest
import os

from agentcode.models import BudgetConfig, SessionConfig, ToolPolicy
from agentcode.tools.mcp_tools.tool_manager import ToolManager


def _make_config(**kwargs) -> SessionConfig:
    return SessionConfig(
        session_id="test",
        trace_id="test",
        mcp_servers=[],
        tool_policy=ToolPolicy(allow=["base__*"]),
        budget=BudgetConfig(),
        **kwargs,
    )


class TestToolManagerBaseTools:
    def test_base_tool_registered(self) -> None:
        tm = ToolManager(_make_config())
        tools = tm.list_tools()
        base_tools = [t for t in tools if t["name"].startswith("base__")]
        assert len(base_tools) >= 1, "base__execute_command should be registered"
        assert base_tools[0]["name"] == "base__execute_command"

    def test_base_tool_has_schema(self) -> None:
        tm = ToolManager(_make_config())
        tools = tm.list_tools()
        base_tool = next(t for t in tools if t["name"] == "base__execute_command")
        assert "description" in base_tool
        assert "input_schema" in base_tool
        assert base_tool["input_schema"]["type"] == "object"
        assert "command" in base_tool["input_schema"]["required"]

    def test_call_base_tool(self) -> None:
        tm = ToolManager(_make_config())
        command = "cmd /d /c echo hello_from_manager" if os.name == "nt" else "echo hello_from_manager"
        result = tm.call_tool("base__execute_command", {"command": command}, timeout_s=30, retries=0)
        assert result["ok"] is True
        assert "hello_from_manager" in result["content"]

    def test_call_base_tool_blocked_command(self) -> None:
        tm = ToolManager(_make_config())
        result = tm.call_tool("base__execute_command", {"command": "shutdown now"}, timeout_s=30, retries=0)
        assert result["ok"] is False
        assert "安全策略拒绝" in result.get("error", "")

    def test_call_unknown_tool(self) -> None:
        tm = ToolManager(_make_config())
        result = tm.call_tool("unknown__tool", {}, timeout_s=30, retries=0)
        assert result["ok"] is False
        assert result["error_code"] is not None

    def test_is_tool_allowed_with_base_pattern(self) -> None:
        assert ToolManager.is_tool_allowed("base__execute_command", ["base__*"]) is True
        assert ToolManager.is_tool_allowed("base__execute_command", ["other__*"]) is False
        assert ToolManager.is_tool_allowed("base__execute_command", []) is True

    def test_http_pool_registers_path_display_without_shell(self) -> None:
        tm = ToolManager(_make_config(), include_shell=False)
        names = [t["name"] for t in tm.list_tools()]
        assert "problem-resolve-path-display" in names
        assert "base__execute_command" not in names

    def test_path_display_echoes_solution_path(self) -> None:
        tm = ToolManager(_make_config(), include_shell=False)
        result = tm.call_tool(
            "problem-resolve-path-display",
            {
                "question": "指标趋势",
                "solution_path": ["步骤1 查询", "步骤2 汇总"],
                "reasoning_summary": "按方案执行",
            },
            timeout_s=5,
            retries=0,
        )
        assert result["ok"] is True
        import json
        payload = json.loads(result["content"])
        assert payload["question"] == "指标趋势"
        assert payload["solution_path"] == ["步骤1 查询", "步骤2 汇总"]

    def test_apply_user_headers_updates_and_clears_http_client(self) -> None:
        from agentcode.tools.mcp_tools.mcp import MCPToolWrapper

        class DummyClient:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {"Content-Type": "application/json"}

        wrapper = MCPToolWrapper()
        client = DummyClient()
        wrapper._http_clients = [client]
        wrapper.apply_user_headers({"authorization": "Bearer abc", "x-user-id": "u1"})
        assert client.headers["Authorization"] == "Bearer abc"
        assert client.headers["X-User-Id"] == "u1"
        wrapper.apply_user_headers({})
        assert "Authorization" not in client.headers
        assert client.headers["Content-Type"] == "application/json"
