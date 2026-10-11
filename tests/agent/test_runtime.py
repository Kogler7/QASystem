from __future__ import annotations

import json
import time

from agentcode.cancellation import CancellationToken
from agentcode.delivery import answer_v1_delivery
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


class CapturingLLM:
    def __init__(self):
        self.messages = None

    def complete(self, messages, tools, cancel_token=None):
        self.messages = list(messages)
        return {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}


def test_runtime_includes_caller_history_before_current_message() -> None:
    llm = CapturingLLM()
    runtime = AgentRuntime(llm, FakeToolManager(), NoopTraceSink())
    runtime.run_session(
        _config(),
        "follow up",
        history=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ans"},
        ],
    )
    roles = [message["role"] for message in llm.messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert llm.messages[-1]["content"][0]["text"] == "follow up"
    assert llm.messages[1]["content"][0]["text"] == "first"


def test_system_prompt_does_not_inline_skill_bodies() -> None:
    from agentcode.prompt import build_domain_knowledge
    from agentcode.skillsets.loader import SkillLoader

    loader = SkillLoader()
    refs = loader.discover_skills()
    text = build_domain_knowledge(loader, refs)
    assert "graph-qa" in text
    assert "Skill bodies are not inlined" in text
    assert "第 4 步 C 分支：图外工具的通用判断标准" not in text


def _valid_answer(**overrides) -> dict:
    payload = {
        "conclusion": "故障率为 2.3%",
        "evidence": [{"name": "故障率", "id": "e1", "detail": "近 7 日 2.3%"}],
        "assumptions": [],
        "gaps": [],
        "confidence": "high",
        "confidence_reason": "知识空间✓ 实体✓ 方案类✓ 工具✓",
        "charts": [],
        "solution_path": ["步骤1 查询"],
    }
    payload.update(overrides)
    return payload


def _qa_config(**budget_kwargs) -> SessionConfig:
    config = _config()
    config.delivery = answer_v1_delivery()
    if budget_kwargs:
        config.budget = BudgetConfig(**{"max_turns": 5, "max_tool_calls": 5, "max_time_s": 30, **budget_kwargs})
    return config


def test_runtime_accepts_answer_v1_json() -> None:
    body = json.dumps(_valid_answer(), ensure_ascii=False)
    llm = ScriptedLLM([
        {"role": "assistant", "content": [{"type": "text", "text": body}]},
    ])
    result = AgentRuntime(llm, FakeToolManager(), NoopTraceSink()).run_session(_qa_config(), "故障率")
    assert result.status == "ok"
    assert result.payload["conclusion"] == "故障率为 2.3%"
    assert result.payload["confidence"] == "high"


def test_runtime_normalizes_fenced_json_and_zh_confidence() -> None:
    body = "```json\n" + json.dumps(_valid_answer(confidence="高"), ensure_ascii=False) + "\n```"
    llm = ScriptedLLM([
        {"role": "assistant", "content": [{"type": "text", "text": body}]},
    ])
    result = AgentRuntime(llm, FakeToolManager(), NoopTraceSink()).run_session(_qa_config(), "故障率")
    assert result.status == "ok"
    assert result.payload["confidence"] == "high"


def test_runtime_retries_invalid_delivery_then_accepts_json() -> None:
    class RecordingLLM:
        def __init__(self) -> None:
            self.seen: list = []
            self.responses = [
                {"role": "assistant", "content": [{"type": "text", "text": "【结论】故障率 2.3%"}]},
                {"role": "assistant", "content": [{"type": "text", "text": json.dumps(_valid_answer(), ensure_ascii=False)}]},
            ]

        def complete(self, messages, tools, cancel_token=None):
            self.seen.append(list(messages))
            return self.responses.pop(0)

    llm = RecordingLLM()
    result = AgentRuntime(llm, FakeToolManager(), NoopTraceSink()).run_session(_qa_config(), "故障率")
    assert result.status == "ok"
    retry_text = llm.seen[1][-1]["content"][0]["text"]
    assert "answer-v1" in retry_text
    assert "Delivery schema not satisfied" in retry_text


def test_runtime_fails_when_delivery_never_satisfied() -> None:
    llm = ScriptedLLM([
        {"role": "assistant", "content": [{"type": "text", "text": "【结论】随便写"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "还是散文"}]},
    ])
    result = AgentRuntime(llm, FakeToolManager(), NoopTraceSink()).run_session(
        _qa_config(max_turns=2),
        "故障率",
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code.value == "DELIVERY_NOT_SATISFIED"
    assert result.payload is None


def test_system_prompt_includes_answer_v1_schema_when_delivery_set() -> None:
    from agentcode.prompt import build_system_prompt

    with_schema = build_system_prompt(FakeToolManager(), None, [], delivery=answer_v1_delivery())
    without = build_system_prompt(FakeToolManager(), None, [], delivery=None)
    assert "# Delivery Contract" in with_schema
    assert '"conclusion"' in with_schema
    assert "high" in with_schema
    assert "# Delivery Contract" not in without
