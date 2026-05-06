from __future__ import annotations

import json
from typing import Any

from agentcode.errors import ErrorCode
from agentcode.mcp import McpClient
from agentcode.models import DeliveryConfig, HitlConfig, SessionConfig
from agentcode.runtime import AgentRuntime
from agentcode.trace import JsonlTraceSink


class MockLLM:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = responses
        self.calls = 0

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], cancel_token=None):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


class MockToolAdapter:
    def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": "graph_search", "description": "mock", "input_schema": {"type": "object"}}]

    def call_tool(self, name: str, arguments: dict[str, Any], timeout_s: int) -> dict[str, Any]:
        return {"result": {"tool": name, "arguments": arguments, "answer": "station-A"}}


def make_config() -> SessionConfig:
    return SessionConfig(
        session_id="s1",
        trace_id="t1",
        mcp_servers=[],
        tool_policy={"allow": ["graph__*"]},
        budget={
            "max_turns": 5,
            "max_tool_calls": 5,
            "max_time_s": 30,
            "max_retries_per_tool": 0,
            "repeat_action_threshold": 3,
        },
        tool_execution="sequential",
    )


def test_delivery_reprompt_until_schema_satisfied(tmp_path):
    llm = MockLLM(
        responses=[
            {"role": "assistant", "content": [{"type": "text", "text": "not-json"}]},
            {"role": "assistant", "content": [{"type": "text", "text": '{"answer": "ok"}'}]},
        ]
    )
    config = make_config()
    config.delivery = DeliveryConfig(
        output_schema_id="answer-v1",
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )
    runtime = AgentRuntime(
        llm_backend=llm,
        mcp_client=McpClient(config=config, adapters={"graph": MockToolAdapter()}),
        trace_sink=JsonlTraceSink(str(tmp_path / "trace.jsonl")),
    )
    result = runtime.run_session(config, "q")
    assert result.status == "ok"
    assert result.payload["answer"] == "ok"
    assert llm.calls == 2
    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").strip().split("\n")
    e0, e1 = json.loads(lines[0]), json.loads(lines[1])
    assert e0["outcome"] == "delivery_invalid"
    assert e0["delivery_error"]
    assert e1["outcome"] == "completed"
    assert e1["delivery_error"] is None


def test_hitl_pause_and_resume(tmp_path):
    llm = MockLLM(
        responses=[
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "u1", "name": "graph__graph_search", "input": {"q": "x"}}],
            },
            {"role": "assistant", "content": [{"type": "text", "text": '{"answer":"done"}'}]},
        ]
    )
    config = make_config()
    config.hitl = HitlConfig(tool_globs=["graph__*"], gate=["before_tool"], require_explicit_approve=True)
    config.delivery = DeliveryConfig(
        output_schema_id="answer-v1",
        json_schema={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
    )
    runtime = AgentRuntime(
        llm_backend=llm,
        mcp_client=McpClient(config=config, adapters={"graph": MockToolAdapter()}),
        trace_sink=JsonlTraceSink(str(tmp_path / "trace.jsonl")),
    )
    pending = runtime.run_session(config, "q")
    assert pending.status == "needs_human"
    assert pending.checkpoint_id

    resumed = runtime.resume(config, checkpoint_id=pending.checkpoint_id, decision="approve")
    assert resumed.status == "ok"
    assert resumed.payload["answer"] == "done"


def test_extract_payload_parses_json_inside_markdown_fence():
    raw = (
        "```json\n"
        '{\n  "answer": "hello",\n  "style": "informative",\n  "confidence": 1.0\n}\n'
        "```"
    )
    msg = {"role": "assistant", "content": [{"type": "text", "text": raw}]}
    payload = AgentRuntime._extract_payload(msg)
    assert payload == {"answer": "hello", "style": "informative", "confidence": 1.0}


def test_delivery_accepts_fenced_json_without_extra_llm_round(tmp_path):
    llm = MockLLM(
        responses=[
            {"role": "assistant", "content": [{"type": "text", "text": "not-json"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": '```json\n{"answer": "ok", "style": "x", "confidence": 1}\n```',
                    }
                ],
            },
        ]
    )
    config = make_config()
    config.delivery = DeliveryConfig(
        output_schema_id="answer-v1",
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}, "style": {"type": "string"}, "confidence": {"type": "number"}},
            "required": ["answer", "style", "confidence"],
        },
    )
    runtime = AgentRuntime(
        llm_backend=llm,
        mcp_client=McpClient(config=config, adapters={"graph": MockToolAdapter()}),
        trace_sink=JsonlTraceSink(str(tmp_path / "trace.jsonl")),
    )
    result = runtime.run_session(config, "q")
    assert result.status == "ok"
    assert result.payload["answer"] == "ok"
    assert llm.calls == 2


def test_tool_policy_violation_returns_failed(tmp_path):
    llm = MockLLM(
        responses=[
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "u1", "name": "graph__graph_search", "input": {"q": "x"}}],
            }
        ]
    )
    config = make_config()
    config.tool_policy.allow = ["other__*"]
    runtime = AgentRuntime(
        llm_backend=llm,
        mcp_client=McpClient(config=config, adapters={"graph": MockToolAdapter()}),
        trace_sink=JsonlTraceSink(str(tmp_path / "trace.jsonl")),
    )
    result = runtime.run_session(config, "q")
    assert result.status == "failed"
    assert result.error
    assert result.error.code == ErrorCode.CONSTRAINT_VIOLATION
