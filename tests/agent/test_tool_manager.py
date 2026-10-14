from __future__ import annotations

import pytest
import os
import sys

from agentcode.errors import ErrorCode
from agentcode.models import BudgetConfig, SessionConfig, ToolPolicy
from agentcode.tools.base_tools import shell_tool
from agentcode.tools.mcp_tools import tool_manager
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


@pytest.fixture
def command_io_manager(monkeypatch) -> ToolManager:
    # This fixture tests dispatch and command I/O, not resource enforcement.
    # Keep defaults on Linux; Darwin's VM map cannot fit the default RLIMIT_AS,
    # and platforms without resource need an explicit test-only opt-out.
    limits = {}
    if not shell_tool._HAS_RESOURCE:
        limits = dict(mem_limit_mb=0, cpu_limit_s=0, file_size_limit_mb=0, nproc_limit=0)
    elif sys.platform == "darwin":
        limits = dict(mem_limit_mb=0)
    monkeypatch.setattr(tool_manager, "ShellTool", lambda: shell_tool.ShellTool(**limits))
    return ToolManager(_make_config())


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

    def test_call_base_tool(self, command_io_manager) -> None:
        tm = command_io_manager
        command = "cmd /d /c echo hello_from_manager" if os.name == "nt" else "echo hello_from_manager"
        result = tm.call_tool("base__execute_command", {"command": command}, timeout_s=30, retries=0)
        assert result["ok"] is True
        assert "hello_from_manager" in result["content"]

    @pytest.mark.parametrize("dispatch", ["single", "batch"])
    def test_default_resource_rejection_propagates_without_execution(self, tmp_path, monkeypatch, dispatch) -> None:
        marker = tmp_path / "must_not_exist"
        script = tmp_path / "side_effect.py"
        script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")

        if shell_tool._HAS_RESOURCE:
            def reject_limit(*args):
                raise ValueError("injected setrlimit rejection")

            monkeypatch.setattr(shell_tool._resource_module, "setrlimit", reject_limit)
            expected_error = "Error: 无法应用子进程资源限制"
        else:
            expected_error = "Error: 当前平台不支持所请求的资源限制"

        tm = ToolManager(_make_config())
        assert tm._shell_tool._mem_limit_mb == 512
        arguments = {"command": f'"{sys.executable}" "{script}"'}
        if dispatch == "single":
            result = tm.call_tool("base__execute_command", arguments, timeout_s=30, retries=0)
            assert result["ok"] is False
        else:
            result = tm.call_many(
                [{"id": "resource-rejection", "name": "base__execute_command", "input": arguments}],
                timeout_s=30,
                retries=0,
            )[0]
            assert result["is_error"] is True
        assert result["error_code"] == ErrorCode.MCP_PROTOCOL_ERROR.value
        assert result["error"].startswith(expected_error)
        assert "RLIMIT_AS(MB)=512" in result["error"]
        assert "命令未执行" in result["error"]
        assert not result.get("content")
        assert not marker.exists()

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

    def test_http_pool_has_no_local_shell_or_removed_display_tool(self) -> None:
        tm = ToolManager(_make_config(), include_shell=False)
        assert tm.list_tools() == []
        result = tm.call_tool("problem-resolve-path-display", {}, timeout_s=5, retries=0)
        assert result["ok"] is False
        assert result["error_code"] is not None

    def test_identity_rebind_reuse_and_end_task_isolation(self, monkeypatch) -> None:
        from agentcode.tools.mcp_tools.mcp import MCPToolWrapper

        wrapper = MCPToolWrapper()
        rebound = []

        def rebind(headers):
            rebound.append(dict(headers))
            wrapper._dirty = False

        monkeypatch.setattr(wrapper, "_rebind", rebind)
        handler = wrapper._make_handler("test", "query")
        assert "identity not bound" in handler()
        wrapper.bind_identity({"authorization": "Bearer test-a", "x-user-id": "u1", "ignored": "x"})
        assert rebound == [{"Authorization": "Bearer test-a", "X-User-Id": "u1"}]
        wrapper.end_task()
        assert "identity not bound" in handler()
        wrapper.bind_identity({"Authorization": "Bearer test-a", "X-User-Id": "u1"})
        assert len(rebound) == 1
        wrapper.bind_identity({"Authorization": "Bearer test-b"})
        assert rebound[-1] == {"Authorization": "Bearer test-b"}
        wrapper.bind_identity({})
        assert rebound[-1] == {}

    def test_failed_identity_rebind_blocks_old_session(self, monkeypatch) -> None:
        from agentcode.tools.mcp_tools.mcp import MCPToolWrapper

        wrapper = MCPToolWrapper()
        def fail(headers):
            raise RuntimeError("connection failed")
        monkeypatch.setattr(wrapper, "_rebind", fail)
        with pytest.raises(RuntimeError, match="connection failed"):
            wrapper.bind_identity({"Authorization": "Bearer test-b"})
        handler = wrapper._make_handler("test", "query")
        assert "being rebound" in handler()
