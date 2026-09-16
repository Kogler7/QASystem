from __future__ import annotations

import json

import pytest

from agentcode.tools.mcp_tools.mcp_loader import load_mcp_servers


@pytest.fixture(autouse=True)
def _clear_mcp_url_override(monkeypatch) -> None:
    # .env 或宿主机环境里的 AGENT_MCP_URL 会覆盖单 server 配置的 url，测试必须与环境无关
    monkeypatch.delenv("AGENT_MCP_URL", raising=False)


def test_stdio_config_preserves_command_args_and_env(tmp_path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"local": {
        "transport": "stdio", "command": "python", "args": ["-m", "server"],
        "env": {"A": "B"},
    }}}), encoding="utf-8")
    server = load_mcp_servers(path)[0]
    assert server.url is None
    assert server.command == "python"
    assert server.args == ["-m", "server"]
    assert server.env == {"A": "B"}


def test_http_config_preserves_headers(tmp_path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"remote": {
        "transport": "streamable_http", "url": "http://example/mcp",
        "headers": {"Authorization": "Bearer test"},
    }}}), encoding="utf-8")
    server = load_mcp_servers(path)[0]
    assert server.headers == {"Authorization": "Bearer test"}
