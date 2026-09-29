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
