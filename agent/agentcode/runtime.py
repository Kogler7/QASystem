from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from typing import Any

from jsonschema import ValidationError, validate

from .errors import ErrorCode, RuntimeErrorInfo
from .interfaces import LLMBackend
from .tools.mcp_tools.tool_manager import ToolManager
from .models import AgentState, FinalResult, SessionConfig
from .skillsets.loader import SkillLoader
from .trace import JsonlTraceSink, TraceSink
from .tools.base_tools.memory_tool import MemoryCompactor
from .prompt import build_system_prompt
from .cancellation import AgentCancelled, CancellationToken


class AgentRuntime:
    def __init__(
        self,
        llm_backend: LLMBackend,
        tool_manager: ToolManager,
        trace_sink: TraceSink,
        skill_loader: SkillLoader | None = None,
        on_tool_results: Callable[[list[dict[str, Any]], int], None] | None = None,
    ) -> None:
        self.llm_backend = llm_backend
        self.tool_manager = tool_manager
        self.trace_sink = trace_sink
        self.skill_loader = skill_loader or SkillLoader()
        self.on_tool_results = on_tool_results
        try:
            self._compactor = MemoryCompactor()
            self._compactor_threshold = int(os.environ.get("COMPACT_TOKEN_THRESHOLD", 100000))
        except Exception:
            self._compactor = None
            self._compactor_threshold = 0

    def run_session(
        self,
        config: SessionConfig,
        user_message: str,
        cancel_token: CancellationToken | None = None,
    ) -> FinalResult:
        state = AgentState(
            session_id=config.session_id,
            trace_id=config.trace_id,
            started_at=time.time(),
            status="running",
            messages=self._build_initial_messages(config, user_message),
        )
        cancel_token = cancel_token or CancellationToken()
        cancel_token.set_deadline(time.monotonic() + config.budget.max_time_s)
        return self._run_loop(config, state, cancel_token)

    def _run_loop(
        self, config: SessionConfig, state: AgentState, cancel_token: CancellationToken
    ) -> FinalResult:
        mcp_tools = self.tool_manager.list_tools()
        if config.skills:
            skill_tool_def = self.skill_loader.build_skill_tool_definition()
            all_tools = mcp_tools + [skill_tool_def]
        else:
            all_tools = mcp_tools
        budget = config.budget
        skill_name = self.skill_loader.skill_tool_name
        trace_ref = ""
        while True:
            if cancel_token.is_cancelled():
                return self._interrupt(state, cancel_token, trace_ref)
            if time.time() - state.started_at > budget.max_time_s:
                return self._fail(state, ErrorCode.BUDGET_EXCEEDED, "Wall time exceeded", trace_ref)
            if state.turn >= budget.max_turns:
                return self._fail(state, ErrorCode.BUDGET_EXCEEDED, "Turn budget exceeded", trace_ref)

            pending_calls = state.pending_tool_calls
            if pending_calls:
                tool_results = self._execute_tool_calls(
                    config, state, pending_calls, cancel_token
                )
                state.pending_tool_calls = []
                if isinstance(tool_results, FinalResult):
                    return tool_results
                tool_msg = {"role": "tool", "content": tool_results}
                state.messages.append(tool_msg)
                continue

            state.turn += 1
            started = time.time()

            if self._compactor is not None and self._compactor_threshold > 0:
                estimated = self._compactor.estimate_tokens(state.messages)
                if estimated > self._compactor_threshold:
                    state.messages = self._compactor.auto_compact(state.messages)

            try:
                assistant = self.llm_backend.complete(
                    state.messages, all_tools, cancel_token=cancel_token
                )
            except AgentCancelled:
                return self._interrupt(state, cancel_token, trace_ref)
            except Exception as exc:
                return self._fail(
                    state, ErrorCode.LLM_ERROR, f"LLM request failed: {exc}", trace_ref
                )
            if cancel_token.is_cancelled():
                return self._interrupt(state, cancel_token, trace_ref)
            state.messages.append(assistant)
            tool_calls = self._extract_tool_calls(assistant)
            duration_ms = int((time.time() - started) * 1000)

            # 检测空响应（reasoning 模型可能耗尽 max_tokens 导致 content 为空）
            if not tool_calls:
                has_text = any(
                    b.get("type") == "text" and b.get("text", "").strip()
                    for b in (assistant.get("content") or [])
                )
                if not has_text:
                    empty_count = state.artifacts.get("_empty_response_count", 0) + 1
                    state.artifacts["_empty_response_count"] = empty_count
                    if empty_count <= 2:
                        retry_msg = {
                            "role": "user", "_agent_injected": True,
                            "content": [{"type": "text", "text": (
                                "Your last response was empty (possibly due to output token limit). "
                                "If you were about to call tools, do it now. "
                                "If you were about to write a text answer, write it now."
                            )}],
                        }
                        state.messages.append(retry_msg)
                        continue
                    return self._fail(
                        state, ErrorCode.LLM_ERROR,
                        "LLM returned empty response repeatedly", trace_ref,
                    )

            if tool_calls:
                skill_calls = [c for c in tool_calls if c.get("name") == skill_name]
                mcp_calls = [c for c in tool_calls if c.get("name") != skill_name]

                if skill_calls:
                    skill_results = self._handle_skill_invocation(skill_calls)
                    skill_msg = {"role": "user", "content": skill_results, "_agent_injected": True}
                    state.messages.append(skill_msg)
                    if mcp_calls:
                        tool_results = self._execute_tool_calls(
                            config, state, mcp_calls, cancel_token
                        )
                        if isinstance(tool_results, FinalResult):
                            return tool_results
                        tool_msg = {"role": "tool", "content": tool_results}
                        state.messages.append(tool_msg)
                        continue
                    continue

                trace_ref = self._write_turn_trace(
                    state,
                    tool_calls=tool_calls,
                    duration_ms=duration_ms,
                    outcome="tool_requests",
                )
                executed = self._execute_tool_calls(
                    config, state, tool_calls, cancel_token
                )
                if isinstance(executed, FinalResult):
                    return executed
                tool_msg = {"role": "tool", "content": executed}
                state.messages.append(tool_msg)
                continue

            payload, schema_error = self._validate_delivery(config, assistant)
            if schema_error:
                trace_ref = self._write_turn_trace(
                    state,
                    tool_calls=[],
                    duration_ms=duration_ms,
                    outcome="delivery_invalid",
                    delivery_error=schema_error,
                )
                delivery_msg = {
                    "role": "user",
                    "_agent_injected": True,
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Delivery schema not satisfied. "
                                f"Please return JSON that matches schema. reason={schema_error}"
                            ),
                        }
                    ],
                }
                state.messages.append(delivery_msg)
                if state.turn >= budget.max_turns:
                    return self._fail(
                        state,
                        ErrorCode.DELIVERY_NOT_SATISFIED,
                        "Delivery schema not satisfied within budget",
                        trace_ref,
                    )
                continue

            trace_ref = self._write_turn_trace(
                state,
                tool_calls=[],
                duration_ms=duration_ms,
                outcome="completed",
            )
            state.status = "completed"
            return FinalResult(
                status="ok",
                payload=payload,
                spent={"turns": state.turn, "tool_calls": state.tool_calls_used},
                trace_ref=trace_ref,
            )

    def _execute_tool_calls(
        self,
        config: SessionConfig,
        state: AgentState,
        tool_calls: list[dict[str, Any]],
        cancel_token: CancellationToken,
    ) -> list[dict[str, Any]] | FinalResult:
        budget = config.budget
        allowed = config.tool_policy.allow

        # ────────────────────────────────────────────────
        # 1. 白名单校验：每个 tool_call 是否匹配 config.tool_policy.allow（fnmatch 通配符）
        #   违规则返回 FinalResult(status="failed")
        # ────────────────────────────────────────────────
        for call in tool_calls:
            if not self.tool_manager.is_tool_allowed(call["name"], allowed):
                return self._fail(
                    state,
                    ErrorCode.CONSTRAINT_VIOLATION,
                    f"Tool not allowed by policy: {call['name']}",
                    "",
                )

        # ────────────────────────────────────────────────
        # 2. 重复动作检测：最近 N 次调用签名是否全部相同
        #    签名格式: "{name}::{json.dumps(input)}"
        #   违规则返回 FinalResult(status="failed")
        # ────────────────────────────────────────────────
        repeated = self._detect_repeat_actions(state, tool_calls, budget.repeat_action_threshold)
        if repeated:
            return self._fail(
                state,
                ErrorCode.REPEAT_ACTION_DETECTED,
                "Detected repeated identical tool actions",
                "",
            )

        # ────────────────────────────────────────────────
        # 3. 工具预算检查：tool_calls_used + len(tool_calls) > max_tool_calls
        # ────────────────────────────────────────────────
        if state.tool_calls_used + len(tool_calls) > budget.max_tool_calls:
            return self._fail(state, ErrorCode.BUDGET_EXCEEDED, "Tool call budget exceeded", "")

        # ────────────────────────────────────────────────
        # 4. ToolManager.call_many：实际执行工具调用
        #    sequential: 逐个 call_tool  |  parallel: ThreadPoolExecutor 并发调用
        # ────────────────────────────────────────────────
        state.tool_calls_used += len(tool_calls)
        started = time.time()
        remaining_s = max(1, int(config.budget.max_time_s - (time.time() - state.started_at)))
        results = self.tool_manager.call_many(
            tool_calls,
            timeout_s=remaining_s,
            retries=budget.max_retries_per_tool,
            execution_mode=config.tool_execution,
            cancel_token=cancel_token,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        if self.on_tool_results:
            self.on_tool_results(results, elapsed_ms)

        if cancel_token.is_cancelled():
            return self._interrupt(state, cancel_token, "")

        return results

    def _handle_skill_invocation(self, skill_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for call in skill_calls:
            skill_id = (call.get("input", {}) or {}).get("skill", "")
            call_id = call.get("id", "")
            if not skill_id:
                results.append({
                    "type": "tool_result", "tool_use_id": call_id,
                    "name": call.get("name", "Skill"), "is_error": True,
                    "content": None, "error": "Missing required parameter: skill",
                })
                continue
            try:
                body = self.skill_loader.get_skill_body(skill_id)
            except ValueError:
                results.append({
                    "type": "tool_result", "tool_use_id": call_id,
                    "name": call.get("name", "Skill"), "is_error": True,
                    "content": None,
                    "error": f"Unknown skill: {skill_id}",
                })
                continue
            results.append({
                "type": "tool_result", "tool_use_id": call_id,
                "name": call.get("name", "Skill"), "is_error": False,
                "content": body,
            })
        return results

    @staticmethod
    def _extract_tool_calls(assistant_message: dict[str, Any]) -> list[dict[str, Any]]:
        content = assistant_message.get("content", [])
        return [block for block in content if block.get("type") in {"tool_use", "tool-call"}]

    @staticmethod
    def _parse_json_from_assistant_text(text: str) -> Any | None:
        """尝试从助手纯文本中解析 JSON；支持整段或嵌入的 ``` / ```json 围栏。"""
        text = text.strip()
        if not text:
            return None
        candidates: list[str] = []
        m_full = re.fullmatch(
            r"```(?:json)?\s*\r?\n?(.*?)\r?\n?```\s*",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if m_full:
            candidates.append(m_full.group(1).strip())
        for m in re.finditer(
            r"```(?:json)?\s*\r?\n?(.*?)\r?\n?```",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            candidates.append(m.group(1).strip())
        candidates.append(text)
        seen: set[str] = set()
        for cand in candidates:
            if not cand or cand in seen:
                continue
            seen.add(cand)
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _extract_payload(assistant_message: dict[str, Any]) -> Any:
        content = assistant_message.get("content", [])
        text_blocks = [block.get("text", "") for block in content if block.get("type") == "text"]
        joined = "\n".join(filter(None, text_blocks)).strip()
        if not joined:
            return assistant_message
        parsed = AgentRuntime._parse_json_from_assistant_text(joined)
        if parsed is not None:
            return parsed
        return {"text": joined}

    def _validate_delivery(self, config: SessionConfig, assistant_message: dict[str, Any]) -> tuple[Any, str | None]:
        payload = self._extract_payload(assistant_message)
        if config.delivery is None:
            return payload, None
        try:
            validate(payload, config.delivery.json_schema)
            return payload, None
        except ValidationError as exc:
            return payload, str(exc)

    @staticmethod
    def _detect_repeat_actions(state: AgentState, tool_calls: list[dict[str, Any]], threshold: int) -> bool:
        history = state.artifacts.get("tool_signatures", [])
        for call in tool_calls:
            signature = f"{call.get('name')}::{json.dumps(call.get('input', {}), sort_keys=True, ensure_ascii=False)}"
            history.append(signature)
        history = history[-threshold:]
        state.artifacts["tool_signatures"] = history
        return threshold > 0 and len(history) >= threshold and len(set(history)) == 1

    def _write_turn_trace(
        self,
        state: AgentState,
        *,
        tool_calls: list[dict[str, Any]],
        duration_ms: int,
        outcome: str,
        delivery_error: str | None = None,
    ) -> str:
        """每条记录对应一次 LLM 调用；`outcome` 标明本轮结束后的判定。"""
        return self.trace_sink.write(
            {
                "session_id": state.session_id,
                "trace_id": state.trace_id,
                "turn": state.turn,
                "tool_calls": tool_calls,
                "duration_ms": duration_ms,
                "tool_calls_used": state.tool_calls_used,
                "outcome": outcome,
                "delivery_error": delivery_error,
            }
        )

    def _build_initial_messages(self, config: SessionConfig, user_message: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        system_text = build_system_prompt(
            tool_manager=self.tool_manager,
            skill_loader=self.skill_loader,
            skills=config.skills,
        )
        if system_text:
            messages.append({"role": "system", "content": [{"type": "text", "text": system_text}]})
        messages.append({"role": "user", "content": [{"type": "text", "text": user_message}]})
        return messages

    @staticmethod
    def _fail(
        state: AgentState,
        code: ErrorCode,
        message: str,
        trace_ref: str,
    ) -> FinalResult:
        state.status = "failed"
        return FinalResult(
            status="failed",
            error=RuntimeErrorInfo(code=code, message=message),
            spent={"turns": state.turn, "tool_calls": state.tool_calls_used},
            trace_ref=trace_ref or None,
        )

    @staticmethod
    def _cancel(state: AgentState, trace_ref: str) -> FinalResult:
        state.status = "cancelled"
        return FinalResult(
            status="cancelled",
            error=RuntimeErrorInfo(code=ErrorCode.CANCELLED, message="Task cancelled"),
            spent={"turns": state.turn, "tool_calls": state.tool_calls_used},
            trace_ref=trace_ref or None,
        )

    @staticmethod
    def _interrupt(
        state: AgentState, cancel_token: CancellationToken, trace_ref: str
    ) -> FinalResult:
        if cancel_token.deadline_exceeded():
            return AgentRuntime._fail(
                state, ErrorCode.BUDGET_EXCEEDED, "Wall time exceeded", trace_ref
            )
        return AgentRuntime._cancel(state, trace_ref)


def run_session(
    config: SessionConfig,
    user_message: str,
    llm_backend: LLMBackend,
    tool_manager: ToolManager,
    trace_path: str,
) -> FinalResult:
    runtime = AgentRuntime(
        llm_backend=llm_backend,
        tool_manager=tool_manager,
        trace_sink=JsonlTraceSink(trace_path),
    )
    return runtime.run_session(config=config, user_message=user_message)
