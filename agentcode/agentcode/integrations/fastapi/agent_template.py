"""
最小模板：多个对外 URL 复用同一个 agent 调用内核，仅返回结构不同。

实现放在本包；`backend` 仅 re-export `router`。配置北京时间 MCP（见 `agentcode.mcp_servers.time` 与
`examples/beijing_time`）后，本地 OpenAI 兼容模型可发 tool_calls，由 agentcode 执行 MCP 再继续对话。

环境变量：

- AGENT_MCP_TIME_URL 或 MCP_BEIJING_TIME_URL：MCP 的 POST 入口，如 http://127.0.0.1:8765/mcp
- AGENT_BEIJING_SKILL_PATH：可选，覆盖 skill 文件绝对路径；默认使用包内 skillsets/common/beijing_time.md

工作流模板（``POST .../define``、``/plan``、``/match``）：问题定义 → 方案模型（含 ``steps``）→ **按步**组件匹配。
问题分解默认不在路由中；编排层对 ``solution_model.steps`` 循环调用 ``/match``，body 为 ``{"step":{...}}``。

- AGENT_WORKFLOW_MCP_URL / AGENT_WORKFLOW_MCP_SERVER_NAME / AGENT_WORKFLOW_TOOL_ALLOW：各阶段共用 MCP。
- AGENT_DEFINE_SKILL_PATH、AGENT_PLAN_SKILL_PATH、AGENT_MATCH_SKILL_PATH：覆盖 skill（规划默认 ``solution_model.md``）。
- AGENT_WORKFLOW_MAX_TURNS 等：预算；AGENT_WORKFLOW_TRACE_PATH：trace 路径。

业务替换 ``workflow_pipeline`` 中 schema 与 ``skillsets/workflow/*.md`` 即可。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...mcp import McpClient
from ...models import DeliveryConfig, SessionConfig
from ...profiles import (
    build_beijing_time_config,
    build_define_session_config,
    build_match_session_config,
    build_plan_session_config,
    new_workflow_ids,
    resolve_default_define_skill_path,
    resolve_default_match_skill_path,
    resolve_default_plan_skill_path,
)
from ...runtime import AgentRuntime
from ...trace import JsonlTraceSink

router = APIRouter(prefix="/api/agent-template", tags=["agent-template"])
logger = logging.getLogger(__name__)


class AgentTemplateRequest(BaseModel):
    query: str


class WorkflowDefineRequest(BaseModel):
    """问题定义接口入参：自然语言问题。"""

    question: str


class WorkflowPlanRequest(BaseModel):
    """方案规划：入参为问题模型 ``problem_model``；产出为 ``solution_model``（含 ``steps``）。"""

    problem_model: dict[str, Any]


class SolutionStepPayload(BaseModel):
    """与方案模型中每一步一致；组件匹配接口每次传入一步。"""

    objective: str
    inputs: list[str]
    outputs: list[str]
    constraints: list[str]


class WorkflowMatchRequest(BaseModel):
    """组件匹配：对单个 ``step`` 调用；编排层对方案模型 ``steps`` 循环请求本接口。"""

    step: SolutionStepPayload


def _llm_config() -> dict[str, Any]:
    """读取统一 LLM 环境变量（与 algorithm/common/llm_client.py 一致）。"""
    return {
        "backend": (os.getenv("LLM_BACKEND") or "openai").strip().lower(),
        "base_url": (os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "http://127.0.0.1:9999").strip(),
        "api_version": (os.getenv("LLM_API_VERSION") or "v1").strip("/"),
        "model": (os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "Qwen/Qwen3-8B").strip(),
        "api_key": (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip(),
        "temperature": float(os.getenv("AGENT_TEMPLATE_TEMPERATURE") or 0.2),
        "max_tokens": int(os.getenv("AGENT_TEMPLATE_MAX_TOKENS") or 1024),
        "timeout_s": int(os.getenv("AGENT_TEMPLATE_TIMEOUT_SECONDS") or 60),
    }


def _chat_completions_url(base_url: str, api_version: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith(f"/{api_version}"):
        return f"{base}/chat/completions"
    return f"{base}/{api_version}/chat/completions"


def _content_text(content_blocks: list[dict[str, Any]]) -> str:
    text_parts: list[str] = []
    for block in content_blocks:
        if block.get("type") == "text":
            value = (block.get("text") or "").strip()
            if value:
                text_parts.append(value)
    return "\n".join(text_parts).strip()


def _sanitize_tool_parameters(schema: dict[str, Any]) -> dict[str, Any]:
    """将 MCP 的 input_schema 收窄为 OpenAI `function.parameters` 常用子集。"""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = dict(schema)
    for k in (
        "additionalProperties",
        "additional_properties",
        "$schema",
        "definitions",
        "$defs",
    ):
        out.pop(k, None)
    if out.get("type") != "object":
        out["type"] = "object"
    if "properties" not in out or not isinstance(out["properties"], dict):
        out["properties"] = {}
    return out


def _mcp_tools_to_openai(mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """McpClient.list_tools() 结果 → OpenAI chat/completions 的 tools 列表。"""
    out: list[dict[str, Any]] = []
    for t in mcp_tools:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        desc = (t.get("description") or "").strip() or name
        params = t.get("input_schema") or t.get("inputSchema") or {"type": "object", "properties": {}}
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        params = _sanitize_tool_parameters(params)
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc[:2048],
                    "parameters": params,
                },
            }
        )
    return out


def _openai_messages_from_agentcode(agent_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """agentcode 消息（含 tool_result 块、assistant tool_use 块）→ OpenAI messages。"""
    out: list[dict[str, Any]] = []
    for msg in agent_messages:
        role = (msg.get("role") or "user").strip().lower()
        content = msg.get("content", [])

        if role == "developer":
            role = "system"

        if role == "user" and isinstance(content, list) and content:
            if all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                for b in content:
                    tid = (b.get("tool_use_id") or b.get("id") or "").strip()
                    if b.get("is_error"):
                        text = json.dumps(
                            {"error": b.get("error"), "error_code": b.get("error_code")},
                            ensure_ascii=False,
                        )
                    else:
                        payload = b.get("content")
                        if payload is None:
                            text = ""
                        elif isinstance(payload, str):
                            text = payload
                        else:
                            text = json.dumps(payload, ensure_ascii=False)
                    out.append({"role": "tool", "tool_call_id": tid, "content": text})
                continue

        if role == "assistant" and isinstance(content, list):
            tool_blocks = [
                b
                for b in content
                if isinstance(b, dict) and b.get("type") in ("tool_use", "tool-call")
            ]
            text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
            if tool_blocks:
                tool_calls: list[dict[str, Any]] = []
                for i, tb in enumerate(tool_blocks):
                    call_id = (tb.get("id") or f"call_{i}").strip()
                    fn_name = (tb.get("name") or "").strip()
                    inp = tb.get("input")
                    if isinstance(inp, str):
                        try:
                            inp = json.loads(inp)
                        except json.JSONDecodeError:
                            inp = {}
                    elif not isinstance(inp, dict):
                        inp = {}
                    tool_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": fn_name,
                                "arguments": json.dumps(inp, ensure_ascii=False),
                            },
                        }
                    )
                oa_msg: dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
                txt = _content_text(text_blocks)
                # 仅含 tool_calls 时勿传 content:null（部分本地 OpenAI 兼容实现会 400）
                if txt:
                    oa_msg["content"] = txt
                out.append(oa_msg)
                continue

        if role not in {"system", "user", "assistant"}:
            continue

        if isinstance(content, list):
            text = _content_text(content)
        elif isinstance(content, str):
            text = content.strip()
        else:
            text = ""
        if not text:
            continue
        out.append({"role": role, "content": text})
    return out


def _assistant_message_from_openai_response(data: dict[str, Any]) -> dict[str, Any]:
    """OpenAI 单条 choice.message → agentcode assistant 消息。"""
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    tool_calls = msg.get("tool_calls") or []
    raw_content = msg.get("content")
    if isinstance(raw_content, list):
        text = ""
        for part in raw_content:
            if isinstance(part, dict) and part.get("type") == "text":
                text += (part.get("text") or "") + "\n"
        text = text.strip()
    elif isinstance(raw_content, str):
        text = raw_content.strip()
    else:
        text = ""

    if tool_calls:
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in tool_calls:
            fn = tc.get("function") or {}
            fn_name = (fn.get("name") or "").strip()
            raw_args = fn.get("arguments")
            if raw_args is None:
                raw_args = "{}"
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(str(raw_args))
                except json.JSONDecodeError:
                    args = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": (tc.get("id") or "").strip(),
                    "name": fn_name,
                    "input": args,
                }
            )
        return {"role": "assistant", "content": blocks}

    if not text:
        raise ValueError("LLM 返回既无文本也无 tool_calls")
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


class _ConfiguredLLMBackend:
    """真实 LLM backend：OpenAI 兼容 chat/completions；支持 tools 与多轮 tool 结果。"""

    def __init__(self) -> None:
        self.cfg = _llm_config()

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], cancel_token: Any = None
    ) -> dict[str, Any]:
        if self.cfg["backend"] != "openai":
            raise ValueError(
                "agent-template 当前仅示例 OpenAI 兼容后端，请设置 LLM_BACKEND=openai"
            )

        headers = {"Content-Type": "application/json"}
        if self.cfg["api_key"]:
            headers["Authorization"] = f"Bearer {self.cfg['api_key']}"

        openai_messages = _openai_messages_from_agentcode(messages)
        openai_tools = _mcp_tools_to_openai(tools) if tools else []

        payload: dict[str, Any] = {
            "model": self.cfg["model"],
            "messages": openai_messages,
            "temperature": self.cfg["temperature"],
            "max_tokens": self.cfg["max_tokens"],
            "stream": False,
        }
        if openai_tools:
            payload["tools"] = openai_tools
            tc = (os.getenv("AGENT_TEMPLATE_TOOL_CHOICE") or "").strip()
            if tc:
                payload["tool_choice"] = tc

        url = _chat_completions_url(self.cfg["base_url"], self.cfg["api_version"])
        try:
            with httpx.Client(timeout=self.cfg["timeout_s"]) as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            body = (exc.response.text or "")[:2000]
            logger.error("agent-template LLM HTTP %s body=%s", exc.response.status_code, body)
            raise ValueError(f"LLM HTTP {exc.response.status_code}: {body}") from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent-template LLM 调用失败")
            raise ValueError(f"LLM request failed: {exc}") from exc

        return _assistant_message_from_openai_response(data)


def _mcp_time_url() -> str:
    return (os.getenv("AGENT_MCP_TIME_URL") or os.getenv("MCP_BEIJING_TIME_URL") or "").strip()


def _beijing_skill_path() -> str:
    custom = (os.getenv("AGENT_BEIJING_SKILL_PATH") or "").strip()
    if custom:
        return custom
    try:
        import agentcode

        root = Path(agentcode.__file__).resolve().parent
        candidates = [root / "skillsets" / "common" / "beijing_time.md"]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    except Exception:
        logger.debug("无法解析内置 skill 路径", exc_info=True)
    return ""


def _run_agent_core(query: str):
    """
    单一 agent 调用内核：所有 URL 都调用这里。
    如需接入真实模型/MCP，只替换这里的 backend/client 构造。
    """
    mcp_url = _mcp_time_url()
    if mcp_url:
        config = build_beijing_time_config(mcp_url=mcp_url, skill_path=_beijing_skill_path(), session_prefix="demo")
    else:
        llm_cfg = _llm_config()
        wall_s = max(120, int(llm_cfg["timeout_s"]))
        now_ms = int(time.time() * 1000)
        config = SessionConfig(
            session_id=f"demo-{now_ms}",
            trace_id=str(uuid.uuid4()),
            budget={
                "max_turns": 12,
                "max_tool_calls": 12,
                "max_time_s": wall_s,
                "max_retries_per_tool": 1,
                "repeat_action_threshold": 3,
            },
            delivery=DeliveryConfig(
                output_schema_id="answer-template-v1",
                json_schema={
                    "type": "object",
                    "properties": {
                        "answer": {"type": "string"},
                        "style": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["answer"],
                },
            ),
        )
    runtime = AgentRuntime(
        llm_backend=_ConfiguredLLMBackend(),
        mcp_client=McpClient(config=config),
        trace_sink=JsonlTraceSink(path="/tmp/agentcode-template-trace.jsonl"),
    )
    return runtime.run_session(config=config, user_message=query)


def _call_template_agent(query: str):
    """统一入口：LLM/MCP 失败时抛出 502，便于路由层返回 JSON。"""
    try:
        return _run_agent_core(query)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _workflow_trace_path() -> str:
    return (os.getenv("AGENT_WORKFLOW_TRACE_PATH") or "/tmp/agentcode-workflow-trace.jsonl").strip()


def _run_workflow_session(config: SessionConfig, user_message: str):
    runtime = AgentRuntime(
        llm_backend=_ConfiguredLLMBackend(),
        mcp_client=McpClient(config=config),
        trace_sink=JsonlTraceSink(path=_workflow_trace_path()),
    )
    return runtime.run_session(config=config, user_message=user_message)


def _call_workflow_session(config: SessionConfig, user_message: str):
    try:
        return _run_workflow_session(config, user_message)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/ping")
async def agent_template_ping() -> dict[str, Any]:
    """
    连通性探测：若本接口返回 JSON 而非 HTML 404，说明请求已到达本 FastAPI 应用。
    `/brief` 等接口必须使用 **POST** + JSON body `{"query":"..."}`。
    """
    return {
        "ok": True,
        "router": "agent-template",
        "hint": (
            "POST /api/agent-template/brief with JSON body {\"query\":\"...\"}; "
            "workflow: /define, /plan, /match (per-step) — see module docstring"
        ),
    }


@router.get("/config")
async def current_model_config() -> dict[str, Any]:
    """查看 agent-template 当前生效模型配置（密钥脱敏）。"""
    cfg = _llm_config()
    mcp_url = _mcp_time_url()
    return {
        "backend": cfg["backend"],
        "base_url": cfg["base_url"],
        "api_version": cfg["api_version"],
        "model": cfg["model"],
        "api_key_configured": bool(cfg["api_key"]),
        "temperature": cfg["temperature"],
        "max_tokens": cfg["max_tokens"],
        "timeout_s": cfg["timeout_s"],
        "mcp_time_configured": bool(mcp_url),
        "mcp_time_tool_full_name": "time__get_beijing_time" if mcp_url else None,
        "beijing_skill_resolved": bool(_beijing_skill_path()),
        "workflow_mcp_configured": bool((os.getenv("AGENT_WORKFLOW_MCP_URL") or "").strip()),
        "workflow_define_skill_resolved": bool(
            (os.getenv("AGENT_DEFINE_SKILL_PATH") or "").strip() or resolve_default_define_skill_path()
        ),
        "workflow_plan_skill_resolved": bool(
            (os.getenv("AGENT_PLAN_SKILL_PATH") or "").strip() or resolve_default_plan_skill_path()
        ),
        "workflow_match_skill_resolved": bool(
            (os.getenv("AGENT_MATCH_SKILL_PATH") or "").strip() or resolve_default_match_skill_path()
        ),
    }


@router.post("/raw")
async def raw_result(body: AgentTemplateRequest) -> dict[str, Any]:
    """
    URL #1：返回 agent 原始结果（包含 spent/trace/warnings）。
    """
    if not body.query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")
    result = _call_template_agent(body.query.strip())
    return result.model_dump(mode="json")


@router.post("/brief")
async def brief_result(body: AgentTemplateRequest) -> dict[str, Any]:
    """
    URL #2：同样调用 agent，但只返回前端最关心字段。
    """
    if not body.query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")
    result = _call_template_agent(body.query.strip())
    payload = result.payload if isinstance(result.payload, dict) else {}
    return {
        "ok": result.status == "ok",
        "answer": payload.get("answer", ""),
    }


@router.post("/debug")
async def debug_result(body: AgentTemplateRequest) -> dict[str, Any]:
    """
    URL #3：同样调用 agent，但返回调试/观测信息。
    """
    if not body.query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")
    result = _call_template_agent(body.query.strip())
    payload = result.payload if isinstance(result.payload, dict) else {}
    return {
        "status": result.status,
        "answer": payload.get("answer", ""),
        "spent": result.spent,
        "trace_ref": result.trace_ref,
        "warnings": result.warnings,
        "error": result.error.model_dump(mode="json") if result.error else None,
    }


@router.post("/define")
async def workflow_define(body: WorkflowDefineRequest) -> dict[str, Any]:
    """阶段一：问题定义。模板：单次 ``run_session``，返回 ``problem_model`` 占位结构。"""
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 不能为空")

    sid, tid = new_workflow_ids("wf-define")
    cfg = build_define_session_config(session_id=sid, trace_id=tid)
    define_input = json.dumps({"question": question}, ensure_ascii=False)
    result = _call_workflow_session(cfg, define_input)

    if result.status == "needs_human":
        raise HTTPException(
            status_code=422,
            detail={
                "stage": "define",
                "message": "需要人工审批或输入",
                "checkpoint_id": result.checkpoint_id,
                "warnings": result.warnings,
            },
        )
    if result.status != "ok":
        raise HTTPException(
            status_code=422,
            detail={
                "stage": "define",
                "message": (result.error.message if result.error else "define failed"),
                "error": result.error.model_dump(mode="json") if result.error else None,
            },
        )

    payload = result.payload if isinstance(result.payload, dict) else {}
    problem_model = payload.get("problem_model")
    if not isinstance(problem_model, dict):
        raise HTTPException(status_code=422, detail={"stage": "define", "message": "交付物缺少 problem_model"})

    return {
        "problem_model": problem_model,
        "warnings": result.warnings,
        "trace_ref": result.trace_ref,
        "spent": result.spent,
    }


@router.post("/plan")
async def workflow_plan(body: WorkflowPlanRequest) -> dict[str, Any]:
    """阶段二：方案规划。入参问题模型，产出 ``solution_model``（``steps`` 供后续逐步 ``/match``）。"""
    if not body.problem_model:
        raise HTTPException(status_code=400, detail="problem_model 不能为空")

    sid, tid = new_workflow_ids("wf-plan")
    cfg = build_plan_session_config(session_id=sid, trace_id=tid)
    plan_input = json.dumps({"problem_model": body.problem_model}, ensure_ascii=False)
    result = _call_workflow_session(cfg, plan_input)

    if result.status == "needs_human":
        raise HTTPException(
            status_code=422,
            detail={
                "stage": "plan",
                "message": "方案规划需要人工审批或输入",
                "checkpoint_id": result.checkpoint_id,
                "warnings": result.warnings,
            },
        )
    if result.status != "ok":
        raise HTTPException(
            status_code=422,
            detail={
                "stage": "plan",
                "message": (result.error.message if result.error else "plan failed"),
                "error": result.error.model_dump(mode="json") if result.error else None,
            },
        )

    payload = result.payload if isinstance(result.payload, dict) else {}
    solution_model = payload.get("solution_model")
    if not isinstance(solution_model, dict):
        raise HTTPException(status_code=422, detail={"stage": "plan", "message": "交付物缺少 solution_model"})
    return {
        "ok": True,
        "solution_model": solution_model,
        "warnings": result.warnings,
        "trace_ref": result.trace_ref,
        "spent": result.spent,
    }


@router.post("/match")
async def workflow_match(body: WorkflowMatchRequest) -> dict[str, Any]:
    """组件匹配：单步。入参 ``{"step":{objective, inputs, outputs, constraints}}``；对 ``solution_model.steps`` 逐项调用。"""
    sid, tid = new_workflow_ids("wf-match")
    cfg = build_match_session_config(session_id=sid, trace_id=tid)
    payload_in = json.dumps({"step": body.step.model_dump()}, ensure_ascii=False)
    result = _call_workflow_session(cfg, payload_in)

    if result.status == "needs_human":
        raise HTTPException(
            status_code=422,
            detail={
                "stage": "match",
                "message": "需要人工审批或输入",
                "checkpoint_id": result.checkpoint_id,
                "warnings": result.warnings,
            },
        )
    if result.status != "ok":
        raise HTTPException(
            status_code=422,
            detail={
                "stage": "match",
                "message": (result.error.message if result.error else "match failed"),
                "error": result.error.model_dump(mode="json") if result.error else None,
            },
        )

    payload = result.payload if isinstance(result.payload, dict) else {}
    return {
        "ok": True,
        "component_match": payload.get("component_match"),
        "warnings": result.warnings,
        "trace_ref": result.trace_ref,
        "spent": result.spent,
    }
