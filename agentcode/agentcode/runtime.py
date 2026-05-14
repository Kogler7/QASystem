from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

from jsonschema import ValidationError, validate

from .errors import ErrorCode, RuntimeErrorInfo
from .interfaces import LLMBackend
from .tools.mcp_tools.tool_manager import ToolManager
from .memory import MemoryStore, NoopMemoryStore
from .models import AgentState, FinalResult, SessionConfig
from .skillsets.loader import SkillLoader
from .trace import JsonlTraceSink, TraceSink


class AgentRuntime:
    def __init__(
        self,
        llm_backend: LLMBackend,
        tool_manager: ToolManager,
        trace_sink: TraceSink,
        memory_store: MemoryStore | None = None,
        skill_loader: SkillLoader | None = None,
        on_tool_results: Callable[[list[dict[str, Any]], int], None] | None = None,
    ) -> None:
        self.llm_backend = llm_backend
        self.tool_manager = tool_manager
        self.trace_sink = trace_sink
        self.memory_store = memory_store or NoopMemoryStore()
        self.skill_loader = skill_loader or SkillLoader()
        self.on_tool_results = on_tool_results

    def run_session(self, config: SessionConfig, user_message: str) -> FinalResult:
        state = AgentState(
            session_id=config.session_id,
            trace_id=config.trace_id,
            started_at=time.time(),
            status="running",
            messages=self._build_initial_messages(config, user_message),
        )
        return self._run_loop(config, state)

    def resume(
        self,
        config: SessionConfig,
        checkpoint_id: str,
        decision: str,
        edited_args: list[dict[str, Any]] | None = None,
        user_message: str | None = None,
    ) -> FinalResult:
        state = self.memory_store.load_checkpoint(checkpoint_id)
        if state.status != "awaiting_human":
            return FinalResult(
                status="failed",
                error=RuntimeErrorInfo(
                    code=ErrorCode.CONSTRAINT_VIOLATION,
                    message="Checkpoint is not awaiting human decision",
                ),
            )
        if decision == "reject":
            return FinalResult(
                status="failed",
                error=RuntimeErrorInfo(
                    code=ErrorCode.SESSION_CANCELLED,
                    message="Human rejected this action",
                ),
                checkpoint_id=checkpoint_id,
            )
        if decision == "edited_args" and edited_args is not None:
            state.pending_tool_calls = edited_args
        if decision in {"approve", "edited_args"}:
            state.artifacts["hitl_preapproved"] = True
        if user_message:
            state.messages.append({"role": "user", "content": [{"type": "text", "text": user_message}]})
        state.status = "running"
        return self._run_loop(config, state, checkpoint_id=checkpoint_id)

    def _run_loop(self, config: SessionConfig, state: AgentState, checkpoint_id: str | None = None) -> FinalResult:
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
            if time.time() - state.started_at > budget.max_time_s:
                return self._fail(state, ErrorCode.BUDGET_EXCEEDED, "Wall time exceeded", trace_ref, checkpoint_id)
            if state.turn >= budget.max_turns:
                return self._fail(state, ErrorCode.BUDGET_EXCEEDED, "Turn budget exceeded", trace_ref, checkpoint_id)

            pending_calls = state.pending_tool_calls
            if pending_calls:
                tool_results = self._execute_tool_calls(config, state, pending_calls)
                state.pending_tool_calls = []
                if isinstance(tool_results, FinalResult):
                    return tool_results
                state.messages.append({"role": "user", "content": tool_results})
                continue

            state.turn += 1
            started = time.time()
            assistant = self.llm_backend.complete(state.messages, all_tools, cancel_token=None)
            state.messages.append(assistant)
            tool_calls = self._extract_tool_calls(assistant)
            duration_ms = int((time.time() - started) * 1000)

            if tool_calls:
                skill_calls = [c for c in tool_calls if c.get("name") == skill_name]
                mcp_calls = [c for c in tool_calls if c.get("name") != skill_name]

                if skill_calls:
                    skill_results = self._handle_skill_invocation(skill_calls)
                    state.messages.append({"role": "user", "content": skill_results})
                    if mcp_calls:
                        tool_results = self._execute_tool_calls(config, state, mcp_calls)
                        if isinstance(tool_results, FinalResult):
                            return tool_results
                        state.messages.append({"role": "user", "content": tool_results})
                    continue

                trace_ref = self._write_turn_trace(
                    state,
                    tool_calls=tool_calls,
                    duration_ms=duration_ms,
                    outcome="tool_requests",
                )
                executed = self._execute_tool_calls(config, state, tool_calls)
                if isinstance(executed, FinalResult):
                    return executed
                state.messages.append({"role": "user", "content": executed})
                continue

            if config.hitl and "before_finalize" in config.hitl.gate:
                trace_ref = self._write_turn_trace(
                    state,
                    tool_calls=[],
                    duration_ms=duration_ms,
                    outcome="needs_human_before_finalize",
                )
                checkpoint = self._await_human(
                    state,
                    ErrorCode.HUMAN_INPUT_REQUIRED,
                    "Finalize step requires human approval",
                )
                return FinalResult(
                    status="needs_human",
                    warnings=["Awaiting human approval before finalize"],
                    checkpoint_id=checkpoint,
                    trace_ref=trace_ref,
                )

            payload, schema_error = self._validate_delivery(config, assistant)
            if schema_error:
                trace_ref = self._write_turn_trace(
                    state,
                    tool_calls=[],
                    duration_ms=duration_ms,
                    outcome="delivery_invalid",
                    delivery_error=schema_error,
                )
                state.messages.append(
                    {
                        "role": "developer",
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
                )
                if state.turn >= budget.max_turns:
                    return self._fail(
                        state,
                        ErrorCode.DELIVERY_NOT_SATISFIED,
                        "Delivery schema not satisfied within budget",
                        trace_ref,
                        checkpoint_id,
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
                checkpoint_id=checkpoint_id,
            )

    def _execute_tool_calls(
        self, config: SessionConfig, state: AgentState, tool_calls: list[dict[str, Any]]
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
                    None,
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
                None,
            )

        # ────────────────────────────────────────────────
        # 3. HITL before_tool 门禁：如果配置了 gate="before_tool" 且工具匹配 tool_globs
        #    保存检查点，返回 FinalResult(status="needs_human")
        # ────────────────────────────────────────────────
        preapproved = bool(state.artifacts.pop("hitl_preapproved", False))
        if (
            not preapproved
            and config.hitl
            and "before_tool" in config.hitl.gate
            and self._hitl_match(config, tool_calls)
        ):
            state.status = "awaiting_human"
            state.pending_tool_calls = tool_calls
            checkpoint = self._await_human(state, ErrorCode.HUMAN_INPUT_REQUIRED, "Tool execution paused for approval")
            return FinalResult(
                status="needs_human",
                warnings=["Awaiting approval before tool execution"],
                checkpoint_id=checkpoint,
            )

        # ────────────────────────────────────────────────
        # 4. 工具预算检查：tool_calls_used + len(tool_calls) > max_tool_calls
        # ────────────────────────────────────────────────
        if state.tool_calls_used + len(tool_calls) > budget.max_tool_calls:
            return self._fail(state, ErrorCode.BUDGET_EXCEEDED, "Tool call budget exceeded", "", None)

        # ────────────────────────────────────────────────
        # 5. ToolManager.call_many：实际执行工具调用
        #    sequential: 逐个 call_tool  |  parallel: ThreadPoolExecutor 并发调用
        # ────────────────────────────────────────────────
        state.tool_calls_used += len(tool_calls)
        started = time.time()
        results = self.tool_manager.call_many(
            tool_calls,
            timeout_s=max(1, int(config.budget.max_time_s)),
            retries=budget.max_retries_per_tool,
            execution_mode=config.tool_execution,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        if self.on_tool_results:
            self.on_tool_results(results, elapsed_ms)

        # ────────────────────────────────────────────────
        # 6. HITL after_tool 门禁：工具执行后再次需要人工审批
        # ────────────────────────────────────────────────
        if config.hitl and "after_tool" in config.hitl.gate and self._hitl_match(config, tool_calls):
            state.status = "awaiting_human"
            checkpoint = self._await_human(state, ErrorCode.HUMAN_INPUT_REQUIRED, "Post-tool approval required")
            return FinalResult(
                status="needs_human",
                warnings=["Awaiting approval after tool execution"],
                checkpoint_id=checkpoint,
            )
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

    def _await_human(self, state: AgentState, code: ErrorCode, message: str) -> str:
        state.status = "awaiting_human"
        state.artifacts["await_reason"] = {"code": code.value, "message": message}
        return self.memory_store.save_checkpoint(state)

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
    def _hitl_match(config: SessionConfig, tool_calls: list[dict[str, Any]]) -> bool:
        if not config.hitl:
            return False
        if not config.hitl.tool_globs:
            return True
        import fnmatch

        for call in tool_calls:
            if any(fnmatch.fnmatch(call["name"], pattern) for pattern in config.hitl.tool_globs):
                return True
        return False

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
        skill_layers = self.skill_loader.resolve(config.skills)
        metadata_text = self.skill_loader.build_metadata_text(config.skills)
        messages: list[dict[str, Any]] = []
        system_text = "\n\n".join(
            [
                "You are a single-agent runtime executor.",
                "Follow MCP-only tool usage and safety constraints.",
                metadata_text,
                *skill_layers["system"],
            ]
        ).strip()
        developer_text = "\n\n".join(
            [
                "Respect session tool policy, budget and delivery schema.",
                *skill_layers["developer"],
            ]
        ).strip()
        task_text = "\n\n".join(skill_layers["task"]).strip()
        if system_text:
            messages.append({"role": "system", "content": [{"type": "text", "text": system_text}]})
        if developer_text:
            messages.append({"role": "developer", "content": [{"type": "text", "text": developer_text}]})
        if task_text:
            messages.append({"role": "developer", "content": [{"type": "text", "text": task_text}]})
        messages.append({"role": "user", "content": [{"type": "text", "text": user_message}]})
        return messages

    @staticmethod
    def _fail(
        state: AgentState,
        code: ErrorCode,
        message: str,
        trace_ref: str,
        checkpoint_id: str | None,
    ) -> FinalResult:
        state.status = "failed"
        return FinalResult(
            status="failed",
            error=RuntimeErrorInfo(code=code, message=message),
            spent={"turns": state.turn, "tool_calls": state.tool_calls_used},
            trace_ref=trace_ref or None,
            checkpoint_id=checkpoint_id,
        )


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
