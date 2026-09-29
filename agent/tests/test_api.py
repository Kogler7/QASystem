from __future__ import annotations

import json
import time
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from agentcode.api import app, QueryRequest, TaskResponse
from agentcode.adapter import _messages_to_openai


client = TestClient(app)


class TestHealth:
    def test_health_ok(self) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "queue_depth" in data


class TestMessageConversion:
    def test_simple_user_message(self) -> None:
        msgs = [{"role": "user", "content": "hi"}]
        result = _messages_to_openai(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_assistant_with_text_and_tool_calls(self) -> None:
        msgs = [{"role": "assistant", "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "call_1", "name": "search", "input": {"q": "test"}},
        ]}]
        result = _messages_to_openai(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert "tool_calls" in result[0]

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
        assert req.max_turns == 50
        assert req.max_tool_calls == 70

    def test_custom_params(self) -> None:
        req = QueryRequest(user_message="hello", session_id="abc", max_turns=10)
        assert req.session_id == "abc"
        assert req.max_turns == 10


class TestAsyncQuery:
    """异步任务模式：提交 → 返回 task_id + 状态码 202。"""
    def test_submit_returns_task_id(self) -> None:
        resp = client.post("/v1/query", json={"user_message": "hello"})
        assert resp.status_code == 202
        data = resp.json()
        assert "task_id" in data
        assert data["status"] == "queued"

    def test_submit_empty_message_is_rejected(self) -> None:
        resp = client.post("/v1/query", json={"user_message": ""})
        assert resp.status_code == 422

    def test_per_request_mcp_switch_is_rejected(self) -> None:
        resp = client.post("/v1/query", json={
            "user_message": "hello", "mcp_config_path": "another.json",
        })
        assert resp.status_code == 400


class TestTaskStatus:
    """任务状态查询。"""
    def test_nonexistent_task_returns_404(self) -> None:
        resp = client.get("/v1/task/nonexistent-id")
        assert resp.status_code == 404

    def test_task_lifecycle(self) -> None:
        # 入队
        resp = client.post("/v1/query", json={
            "user_message": "查询故障率",
            "session_id": "test-session",
            "max_turns": 5,
        })
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        # 查询状态（应为 queued）
        resp = client.get(f"/v1/task/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == task_id
        assert data["status"] == "queued"


class TestStreamEndpoint:
    def test_stream_nonexistent_task(self) -> None:
        with client.stream("GET", "/v1/task/nonexistent/stream") as resp:
            assert resp.status_code == 404
            resp.read()
            assert "task not found" in resp.text

    def test_cancel_queued_task(self) -> None:
        submitted = client.post("/v1/query", json={"user_message": "cancel me"})
        task_id = submitted.json()["task_id"]
        resp = client.post(f"/v1/task/{task_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"


class TestPool:
    def test_import_pool(self) -> None:
        """验证 pool 模块可正常导入。"""
        from agentcode.pool import ToolManagerPool
        assert ToolManagerPool is not None

    def test_multi_process_mode_requires_redis(self, monkeypatch) -> None:
        from agentcode import api, task_store
        monkeypatch.setattr(task_store, "_QUEUE", task_store._MemoryQueue())
        monkeypatch.setenv("AGENT_API_WORKERS", "2")
        with pytest.raises(RuntimeError, match="Redis is required"):
            api._start_workers()
