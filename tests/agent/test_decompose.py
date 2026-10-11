from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager

from agentcode.cancellation import AgentCancelled, CancellationToken
from agentcode.decompose import (
    SubProblem,
    _child_share,
    _SubtaskTraceSink,
    level_sub_problems,
    parse_pre_call_output,
    publish_tool_results,
    run_orchestrated,
)
from agentcode.delivery import answer_v1_delivery
from agentcode.models import BudgetConfig, SessionConfig


# ═══════════════════════════════════════════════════════════════════════
# Fakes
# ═══════════════════════════════════════════════════════════════════════


class FakeToolManager:
    def list_tools(self):
        return []

    @staticmethod
    def is_tool_allowed(name, allow):
        return not allow or "*" in allow

    def call_many(self, calls, **kwargs):
        return []

    @contextmanager
    def bind_user_headers(self, headers):
        yield


class FakePool:
    """可配置额外可用数的 ToolManager 池，并跟踪峰值并发借用数。"""

    def __init__(self, extra: int = 0) -> None:
        self._extra = extra
        self.in_flight = 0
        self.max_in_flight = 0

    @contextmanager
    def borrow(self, timeout: float = 30.0):
        if self._extra <= 0:
            raise RuntimeError("ToolManagerPool: borrow 超时")
        self._extra -= 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            yield FakeToolManager()
        finally:
            self.in_flight -= 1
            self._extra += 1


class FakeBroker:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, task_id, event):
        self.events.append({"_task": task_id, **dict(event)})

    def of_type(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == event_type]

    def subtask_statuses(self) -> list[tuple[str, str]]:
        return [
            (e["subtask"], e["status"])
            for e in self.of_type("subtask_status")
        ]


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls: list[list[dict]] = []

    def complete(self, messages, tools, cancel_token=None):
        self.calls.append(list(messages))
        return next(self.responses)


class NeverLLM:
    """直连哨兵：被调用即失败。"""

    def complete(self, messages, tools, cancel_token=None):
        raise AssertionError("direct path should not be used")


class LLMFactory:
    def __init__(self, by_label: dict) -> None:
        self.by_label = by_label
        self.requested: list[str] = []

    def __call__(self, label: str):
        self.requested.append(label)
        return self.by_label[label]


class BarrierLLM:
    """两个实例都到达 barrier 才放行——验证真并行。"""

    def __init__(self, barrier: threading.Barrier, conclusion: str) -> None:
        self._barrier = barrier
        self._conclusion = conclusion

    def complete(self, messages, tools, cancel_token=None):
        self._barrier.wait(timeout=10)
        return _assistant(_answer_text(conclusion=self._conclusion))


class WaitCancelLLM:
    """阻塞直到取消令牌生效——验证父取消传播。"""

    def complete(self, messages, tools, cancel_token=None):
        while True:
            if cancel_token is not None and cancel_token.is_cancelled():
                raise AgentCancelled("cancelled")
            time.sleep(0.02)


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _answer(**overrides) -> dict:
    payload = {
        "conclusion": "结论",
        "evidence": [],
        "assumptions": [],
        "gaps": [],
        "confidence": "medium",
        "confidence_reason": "理由",
        "charts": [],
        "solution_path": [],
    }
    payload.update(overrides)
    return payload


def _answer_text(**overrides) -> str:
    return json.dumps(_answer(**overrides), ensure_ascii=False)


def _plan_text(subs: list[dict], constraints: str = "") -> str:
    return json.dumps(
        {"decompose": True, "constraints": constraints, "sub_problems": subs},
        ensure_ascii=False,
    )


def _config() -> SessionConfig:
    return SessionConfig(
        session_id="s",
        trace_id="t",
        budget=BudgetConfig(max_turns=10, max_tool_calls=10, max_time_s=1200),
        delivery=answer_v1_delivery(),
        skills=[],
    )


def _two_subs() -> list[dict]:
    return [
        {"id": "sub-1", "question": "A的故障率", "search_terms": ["A"], "depends_on": []},
        {"id": "sub-2", "question": "B的故障率", "search_terms": ["B"], "depends_on": []},
    ]


def _run(
    factory: LLMFactory,
    *,
    user_message: str = "对比A和B",
    direct_llm=None,
    pool: FakePool | None = None,
    config: SessionConfig | None = None,
    cancel_token: CancellationToken | None = None,
    history: list[dict] | None = None,
):
    broker = FakeBroker()
    result = run_orchestrated(
        task_id="t1",
        broker=broker,
        pool=pool if pool is not None else FakePool(extra=2),
        parent_tm=FakeToolManager(),
        user_headers={},
        direct_llm=direct_llm if direct_llm is not None else NeverLLM(),
        llm_factory=factory,
        config=config if config is not None else _config(),
        user_message=user_message,
        history=history,
        cancel_token=cancel_token if cancel_token is not None else CancellationToken(),
        skill_loader=None,
    )
    return result, broker


def _parallel_factory(**child_conclusions) -> LLMFactory:
    """双子问题 + 合并的常规成功编排。"""
    by_label = {
        "decompose": ScriptedLLM([_assistant(_plan_text(_two_subs(), constraints="2024年"))]),
        "sub-1": ScriptedLLM([_assistant(_answer_text(conclusion=child_conclusions.get("sub-1", "A结论")))]),
        "sub-2": ScriptedLLM([_assistant(_answer_text(conclusion=child_conclusions.get("sub-2", "B结论")))]),
        "merge": ScriptedLLM([_assistant(_answer_text(conclusion="合并结论", confidence="high"))]),
    }
    return LLMFactory(by_label)


# ═══════════════════════════════════════════════════════════════════════
# 解析 / 分层（纯函数）
# ═══════════════════════════════════════════════════════════════════════


def test_parse_pre_call_valid_json() -> None:
    plan = parse_pre_call_output(_plan_text(_two_subs(), constraints="2024年"), 4)
    assert plan is not None
    assert plan.constraints == "2024年"
    assert [s.id for s in plan.sub_problems] == ["sub-1", "sub-2"]
    assert plan.sub_problems[0].search_terms == ["A"]


def test_parse_pre_call_fenced_json() -> None:
    text = "```json\n" + _plan_text(_two_subs()) + "\n```"
    assert parse_pre_call_output(text, 4) is not None


def test_parse_pre_call_garbage_returns_none() -> None:
    assert parse_pre_call_output("我不会分解这个问题", 4) is None


def test_parse_pre_call_decompose_false_returns_none() -> None:
    assert parse_pre_call_output('{"decompose": false}', 4) is None


def test_parse_pre_call_duplicate_ids_returns_none() -> None:
    raw = json.dumps({"decompose": True, "sub_problems": [
        {"id": "sub-1", "question": "a"}, {"id": "sub-1", "question": "b"},
    ]})
    assert parse_pre_call_output(raw, 4) is None


def test_parse_truncate_dropped_dependency_returns_none() -> None:
    # 截断后 sub-1 依赖的 sub-3 被丢弃 → 宁可不分解
    raw = json.dumps({"decompose": True, "sub_problems": [
        {"id": "sub-1", "question": "a", "depends_on": ["sub-3"]},
        {"id": "sub-2", "question": "b"},
        {"id": "sub-3", "question": "c"},
    ]})
    assert parse_pre_call_output(raw, 2) is None


def test_parse_truncate_without_dropped_dependency_keeps_prefix() -> None:
    raw = json.dumps({"decompose": True, "sub_problems": [
        {"id": "sub-1", "question": "a"},
        {"id": "sub-2", "question": "b"},
        {"id": "sub-3", "question": "c"},
    ]})
    plan = parse_pre_call_output(raw, 2)
    assert plan is not None
    assert [s.id for s in plan.sub_problems] == ["sub-1", "sub-2"]


def test_level_sub_problems_diamond_dag() -> None:
    subs = [
        SubProblem("a", "qa", [], []),
        SubProblem("b", "qb", [], ["a"]),
        SubProblem("c", "qc", [], ["a"]),
        SubProblem("d", "qd", [], ["b", "c"]),
    ]
    levels = level_sub_problems(subs)
    assert [[s.id for s in level] for level in levels] == [["a"], ["b", "c"], ["d"]]


def test_level_sub_problems_cycle_returns_none() -> None:
    subs = [
        SubProblem("a", "qa", [], ["b"]),
        SubProblem("b", "qb", [], ["a"]),
    ]
    assert level_sub_problems(subs) is None


def test_level_sub_problems_unknown_dep_returns_none() -> None:
    subs = [SubProblem("a", "qa", [], ["ghost"])]
    assert level_sub_problems(subs) is None


def test_child_share_arithmetic() -> None:
    assert _child_share(1000.0, 120, 2) == 440
    assert _child_share(10.0, 120, 1) == 5   # 下限
    assert _child_share(None, 60, 1) == 5    # 无 deadline 也有下限


# ═══════════════════════════════════════════════════════════════════════
# 门控：走直连的情形
# ═══════════════════════════════════════════════════════════════════════


def test_gate_single_subproblem_runs_direct() -> None:
    factory = LLMFactory({
        "decompose": ScriptedLLM([_assistant(_plan_text([
            {"id": "sub-1", "question": "唯一问题", "depends_on": []},
        ]))]),
    })
    direct = ScriptedLLM([_assistant(_answer_text(conclusion="直连结果"))])
    result, broker = _run(factory, direct_llm=direct)
    assert result.status == "ok"
    assert result.payload["conclusion"] == "直连结果"
    assert broker.of_type("decompose") == []
    assert factory.requested == ["decompose"]


def test_gate_serial_chain_runs_direct() -> None:
    subs = [
        {"id": "sub-1", "question": "先求", "depends_on": []},
        {"id": "sub-2", "question": "后比", "depends_on": ["sub-1"]},
    ]
    factory = LLMFactory({"decompose": ScriptedLLM([_assistant(_plan_text(subs))])})
    direct = ScriptedLLM([_assistant(_answer_text(conclusion="直连结果"))])
    result, broker = _run(factory, direct_llm=direct)
    assert result.status == "ok"
    assert result.payload["conclusion"] == "直连结果"
    assert broker.of_type("decompose") == []


def test_env_disabled_never_calls_pre_call(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_DECOMPOSITION_ENABLED", "0")
    factory = LLMFactory({})  # 任何标签被请求都会 KeyError
    direct = ScriptedLLM([_assistant(_answer_text(conclusion="直连结果"))])
    result, broker = _run(factory, direct_llm=direct)
    assert result.status == "ok"
    assert factory.requested == []
    assert broker.events == []


def test_malformed_pre_call_falls_back_to_direct() -> None:
    factory = LLMFactory({"decompose": ScriptedLLM([_assistant("不是 JSON")] )})
    direct = ScriptedLLM([_assistant(_answer_text(conclusion="直连结果"))])
    result, broker = _run(factory, direct_llm=direct)
    assert result.status == "ok"
    assert direct.calls[0][-1]["content"][0]["text"] == "对比A和B"  # 直连收到原始问题
    assert broker.of_type("decompose") == []


# ═══════════════════════════════════════════════════════════════════════
# 编排：并行 / 依赖 / 失败 / 取消
# ═══════════════════════════════════════════════════════════════════════


def test_two_children_overlap_via_barrier() -> None:
    barrier = threading.Barrier(2)
    by_label = {
        "decompose": ScriptedLLM([_assistant(_plan_text(_two_subs()))]),
        "sub-1": BarrierLLM(barrier, "A结论"),
        "sub-2": BarrierLLM(barrier, "B结论"),
        "merge": ScriptedLLM([_assistant(_answer_text(conclusion="合并结论"))]),
    }
    pool = FakePool(extra=2)
    result, broker = _run(LLMFactory(by_label), pool=pool)
    # 串行执行时 barrier 超时 → 子问题失败 → 不可能 ok
    assert result.status == "ok"
    assert pool.max_in_flight >= 1  # 额外 TM 确实被借出
    assert [e["status"] for e in broker.of_type("subtask_status") if e["subtask"] == "sub-1"] == ["running", "ok"]


def test_dependent_child_receives_prerequisite_payload() -> None:
    # 两个独立子问题 + 一个依赖 sub-1 的子问题（纯串行链会被门控送直连）
    subs = [
        {"id": "sub-1", "question": "C区域故障率", "search_terms": ["C区域"], "depends_on": []},
        {"id": "sub-2", "question": "全国平均故障率", "search_terms": ["全国"], "depends_on": []},
        {"id": "sub-3", "question": "与全国平均比较", "search_terms": ["比较"], "depends_on": ["sub-1"]},
    ]
    sub3 = ScriptedLLM([_assistant(_answer_text(conclusion="比较结论"))])
    by_label = {
        "decompose": ScriptedLLM([_assistant(_plan_text(subs, constraints="2024年"))]),
        "sub-1": ScriptedLLM([_assistant(_answer_text(conclusion="C区域结果X"))]),
        "sub-2": ScriptedLLM([_assistant(_answer_text(conclusion="全国结果Y"))]),
        "sub-3": sub3,
        "merge": ScriptedLLM([_assistant(_answer_text(conclusion="合并结论"))]),
    }
    result, broker = _run(LLMFactory(by_label))
    assert result.status == "ok"
    child_message = sub3.calls[0][-1]["content"][0]["text"]
    assert "前置子问题结论" in child_message
    assert "## sub-1" in child_message
    assert "C区域结果X" in child_message          # 前置 payload 进了子消息
    assert "[全局约束] 2024年" in child_message     # constraints 继承
    assert "[检索关键词] 比较" in child_message
    # 顺序：sub-1 完成在 sub-3 开跑之前（依赖层串行）
    statuses = broker.subtask_statuses()
    assert statuses.index(("sub-1", "ok")) < statuses.index(("sub-3", "running"))


def test_child_failure_becomes_gap_in_merge() -> None:
    class BrokenLLM:
        def complete(self, messages, tools, cancel_token=None):
            raise RuntimeError("boom")

    by_label = {
        "decompose": ScriptedLLM([_assistant(_plan_text(_two_subs()))]),
        "sub-1": BrokenLLM(),
        "sub-2": ScriptedLLM([_assistant(_answer_text(conclusion="B结论"))]),
        "merge": ScriptedLLM([_assistant(_answer_text(conclusion="合并结论"))]),
    }
    result, broker = _run(LLMFactory(by_label))
    assert result.status == "ok"
    gaps = result.payload["gaps"]
    assert any("sub-1" in g and "求解失败" in g and "boom" in g for g in gaps)


def test_dependent_of_failed_child_is_skipped_as_gap() -> None:
    class BrokenLLM:
        def complete(self, messages, tools, cancel_token=None):
            raise RuntimeError("boom")

    subs = [
        {"id": "sub-1", "question": "先求", "depends_on": []},
        {"id": "sub-2", "question": "独立", "depends_on": []},
        {"id": "sub-3", "question": "依赖一", "depends_on": ["sub-1"]},
    ]
    by_label = {
        "decompose": ScriptedLLM([_assistant(_plan_text(subs))]),
        "sub-1": BrokenLLM(),
        "sub-2": ScriptedLLM([_assistant(_answer_text(conclusion="B结论"))]),
        "sub-3": ScriptedLLM([]),  # 应被跳过，不会调用
        "merge": ScriptedLLM([_assistant(_answer_text(conclusion="合并结论"))]),
    }
    factory = LLMFactory(by_label)
    result, broker = _run(factory)
    assert result.status == "ok"
    assert "sub-3" not in factory.requested
    assert ("sub-3", "skipped") in broker.subtask_statuses()
    assert any("sub-3" in g and "跳过" in g for g in result.payload["gaps"])


def test_all_children_failed_returns_failed() -> None:
    class BrokenLLM:
        def complete(self, messages, tools, cancel_token=None):
            raise RuntimeError("boom")

    by_label = {
        "decompose": ScriptedLLM([_assistant(_plan_text(_two_subs()))]),
        "sub-1": BrokenLLM(),
        "sub-2": BrokenLLM(),
    }
    factory = LLMFactory(by_label)
    result, _ = _run(factory)
    assert result.status == "failed"
    assert "所有子问题求解失败" in result.error.message
    assert "merge" not in factory.requested


def test_cancel_propagates_to_children() -> None:
    by_label = {
        "decompose": ScriptedLLM([_assistant(_plan_text(_two_subs()))]),
        "sub-1": WaitCancelLLM(),
        "sub-2": WaitCancelLLM(),
    }
    token = CancellationToken()
    timer = threading.Timer(0.3, token.cancel)
    timer.start()
    try:
        result, _ = _run(LLMFactory(by_label), cancel_token=token)
    finally:
        timer.join()
    assert result.status == "cancelled"


def test_pool_width_capping_runs_serially() -> None:
    subs = [
        {"id": "sub-1", "question": "a", "depends_on": []},
        {"id": "sub-2", "question": "b", "depends_on": []},
        {"id": "sub-3", "question": "c", "depends_on": []},
    ]
    by_label = {
        "decompose": ScriptedLLM([_assistant(_plan_text(subs))]),
        "sub-1": ScriptedLLM([_assistant(_answer_text(conclusion="A"))]),
        "sub-2": ScriptedLLM([_assistant(_answer_text(conclusion="B"))]),
        "sub-3": ScriptedLLM([_assistant(_answer_text(conclusion="C"))]),
        "merge": ScriptedLLM([_assistant(_answer_text(conclusion="合并"))]),
    }
    factory = LLMFactory(by_label)
    result, broker = _run(factory, pool=FakePool(extra=0))  # 池空 → 全串行
    assert result.status == "ok"
    assert sorted(factory.requested) == ["decompose", "merge", "sub-1", "sub-2", "sub-3"]
    completed = [s for s in broker.subtask_statuses() if s[1] == "ok"]
    assert len(completed) == 3


# ═══════════════════════════════════════════════════════════════════════
# 合并
# ═══════════════════════════════════════════════════════════════════════


def test_merge_schema_retry_then_success() -> None:
    merge_llm = ScriptedLLM([
        _assistant("第一次不合法"),
        _assistant(_answer_text(conclusion="重试后的合并结论", confidence="high")),
    ])
    by_label = {
        "decompose": ScriptedLLM([_assistant(_plan_text(_two_subs()))]),
        "sub-1": ScriptedLLM([_assistant(_answer_text(conclusion="A结论"))]),
        "sub-2": ScriptedLLM([_assistant(_answer_text(conclusion="B结论"))]),
        "merge": merge_llm,
    }
    result, _ = _run(LLMFactory(by_label))
    assert result.status == "ok"
    assert result.payload["conclusion"] == "重试后的合并结论"
    # 第二次调用时带上了纠正消息
    assert len(merge_llm.calls) == 2
    assert any("Delivery schema not satisfied" in m["content"][0]["text"] for m in merge_llm.calls[1])


def test_merge_double_failure_uses_conservative_payload() -> None:
    by_label = {
        "decompose": ScriptedLLM([_assistant(_plan_text(_two_subs()))]),
        "sub-1": ScriptedLLM([_assistant(_answer_text(conclusion="A结论"))]),
        "sub-2": ScriptedLLM([_assistant(_answer_text(conclusion="B结论"))]),
        "merge": ScriptedLLM([_assistant("垃圾一"), _assistant("垃圾二")]),
    }
    result, _ = _run(LLMFactory(by_label))
    assert result.status == "ok"
    assert result.payload["confidence"] == "low"
    assert "A结论" in result.payload["conclusion"]
    assert "B结论" in result.payload["conclusion"]
    assert any("两次未通过 answer-v1 校验" in g for g in result.payload["gaps"])
    assert result.payload["solution_path"] == []


# ═══════════════════════════════════════════════════════════════════════
# 聚合与事件
# ═══════════════════════════════════════════════════════════════════════


def test_spent_aggregates_precall_children_and_merge() -> None:
    result, _ = _run(_parallel_factory())
    subtasks = result.spent["subtasks"]
    assert set(subtasks) == {"sub-1", "sub-2"}
    assert all(v["status"] == "ok" and v["turns"] == 1 for v in subtasks.values())
    assert result.spent["turns"] == 1 + 1 + 1 + 1  # 预调用 + 两个子会话 + 合并
    assert result.spent["tool_calls"] == 0


def test_decompose_event_and_deadline_published() -> None:
    token = CancellationToken()
    result, broker = _run(_parallel_factory(), cancel_token=token)
    assert result.status == "ok"
    events = broker.of_type("decompose")
    assert len(events) == 1
    assert events[0]["status"] == "planned"
    assert events[0]["levels"] == [["sub-1", "sub-2"]]
    # 全局截止已设到父 token（≤ max_time_s=1200）
    assert token.remaining_seconds() is not None
    assert 0 < token.remaining_seconds() <= 1200


def test_subtask_trace_sink_tags_events() -> None:
    class RecordingSink:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def write(self, event):
            self.events.append(event)
            return "ref"

    inner = RecordingSink()
    sink = _SubtaskTraceSink(inner, "sub-1")  # type: ignore[arg-type]
    assert sink.write({"turn": 1}) == "ref"
    assert inner.events == [{"turn": 1, "subtask": "sub-1"}]


# ═══════════════════════════════════════════════════════════════════════
# worker 集成：打标事件
# ═══════════════════════════════════════════════════════════════════════


def test_streaming_llm_publishes_subtask_field() -> None:
    from agentcode.worker import _StreamingWorkerLLM

    llm = _StreamingWorkerLLM(broker=None)
    llm.set_task_id("t1")
    llm.set_subtask("sub-1")
    broker = FakeBroker()
    llm._broker = broker
    llm._publish("token", {"text": "x"})
    assert broker.events == [{"_task": "t1", "type": "token", "text": "x", "subtask": "sub-1"}]
    llm.set_subtask(None)
    llm._publish("token", {"text": "y"})
    assert broker.events[-1] == {"_task": "t1", "type": "token", "text": "y"}
    llm.close()


def test_make_tagged_llm_sets_ids() -> None:
    from agentcode.worker import _make_tagged_llm

    broker = FakeBroker()
    llm = _make_tagged_llm(broker, "t1", "sub-2")
    assert llm._task_id == "t1"
    assert llm._subtask == "sub-2"
    llm._publish("reasoning", {"text": "r"})
    assert broker.events[-1]["subtask"] == "sub-2"
    llm.close()


def test_publish_tool_results_event_shape() -> None:
    broker = FakeBroker()
    results = [
        {"name": "tool_a", "is_error": False, "content": "ok", "error": None},
        {"name": "tool_b", "is_error": True, "content": "", "error": "bad"},
    ]
    publish_tool_results(broker, "t1", None, results, 12)
    assert broker.events == [
        {"_task": "t1", "type": "tool_result", "name": "tool_a", "is_error": False, "content": "ok", "error": None},
        {"_task": "t1", "type": "tool_result", "name": "tool_b", "is_error": True, "content": None, "error": "bad"},
    ]
    broker.events.clear()
    publish_tool_results(broker, "t1", "sub-1", results[:1], 5)
    assert broker.events[0]["subtask"] == "sub-1"
