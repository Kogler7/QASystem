from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from agentcode.api import app, QueryRequest, QueryResponse, _messages_to_openai


client = TestClient(app)


class TestHealth:
    def test_health_ok(self) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestMessageConversion:
    def test_simple_user_message(self) -> None:
        msgs = [{"role": "user", "content": "hi"}]
        result = _messages_to_openai(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "hi"

    def test_assistant_with_text_and_tool_calls(self) -> None:
        msgs = [{"role": "assistant", "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "call_1", "name": "search", "input": {"q": "test"}},
        ]}]
        result = _messages_to_openai(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "Let me check."
        assert "tool_calls" in result[0]
        assert result[0]["tool_calls"][0]["function"]["name"] == "search"

    def test_tool_result_conversion(self) -> None:
        msgs = [{"role": "tool", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "result data", "is_error": False},
        ]}]
        result = _messages_to_openai(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "tool"
        assert "result data" in result[0]["content"]

    def test_developer_role_maps_to_system(self) -> None:
        msgs = [{"role": "developer", "content": "instructions"}]
        result = _messages_to_openai(msgs)
        assert result[0]["role"] == "system"

    def test_empty_text_skipped(self) -> None:
        msgs = [{"role": "user", "content": "   "}]
        result = _messages_to_openai(msgs)
        assert len(result) == 0


class TestQueryRequestModel:
    def test_defaults(self) -> None:
        req = QueryRequest(user_message="hello")
        assert req.user_message == "hello"
        assert req.session_id
        assert req.trace_id
        assert req.tool_allowlist == ["*"]
        assert req.max_turns == 50
        assert req.max_tool_calls == 70

    def test_custom_params(self) -> None:
        req = QueryRequest(
            user_message="hello",
            session_id="abc",
            max_turns=10,
            skill_ids=["smart_query"],
        )
        assert req.session_id == "abc"
        assert req.max_turns == 10
        assert req.skill_ids == ["smart_query"]


class TestQueryEndpoint:
    @patch("agentcode.api.build_config_from_mcp_json")
    @patch("agentcode.api.ToolManager")
    @patch("agentcode.api._BlockingLLM")
    def test_query_mcp_not_found(self, mock_llm, mock_tm, mock_cfg) -> None:
        resp = client.post("/v1/query", json={
            "user_message": "hello",
            "mcp_config_path": "/nonexistent/mcp.json",
        })
        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"]


class TestStreamEndpoint:
    @patch("agentcode.api.build_config_from_mcp_json")
    @patch("agentcode.api.ToolManager")
    def test_stream_mcp_not_found(self, mock_tm, mock_cfg) -> None:
        resp = client.post("/v1/query/stream", json={
            "user_message": "hello",
            "mcp_config_path": "/nonexistent/mcp.json",
        })
        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"]


class TestStreamEvents:
    """验证 SSE 事件格式"""
    def test_event_format(self) -> None:
        line = 'event: token\ndata: {"type": "token", "data": "hello"}\n\n'
        assert line.startswith("event: ")
        assert "\ndata: " in line
        parts = line.strip().split("\n")
        event_part = parts[0]
        data_part = parts[1]
        assert event_part.startswith("event: ")
        assert data_part.startswith("data: ")
        parsed = json.loads(data_part[6:])
        assert parsed["type"] == "token"
        assert parsed["data"] == "hello"
