"""问题分解与并行子问题求解编排。

worker 拿到任务后先走 run_orchestrated 入口：
- 预调用（一次小 LLM 调用）判断问题是否值得分解为子问题 DAG；
- 独立子问题并行求解（同任务内线程，各占一个 ToolManager），
  依赖型子问题在拿到前置结论后求解；
- 最后合并为单个 answer-v1 负载。

门控不通过 / 预调用失败时走与原先逐字节等价的直连路径。
不改动 task_store、队列、API 端点与交付契约；子会话内部工具调用
保持 sequential（skill 铁律禁止并行工具）。
"""

from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable

from jsonschema import ValidationError, validate

from .cancellation import AgentCancelled, CancellationToken
from .delivery import ANSWER_V1_ID, normalize_answer_payload, schema_prompt_text
from .errors import ErrorCode, RuntimeErrorInfo
from .models import FinalResult, SessionConfig
from .runtime import AgentRuntime
from .skillsets.loader import SkillLoader
from .trace import TaskEventTraceSink
from .tools.mcp_tools.tool_manager import ToolManager


logger = logging.getLogger("agentcode.decompose")

# 额外 ToolManager 的借用超时：池不足时降并行度，不永久阻塞。
_BORROW_TIMEOUT_S = 5.0


# ═══════════════════════════════════════════════════════════════════════
# 配置与数据结构
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class DecompSettings:
    enabled: bool = True
    max_subproblems: int = 4
    max_parallel: int = 3

    @classmethod
    def from_env(cls) -> "DecompSettings":
        def _flag(name: str, default: str = "1") -> bool:
            return config_getenv(name, default).strip().lower() not in {"0", "false", "off", "no", ""}

        def _clamped(name: str, default: int, lo: int, hi: int) -> int:
            try:
                return min(max(int(config_getenv(name, str(default))), lo), hi)
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=_flag("AGENT_DECOMPOSITION_ENABLED"),
            max_subproblems=_clamped("AGENT_DECOMPOSE_MAX_SUBPROBLEMS", 4, 2, 8),
            max_parallel=_clamped("AGENT_DECOMPOSE_MAX_PARALLEL", 3, 1, 8),
        )


@dataclass
class SubProblem:
    id: str
    question: str
    search_terms: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


@dataclass
class DecompositionPlan:
    constraints: str = ""
    sub_problems: list[SubProblem] = field(default_factory=list)


@dataclass
class _ChildEntry:
    """一个子问题的求解结果（result=None 且 skipped_reason 非空表示被级联跳过）。"""

    sub: SubProblem
    result: FinalResult | None = None
    skipped_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.status == "ok"


# ═══════════════════════════════════════════════════════════════════════
# 预调用：提示词 / 解析 / DAG 分层（纯函数）
# ═══════════════════════════════════════════════════════════════════════


def _build_pre_call_prompt(user_message: str, history: list[dict] | None, max_subproblems: int) -> str:
    parts = [
        "你是问题分解器，只输出一个 JSON 对象，不输出其它文字。",
        "判断下面的问题是否值得分解为多个可并行求解的子问题。",
        "",
        "规则：",
        "- 仅当问题包含 >=2 个相互独立、可分别检索的信息需求时才分解；",
        '- 单一事实、单跳查询、闲聊、追问 → 输出 {"decompose": false}；',
        "- 每个子问题给出自己的 search_terms（知识图谱检索关键词，名词性实体，用户原词）；",
        "- depends_on 只能引用其它子问题的 id；无依赖用空数组；",
        '- 不要产生"综合/汇总"类子问题，汇总由系统完成；',
        "- constraints 提取所有子问题都要继承的约束（时间范围、对象、口径等），没有则为空字符串；",
        f"- 最多 {max_subproblems} 个子问题。",
    ]
    recent = [m for m in (history or []) if isinstance(m, dict)][-6:]
    if recent:
        parts.append("")
        parts.append("[历史对话（仅用于消歧）]")
        for m in recent:
            parts.append(f"{m.get('role', 'user')}: {m.get('content', '')}")
    parts.append("")
    parts.append("[用户问题]")
    parts.append(user_message)
    parts.append("")
    parts.append("输出（可包在 ```json 围栏里）：")
    parts.append('{"decompose": true, "constraints": "...",')
    parts.append(' "sub_problems": [{"id": "sub-1", "question": "...", "search_terms": ["..."], "depends_on": []}]}')
    return "\n".join(parts)


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def parse_pre_call_output(text: str, max_subproblems: int) -> DecompositionPlan | None:
    """解析预调用输出；任何结构违规返回 None（= 不分解，走直连）。"""
    data = AgentRuntime._parse_json_from_assistant_text(text or "")
    if not isinstance(data, dict) or not data.get("decompose"):
        return None
    raw_subs = data.get("sub_problems")
    if not isinstance(raw_subs, list) or not raw_subs:
        return None

    subs: list[SubProblem] = []
    seen_ids: set[str] = set()
    for raw in raw_subs:
        if not isinstance(raw, dict):
            return None
        sub_id = str(raw.get("id") or "").strip()
        question = str(raw.get("question") or "").strip()
        if not sub_id or not question or sub_id in seen_ids:
            return None
        seen_ids.add(sub_id)
        subs.append(SubProblem(
            id=sub_id,
            question=question,
            search_terms=_as_str_list(raw.get("search_terms")),
            depends_on=_as_str_list(raw.get("depends_on")),
        ))

    # 截断到上限；被丢弃的子问题如果被保留项依赖则宁可不分解——
    # 丢掉真实依赖会让后续子问题拿到错误的前置输入。
    dropped_ids = {s.id for s in subs[max_subproblems:]}
    subs = subs[:max_subproblems]
    if any(dropped_ids.intersection(s.depends_on) for s in subs):
        return None
    return DecompositionPlan(constraints=str(data.get("constraints") or "").strip(), sub_problems=subs)


def level_sub_problems(subs: list[SubProblem]) -> list[list[SubProblem]] | None:
    """Kahn 分层：level k 收纳依赖全部落在更底层次的子问题（最大化分层）。

    依赖引用未知 id 或成环 → None。
    """
    ids = {s.id for s in subs}
    if any(dep not in ids for s in subs for dep in s.depends_on):
        return None
    by_id = {s.id: s for s in subs}
    remaining = {s.id: set(s.depends_on) for s in subs}
    levels: list[list[SubProblem]] = []
    while remaining:
        ready = sorted(sid for sid, deps in remaining.items() if not deps)
        if not ready:
            return None  # 环
        levels.append([by_id[sid] for sid in ready])
        for sid in ready:
            del remaining[sid]
        for deps in remaining.values():
            deps.difference_update(ready)
    return levels


# ═══════════════════════════════════════════════════════════════════════
# 事件与子会话辅助
# ═══════════════════════════════════════════════════════════════════════


def publish_tool_results(
    broker: Any, task_id: str, subtask: str | None, results: list[dict], elapsed_ms: int
) -> None:
    """把工具结果发布到任务事件流；subtask=None 时事件形状与直连路径一致。"""
    for r in results:
        name = r.get("name", "")
        content = r.get("content", "") if not r.get("is_error") else None
        event = {
            "type": "tool_result",
            "name": name,
            "is_error": r.get("is_error", False),
            "content": content,
            "error": r.get("error") if r.get("is_error") else None,
        }
        if subtask is not None:
            event["subtask"] = subtask
        broker.publish(task_id, event)


class _SubtaskTraceSink:
    """给每轮 trace 事件打上 subtask 标签后写入父任务事件流。"""

    def __init__(self, inner: TaskEventTraceSink, subtask: str) -> None:
        self._inner = inner
        self._subtask = subtask

    def write(self, event: dict[str, Any]) -> str:
        return self._inner.write({**event, "subtask": self._subtask})


def _close_llm(llm: Any) -> None:
    closer = getattr(llm, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # 关闭失败不影响主流程
            logger.debug("llm close failed", exc_info=True)


def _build_child_message(sub: SubProblem, plan: DecompositionPlan, prereq_payloads: dict[str, Any]) -> str:
    lines = [
        "请求解以下子问题，并按 answer-v1 契约输出。",
        "",
        f"[子问题] {sub.question}",
    ]
    if sub.search_terms:
        lines.append(f"[检索关键词] {', '.join(sub.search_terms)}")
    lines.append(f"[全局约束] {plan.constraints or '无'}")
    if prereq_payloads:
        by_id = {s.id: s for s in plan.sub_problems}
        lines.append("")
        lines.append("[前置子问题结论]（来自其它子问题的已验证答案，可直接引用，不要重复检索）")
        for dep_id, payload in prereq_payloads.items():
            dep_question = by_id[dep_id].question if dep_id in by_id else ""
            lines.append(f"## {dep_id}: {dep_question}")
            lines.append("```json")
            lines.append(json.dumps(payload, ensure_ascii=False))
            lines.append("```")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# 合并
# ═══════════════════════════════════════════════════════════════════════


def _merge_arrays(payloads: list[dict]) -> dict[str, list]:
    """程序化合并子答案的数组字段（去重拼接），LLM 合并时原样保留。"""
    merged: dict[str, list] = {"evidence": [], "charts": [], "assumptions": [], "gaps": []}
    seen_ev: set[tuple] = set()
    seen_ch: set[str] = set()
    seen_txt: set[str] = set()
    for payload in payloads:
        for ev in payload.get("evidence") or []:
            key = (str(ev.get("name", "")), str(ev.get("id", ""))) if isinstance(ev, dict) else str(ev)
            if key in seen_ev:
                continue
            seen_ev.add(key)
            merged["evidence"].append(ev)
        for ch in payload.get("charts") or []:
            key = str(ch.get("path", "")) if isinstance(ch, dict) else str(ch)
            if not key or key in seen_ch:
                continue
            seen_ch.add(key)
            merged["charts"].append(ch)
        for key, field_name in (("assumptions", "assumptions"), ("gaps", "gaps")):
            for item in payload.get(field_name) or []:
                text = str(item)
                if text in seen_txt:
                    continue
                seen_txt.add(text)
                merged[key].append(item)
    return merged


def _build_merge_prompt(
    user_message: str,
    plan: DecompositionPlan,
    entries: list[_ChildEntry],
    merged_arrays: dict[str, list],
    config: SessionConfig,
) -> str:
    lines = [
        "把下列子问题的答案合并为对用户原始问题的一个完整回答。",
        "evidence / assumptions / gaps / charts 已由系统合并完毕，原样保留，不要增删改写；",
        "你只需撰写 conclusion（综合对比/归纳各子结论）、confidence、confidence_reason、solution_path。",
        "",
        "[用户原始问题]",
        user_message,
    ]
    if plan.constraints:
        lines.append(f"[全局约束] {plan.constraints}")
    lines.append("")
    lines.append("[子问题及其答案]")
    for entry in entries:
        lines.append(f"## {entry.sub.id}: {entry.sub.question}")
        if entry.ok and isinstance(entry.result.payload, dict):
            lines.append(f"状态: 成功")
            lines.append("```json")
            lines.append(json.dumps(entry.result.payload, ensure_ascii=False))
            lines.append("```")
        elif entry.skipped_reason:
            lines.append(f"状态: 跳过（{entry.skipped_reason}）")
        else:
            err = entry.result.error if entry.result else None
            lines.append(f"状态: 失败（{err.message if err else 'unknown'}）")
    lines.append("")
    lines.append("[系统已合并的数组字段，原样保留]")
    lines.append("```json")
    lines.append(json.dumps(merged_arrays, ensure_ascii=False))
    lines.append("```")
    if config.delivery is not None:
        lines.append("")
        lines.append("输出要求：只输出一个符合下列 schema 的 JSON 对象，不输出其它文字。")
        lines.append(schema_prompt_text(config.delivery))
    return "\n".join(lines)


def _validate_answer_payload(config: SessionConfig, assistant_message: dict) -> tuple[Any, str | None]:
    payload = AgentRuntime._extract_payload(assistant_message)
    if config.delivery is None:
        return payload, None
    if config.delivery.output_schema_id == ANSWER_V1_ID:
        payload = normalize_answer_payload(payload)
    try:
        validate(payload, config.delivery.json_schema)
        return payload, None
    except ValidationError as exc:
        return payload, str(exc)


def _failure_gaps(entries: list[_ChildEntry]) -> list[str]:
    """失败/跳过子问题的缺口说明——程序化注入 gaps，不依赖合并模型自觉。"""
    gaps: list[str] = []
    for entry in entries:
        if entry.skipped_reason:
            gaps.append(f"子问题 {entry.sub.id}（{entry.sub.question}）跳过: {entry.skipped_reason}")
        elif not entry.ok:
            err = entry.result.error if entry.result else None
            gaps.append(f"子问题 {entry.sub.id}（{entry.sub.question}）求解失败: {err.message if err else 'unknown'}")
    return gaps


def _inject_failure_gaps(payload: Any, entries: list[_ChildEntry]) -> Any:
    """成功合并的负载也确定性带上失败/跳过缺口。

    提示词要求 LLM 原样保留系统合并的 gaps，但合规与否不能成为
    缺口是否暴露给用户的前提——这里按精确串查漏补齐。
    """
    if not isinstance(payload, dict):
        return payload
    present = payload.get("gaps") or []
    missing = [g for g in _failure_gaps(entries) if g not in present]
    if not missing:
        return payload
    return {**payload, "gaps": list(present) + missing}


def _conservative_merge(
    merged_arrays: dict[str, list], entries: list[_ChildEntry], merge_failed: bool
) -> dict:
    """合并模型两次未过校验时的程序化兜底：拼装成功子结论，低置信度交付。"""
    ok_entries = [e for e in entries if e.ok]
    sections = []
    for entry in ok_entries:
        conclusion = ""
        if isinstance(entry.result.payload, dict):
            conclusion = str(entry.result.payload.get("conclusion") or "")
        sections.append(f"### {entry.sub.id} {entry.sub.question}\n{conclusion}".rstrip())
    prefix = ""
    if len(ok_entries) < len(entries):
        prefix = "（部分子问题未能求解，以下为部分结果）\n\n"
    gaps = list(merged_arrays["gaps"])
    confidence_reason = "各子问题结论直接拼装"
    if merge_failed:
        gaps.append("合并模型输出两次未通过 answer-v1 校验，本答案由系统程序化合并生成")
        confidence_reason = "程序化合并兜底（合并模型输出未通过校验）"
    return {
        "conclusion": prefix + "\n\n".join(sections),
        "evidence": merged_arrays["evidence"],
        "assumptions": merged_arrays["assumptions"],
        "gaps": gaps,
        "confidence": "low",
        "confidence_reason": confidence_reason,
        "charts": merged_arrays["charts"],
        "solution_path": [],
    }


# ═══════════════════════════════════════════════════════════════════════
# 编排入口
# ═══════════════════════════════════════════════════════════════════════


def _cancelled_result(entries: list[_ChildEntry], task_id: str) -> FinalResult:
    spent = _aggregate_spent(entries, merge_call=False)
    return FinalResult(
        status="cancelled",
        error=RuntimeErrorInfo(code=ErrorCode.CANCELLED, message="Task cancelled"),
        spent=spent,
        trace_ref=f"task://{task_id}",
    )


def _aggregate_spent(entries: list[_ChildEntry], merge_call: bool) -> dict[str, Any]:
    """turns = 预调用(1) + Σ子会话 + 合并(1)；tool_calls 为各子会话求和。"""
    turns = 1 + sum((e.result.spent.get("turns", 0) if e.result else 0) for e in entries)
    if merge_call:
        turns += 1
    tool_calls = sum((e.result.spent.get("tool_calls", 0) if e.result else 0) for e in entries)
    subtasks = {
        e.sub.id: {
            "status": e.skipped_reason and "skipped" or (e.result.status if e.result else "unknown"),
            "turns": e.result.spent.get("turns", 0) if e.result else 0,
            "tool_calls": e.result.spent.get("tool_calls", 0) if e.result else 0,
        }
        for e in entries
    }
    return {"turns": turns, "tool_calls": tool_calls, "subtasks": subtasks}


def run_orchestrated(
    *,
    task_id: str,
    broker: Any,
    pool: Any,
    parent_tm: ToolManager,
    user_headers: dict | None,
    direct_llm: Any,
    llm_factory: Callable[[str], Any],
    config: SessionConfig,
    user_message: str,
    history: list[dict] | None,
    cancel_token: CancellationToken,
    skill_loader: SkillLoader | None = None,
) -> FinalResult:
    """worker 的执行入口：先尝试分解并行，门控不通过则直连。

    parent_tm 由调用方借出并已绑定用户身份；direct_llm 为工作线程级 LLM 实例
    （复用勿关闭）；llm_factory 按标签（"decompose" / "sub-N" / "merge"）新建
    已打标 LLM 实例，由本模块负责关闭。
    """

    def _run_direct() -> FinalResult:
        runtime = AgentRuntime(
            llm_backend=direct_llm,
            tool_manager=parent_tm,
            trace_sink=TaskEventTraceSink(task_id),
            skill_loader=skill_loader,
            on_tool_results=partial(publish_tool_results, broker, task_id, None),
        )
        return runtime.run_session(config, user_message, cancel_token=cancel_token, history=history)

    settings = DecompSettings.from_env()
    if not settings.enabled:
        return _run_direct()

    extra_llms: list[Any] = []
    try:
        # ── 预调用：判定是否分解 ─────────────────────────────
        plan: DecompositionPlan | None = None
        levels: list[list[SubProblem]] | None = None
        pre_llm = llm_factory("decompose")
        extra_llms.append(pre_llm)
        prompt = _build_pre_call_prompt(user_message, history, settings.max_subproblems)
        try:
            assistant = pre_llm.complete(
                [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
                None,
                cancel_token=cancel_token,
            )
            text = "\n".join(
                b.get("text", "") for b in (assistant.get("content") or []) if b.get("type") == "text"
            )
            plan = parse_pre_call_output(text, settings.max_subproblems)
        except AgentCancelled:
            raise
        except Exception as exc:
            logger.warning("[%s] 分解预调用失败，走直连: %s", task_id, exc)
            plan = None
        if plan is not None:
            levels = level_sub_problems(plan.sub_problems)
        if (
            plan is None
            or levels is None
            or len(plan.sub_problems) < 2
            or max(len(level) for level in levels) < 2
        ):
            return _run_direct()

        # ── 全局截止（镜像 run_session 行为，子 token 全部级联到此）──
        cancel_token.set_deadline(time.monotonic() + config.budget.max_time_s)
        broker.publish(task_id, {
            "type": "decompose",
            "status": "planned",
            "subtasks": [{"id": s.id, "question": s.question} for s in plan.sub_problems],
            "levels": [[s.id for s in level] for level in levels],
            "constraints": plan.constraints,
        })

        # ── 分层执行 ─────────────────────────────────────────
        width = min(max(len(level) for level in levels), settings.max_parallel)
        entries_by_id: dict[str, _ChildEntry] = {}
        with ExitStack() as stack:
            tms = [parent_tm]
            for _ in range(width - 1):
                try:
                    tm = stack.enter_context(pool.borrow(timeout=_BORROW_TIMEOUT_S))
                except RuntimeError:
                    logger.info("[%s] ToolManager 池不足，并行度降为 %d", task_id, len(tms))
                    break
                stack.enter_context(tm.bind_user_headers(user_headers))
                tms.append(tm)
            executor = ThreadPoolExecutor(max_workers=len(tms), thread_name_prefix=f"sub-{task_id[:8]}")
            stack.enter_context(executor)

            reserve = max(60, int(0.1 * config.budget.max_time_s))
            total_levels = len(levels)
            for level_index, level in enumerate(levels):
                # 级联跳过：依赖了失败/跳过子问题的项本轮不再求解
                runnable: list[SubProblem] = []
                for sub in level:
                    bad = [d for d in sub.depends_on if d in entries_by_id and not entries_by_id[d].ok]
                    if bad:
                        reason = f"因前置子问题失败跳过: {', '.join(bad)}"
                        entries_by_id[sub.id] = _ChildEntry(sub=sub, skipped_reason=reason)
                        broker.publish(task_id, {
                            "type": "subtask_status", "subtask": sub.id,
                            "status": "skipped", "error": reason,
                        })
                    else:
                        runnable.append(sub)
                for start in range(0, len(runnable), len(tms)):
                    chunk = runnable[start:start + len(tms)]
                    share_s = _child_share(
                        cancel_token.remaining_seconds(), reserve, total_levels - level_index
                    )
                    futures = []
                    for slot, sub in enumerate(chunk):
                        prereq_payloads = {
                            d: entries_by_id[d].result.payload
                            for d in sub.depends_on
                            if d in entries_by_id and entries_by_id[d].ok
                        }
                        futures.append(executor.submit(
                            _run_child_guarded,
                            task_id=task_id, broker=broker, sub=sub, plan=plan,
                            prereq_payloads=prereq_payloads, tm=tms[slot],
                            share_s=share_s, config=config, llm_factory=llm_factory,
                            parent_token=cancel_token, skill_loader=skill_loader,
                        ))
                    for future in futures:
                        entry = future.result()
                        entries_by_id[entry.sub.id] = entry
                    if cancel_token.is_cancelled():
                        ordered = _ordered_entries(plan, entries_by_id)
                        return _cancelled_result(ordered, task_id)

        ordered = _ordered_entries(plan, entries_by_id)
        if cancel_token.is_cancelled():
            return _cancelled_result(ordered, task_id)
        if not any(e.ok for e in ordered):
            first = next((e for e in ordered if e.result and e.result.error), None)
            code = first.result.error.code if first else ErrorCode.LLM_ERROR
            message = first.result.error.message if first else "所有子问题求解失败"
            return FinalResult(
                status="failed",
                error=RuntimeErrorInfo(code=code, message=f"所有子问题求解失败: {message}"),
                spent=_aggregate_spent(ordered, merge_call=False),
                trace_ref=f"task://{task_id}",
            )

        # ── 合并 ─────────────────────────────────────────────
        merged_arrays = _merge_arrays([e.result.payload for e in ordered if e.ok])
        merged_arrays["gaps"].extend(_failure_gaps(ordered))
        merge_llm = llm_factory("merge")
        extra_llms.append(merge_llm)
        merge_prompt = _build_merge_prompt(user_message, plan, ordered, merged_arrays, config)
        messages: list[dict] = [{"role": "user", "content": [{"type": "text", "text": merge_prompt}]}]
        payload: Any = None
        schema_error: str | None = None
        for _attempt in range(2):
            try:
                assistant = merge_llm.complete(messages, None, cancel_token=cancel_token)
            except AgentCancelled:
                raise
            except Exception as exc:
                logger.warning("[%s] 合并调用失败: %s", task_id, exc)
                break
            payload, schema_error = _validate_answer_payload(config, assistant)
            if schema_error is None:
                break
            messages.append(assistant)
            messages.append({"role": "user", "content": [{"type": "text", "text": (
                "Delivery schema not satisfied. "
                "Return only a JSON object matching "
                f"{config.delivery.output_schema_id if config.delivery else 'answer-v1'}. "
                f"reason={schema_error}"
            )}]})
        if payload is None or schema_error is not None:
            payload = _conservative_merge(merged_arrays, ordered, merge_failed=True)
        else:
            payload = _inject_failure_gaps(payload, ordered)
        return FinalResult(
            status="ok",
            payload=payload,
            spent=_aggregate_spent(ordered, merge_call=True),
            trace_ref=f"task://{task_id}",
        )
    except AgentCancelled:
        return FinalResult(
            status="cancelled",
            error=RuntimeErrorInfo(code=ErrorCode.CANCELLED, message="Task cancelled"),
            spent={},
            trace_ref=f"task://{task_id}",
        )
    finally:
        for llm in extra_llms:
            _close_llm(llm)


def _ordered_entries(plan: DecompositionPlan, entries_by_id: dict[str, _ChildEntry]) -> list[_ChildEntry]:
    return [entries_by_id[s.id] for s in plan.sub_problems if s.id in entries_by_id]


def _child_share(remaining_seconds: float | None, reserve_s: int, levels_left: int) -> int:
    """子问题的墙钟份额：剩余时间扣掉合并预留后按剩余层数均分，下限 5 秒。"""
    remaining = (remaining_seconds or 0.0) - reserve_s
    return max(5, int(remaining / max(1, levels_left)))


def _run_child_guarded(
    *,
    task_id: str,
    broker: Any,
    sub: SubProblem,
    plan: DecompositionPlan,
    prereq_payloads: dict[str, Any],
    tm: ToolManager,
    share_s: int,
    config: SessionConfig,
    llm_factory: Callable[[str], Any],
    parent_token: CancellationToken,
    skill_loader: SkillLoader | None,
) -> _ChildEntry:
    """子线程入口：任何线程异常都收敛为 failed 子结果，不能杀整个任务。"""
    try:
        return _run_child(
            task_id=task_id, broker=broker, sub=sub, plan=plan,
            prereq_payloads=prereq_payloads, tm=tm, share_s=share_s,
            config=config, llm_factory=llm_factory, parent_token=parent_token,
            skill_loader=skill_loader,
        )
    except Exception as exc:
        logger.warning("[%s] 子问题 %s 线程异常: %s", task_id, sub.id, exc)
        return _ChildEntry(
            sub=sub,
            result=FinalResult(
                status="failed",
                error=RuntimeErrorInfo(code=ErrorCode.LLM_ERROR, message=f"子问题线程异常: {exc}"),
                spent={},
            ),
        )


def _run_child(
    *,
    task_id: str,
    broker: Any,
    sub: SubProblem,
    plan: DecompositionPlan,
    prereq_payloads: dict[str, Any],
    tm: ToolManager,
    share_s: int,
    config: SessionConfig,
    llm_factory: Callable[[str], Any],
    parent_token: CancellationToken,
    skill_loader: SkillLoader | None,
) -> _ChildEntry:
    child_config = config.model_copy(deep=True)
    child_config.session_id = f"{config.session_id}::{sub.id}"
    child_config.trace_id = f"{config.trace_id}::{sub.id}"
    child_config.budget.max_time_s = share_s
    child_message = _build_child_message(sub, plan, prereq_payloads)
    child_token = CancellationToken(
        remote_check=parent_token.is_cancelled,
        deadline=time.monotonic() + share_s,
    )
    llm = llm_factory(sub.id)
    broker.publish(task_id, {"type": "subtask_status", "subtask": sub.id, "status": "running"})
    try:
        runtime = AgentRuntime(
            llm_backend=llm,
            tool_manager=tm,
            trace_sink=_SubtaskTraceSink(TaskEventTraceSink(task_id), sub.id),
            skill_loader=skill_loader,
            on_tool_results=partial(publish_tool_results, broker, task_id, sub.id),
        )
        result = runtime.run_session(child_config, child_message, cancel_token=child_token, history=None)
        broker.publish(task_id, {
            "type": "subtask_status", "subtask": sub.id,
            "status": result.status, "spent": result.spent,
        })
        return _ChildEntry(sub=sub, result=result)
    finally:
        _close_llm(llm)
