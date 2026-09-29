from __future__ import annotations

import time

from agentcode.cancellation import CancellationToken
from agentcode.models import BudgetConfig, SessionConfig, ToolPolicy
from agentcode.runtime import AgentRuntime
from agentcode.trace import NoopTraceSink


class FakeToolManager:
    def __init__(self) -> None:
        self.calls = []

    def list_tools(self):
        return [{"name": "fake__lookup", "description": "lookup", "input_schema": {"type": "object"}}]

    @staticmethod
    def is_tool_allowed(name, allow):
        return not allow or name in allow or "*" in allow

    def call_many(self, calls, **kwargs):
        self.calls.extend(calls)
        return [{
            "type": "tool_result", "tool_use_id": calls[0]["id"],
            "name": calls[0]["name"], "is_error": False,
            "content": "42", "error_code": None, "error": None,
        }]


class ScriptedLLM:
    def __init__(self, messages):
        self.responses = iter(messages)

    def complete(self, messages, tools, cancel_token=None):
        return next(self.responses)


def _config() -> SessionConfig:
    return SessionConfig(
        session_id="s", trace_id="t",
        tool_policy=ToolPolicy(allow=["*"]),
        budget=BudgetConfig(max_turns=5, max_tool_calls=5, max_time_s=30),
    )


def test_runtime_completes_after_tool_roundtrip() -> None:
    llm = ScriptedLLM([
        {"role": "assistant", "content": [{
            "type": "tool_use", "id": "call-1", "name": "fake__lookup", "input": {"q": "x"},
        }]},
        {"role": "assistant", "content": [{"type": "text", "text": "answer 42"}]},
    ])
    tools = FakeToolManager()
    runtime = AgentRuntime(llm, tools, NoopTraceSink())
    result = runtime.run_session(_config(), "question")
    assert result.status == "ok"
    assert result.payload == {"text": "answer 42"}
    assert result.spent == {"turns": 2, "tool_calls": 1}
    assert len(tools.calls) == 1


def test_runtime_honours_pre_cancelled_token() -> None:
    token = CancellationToken()
    token.cancel()
    runtime = AgentRuntime(ScriptedLLM([]), FakeToolManager(), NoopTraceSink())
    result = runtime.run_session(_config(), "question", cancel_token=token)
    assert result.status == "cancelled"
    assert result.error is not None
    assert result.error.code.value == "CANCELLED"


def test_runtime_reports_deadline_as_budget_exceeded() -> None:
    token = CancellationToken(deadline=time.monotonic() - 1)
    runtime = AgentRuntime(ScriptedLLM([]), FakeToolManager(), NoopTraceSink())
    result = runtime.run_session(_config(), "question", cancel_token=token)
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code.value == "BUDGET_EXCEEDED"


def test_runtime_maps_llm_exception_to_structured_error() -> None:
    class BrokenLLM:
        def complete(self, messages, tools, cancel_token=None):
            raise OSError("upstream unavailable")

    runtime = AgentRuntime(BrokenLLM(), FakeToolManager(), NoopTraceSink())
    result = runtime.run_session(_config(), "question")
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code.value == "LLM_ERROR"
