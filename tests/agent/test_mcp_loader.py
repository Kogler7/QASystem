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


def test_default_connection_is_generated_from_topology(monkeypatch):
    from agentcode import adapter
    from agentcode.tools.mcp_tools import mcp_loader
    monkeypatch.setenv("AGENT_MCP_CONFIG", "missing.json")
    monkeypatch.setenv("AGENT_MCP_URL", "http://unapproved/mcp")
    monkeypatch.setattr(mcp_loader, "internal_agent_mcp_url", lambda: "http://agent-mcp:9181/mcp")
    assert adapter._default_mcp_path() is None
    server = load_mcp_servers()[0]
    assert server.name == "agent-tools"
    assert server.url == "http://agent-mcp:9181/mcp"


def test_explicit_multiple_external_servers_remain_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MCP_URL", "http://unapproved/mcp")
    path = tmp_path / "external.json"
    path.write_text(json.dumps({"mcpServers": {
        "first": {"transport": "http", "url": "https://first/mcp"},
        "second": {"transport": "stdio", "command": "python", "args": ["-m", "local_tools"]},
    }}), encoding="utf-8")
    servers = load_mcp_servers(path)
    assert [server.name for server in servers] == ["first", "second"]
    assert servers[0].url == "https://first/mcp"
    assert servers[1].command == "python"


def test_http_worker_binds_answer_v1_delivery(tmp_path, monkeypatch) -> None:
    from agentcode.delivery import ANSWER_V1_ID, ANSWER_V1_SCHEMA
    from agentcode.worker import _build_config

    monkeypatch.delenv("AGENT_MCP_URL", raising=False)
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"remote": {
        "transport": "streamable_http", "url": "http://example/mcp",
    }}}), encoding="utf-8")
    config, _loader = _build_config({"user_message": "hello", "session_id": "s", "trace_id": "t"}, str(path))
    assert config.delivery is not None
    assert config.delivery.output_schema_id == ANSWER_V1_ID
    assert config.delivery.json_schema == ANSWER_V1_SCHEMA
