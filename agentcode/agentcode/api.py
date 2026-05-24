"""
agentcode HTTP API — 将 AgentRuntime 暴露为 RESTful / SSE 流式服务。

核心架构：
    本模块是 agentcode 的 HTTP 网关层，将底层的 AgentRuntime 执行循环
    包装成 FastAPI 端点。用户通过 HTTP 请求触发智能问数 / 问题求解，
    内部 Agent 自动调用 MCP 工具链完成查询任务。

整体数据流：
    HTTP 请求 → QueryRequest 模型 → _build_agent() 构建运行时
    → AgentRuntime.run_session() 执行 Agent Loop
    → LLM 推理 → 工具调用 → 结果返回 → QueryResponse / SSE 流

两种输出模式：
    1. 同步模式 (/v1/query)：Agent 完整执行完，一次性返回结果
    2. 流式模式 (/v1/query/stream)：通过 SSE 逐 token 推送 LLM 输出，
       同时推送工具调用和工具结果事件

启动:
    uvicorn agentcode.api:app --host 0.0.0.0 --port 9180

    # 或用 CLI 入口
    agentcode-api --port 9180

端点:
    GET  /health            — 健康检查
    POST /v1/query           — 同步查询，返回 FinalResult
    POST /v1/query/stream    — SSE 流式查询
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .interfaces import LLMBackend
from .models import BudgetConfig, SessionConfig, ToolPolicy
from .runtime import AgentRuntime
from .skillsets.loader import SkillLoader
from .tools.mcp_tools.mcp_loader import build_config_from_mcp_json
from .tools.mcp_tools.tool_manager import ToolManager
from .trace import NoopTraceSink

# ═══════════════════════════════════════════════════════
# 环境 & 路径工具
# ═══════════════════════════════════════════════════════

def _load_env() -> dict[str, Any]:
    """加载 LLM 连接所需的环境变量。

    加载顺序：
        1. 优先读取 agentcode/.env（agentcode 项目自身配置）
        2. 回退到 backend/.env（兼容旧的 monorepo 结构）
        3. 再回退到硬编码默认值

    每个配置项都支持 LLM_* 和 OPENAI_* 两套命名，
    LLM_* 优先级更高，方便同时对接多种 LLM 提供商。
    """
    # 定位 agentcode 目录：当前文件 api.py 位于 agentcode/agentcode/ 下
    # os.path.dirname(__file__)  = agentcode/agentcode/
    # os.path.dirname(...)        = agentcode/
    agentcode_dir = os.path.dirname(os.path.dirname(__file__))
    env_path = os.path.join(agentcode_dir, ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path, override=False)
    else:
        backend_env = os.path.join(os.path.dirname(agentcode_dir), "backend", ".env")
        if os.path.exists(backend_env):
            load_dotenv(backend_env, override=False)
    return {
        "base_url": (os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.deepseek.com").strip(),
        "api_key": (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip(),
        "model": (os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "deepseek-v4-pro").strip(),
        "temperature": float(os.getenv("AGENT_TEMPLATE_TEMPERATURE") or 0.2),
        "max_tokens": int(os.getenv("AGENT_TEMPLATE_MAX_TOKENS") or 4096),
        "timeout_s": int(os.getenv("AGENT_TEMPLATE_TIMEOUT_SECONDS") or 120),
    }

def _chat_completions_url(base_url: str) -> str:
    """将用户配置的 base_url 标准化为 /v1/chat/completions 完整路径。

    兼容三种输入格式：
        - https://api.deepseek.com        → 追加 /v1/chat/completions
        - https://api.deepseek.com/v1     → 追加 /chat/completions
        - https://api.openai.com/v1/chat/completions  → 原样返回
    """
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"

def _default_mcp_path() -> Path:
    """获取默认的 MCP 配置文件路径：agentcode/tools/mcp_tools/mcp_config.json"""
    p = Path(__file__).resolve().parent / "tools" / "mcp_tools" / "mcp_config.json"
    return p if p.is_file() else Path("mcp_config.json")

# ═══════════════════════════════════════════════════════
# 消息格式转换 (agentcode 内部格式 → OpenAI API 格式)
# ═══════════════════════════════════════════════════════
#
# 背景：agentcode 内部使用自定义消息格式（MCP 兼容），
# 而 LLM API（OpenAI / DeepSeek）需要标准 OpenAI Chat Completions 格式。
# 这两个函数负责双向转换。
#
# 内部消息格式示例（agentcode）：
#   {"role": "user", "content": [{"type": "text", "text": "查故障率"}]}
#   {"role": "assistant", "content": [{"type": "text", ...}, {"type": "tool_use", ...}]}
#   {"role": "tool", "content": [{"type": "tool_result", ...}]}
#
# OpenAI 格式示例：
#   {"role": "user", "content": "查故障率"}
#   {"role": "assistant", "content": "结果...", "tool_calls": [...]}
#   {"role": "tool", "tool_call_id": "call_1", "content": "{...}"}


def _messages_to_openai(agent_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 agentcode 内部消息列表转换为 OpenAI Chat Completions 格式。

    转换要点：
        - content 为列表（[{"type":"text","text":...}, ...]）→ 拼成纯文本
        - developer role → system role（OpenAI 不区分 developer/system）
        - tool_use 块 → OpenAI tool_calls 格式
        - tool_result 块 → OpenAI tool 消息格式
        - 空文本消息会被跳过（减少 token 浪费）
    """
    out: list[dict[str, Any]] = []
    for msg in agent_messages:
        role = (msg.get("role") or "user").strip().lower()
        content = msg.get("content", [])
        if role == "developer":
            role = "system"
    # ── 处理 tool_result 类消息（工具执行结果）──
        if role in ("user", "tool") and isinstance(content, list) and content:
            if all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                for b in content:
                    tid = (b.get("tool_use_id") or b.get("id") or "").strip()
                    if b.get("is_error"):
                        # 工具失败：封装错误信息
                        text = json.dumps({"error": b.get("error"), "error_code": b.get("error_code")}, ensure_ascii=False)
                    else:
                        payload = b.get("content")
                        # 确保 content 是字符串
                        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False) if payload else ""
                    out.append({"role": "tool", "tool_call_id": tid, "content": text})
                continue

        # ── 处理 assistant 类消息（LLM 回复，可能包含 tool_use）──
        if role == "assistant" and isinstance(content, list):
            tool_blocks = [b for b in content if isinstance(b, dict) and b.get("type") in ("tool_use", "tool-call")]
            text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
            if tool_blocks:
                tool_calls = []
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
                    tool_calls.append({"id": call_id, "type": "function", "function": {"name": fn_name, "arguments": json.dumps(inp, ensure_ascii=False)}})
                oa_msg: dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
                txt = "\n".join(b.get("text", "") for b in text_blocks if b.get("text")).strip()
                if txt:
                    oa_msg["content"] = txt
                reasoning = msg.get("reasoning_content")
                if reasoning:
                    oa_msg["reasoning_content"] = reasoning
                out.append(oa_msg)
                continue

        if role not in {"system", "user", "assistant", "tool"}:
            continue
        if isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content if b.get("text")).strip()
        elif isinstance(content, str):
            text = content.strip()
        else:
            text = ""
        if not text:
            continue
        oa_simple = {"role": role, "content": text}
        reasoning = msg.get("reasoning_content")
        if reasoning:
            oa_simple["reasoning_content"] = reasoning
        out.append(oa_simple)
    return out

def _mcp_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 MCP 工具 schema 列表转换为 OpenAI function calling tools 格式。

    OpenAI 工具格式：
        {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}

    转换要点：
        - 清理 MCP schema 中 OpenAI 不支持的字段（$schema, definitions 等）
        - 确保 parameters.type == "object"（OpenAI 要求）
        - 确保 properties 存在（即使为空对象）
        - description 截断到 2048 字符（OpenAI 限制）
    """
    out: list[dict[str, Any]] = []
    for t in tools:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        desc = (t.get("description") or "").strip() or name
        params = t.get("input_schema") or t.get("inputSchema") or {"type": "object", "properties": {}}
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        for k in ("additionalProperties", "additional_properties", "$schema", "definitions", "$defs"):
            params.pop(k, None)
        if params.get("type") != "object":
            params["type"] = "object"
        if "properties" not in params or not isinstance(params["properties"], dict):
            params["properties"] = {}
        out.append({"type": "function", "function": {"name": name, "description": desc[:2048], "parameters": params}})
    return out

# ═══════════════════════════════════════════════════════
# 流式 LLM 后端 (线程安全的 asyncio.Queue 转发)
# ═══════════════════════════════════════════════════════
#
# 设计背景：
#   AgentRuntime._run_loop() 是一个同步 while 循环，内部同步调用
#   LLMBackend.complete()。但我们需要把 LLM 的逐 token 输出实时推送给
#   HTTP 客户端（SSE 流式）。
#
# 解决方案：
#   利用 Python asyncio.Queue 的线程安全特性：
#     1. 主线程（FastAPI event loop）：从 queue 中异步读取事件，推给 SSE
#     2. 后台线程（run_in_executor）：同步调用 LLM 流式 API，
#        每收到一个 token 就 queue.put_nowait()，线程安全
#
#   Queue 元素格式：{"type": "token"|"tool_call"|"error", "data": ...}
#
# 哨兵对象 _STREAM_EVENT_SENTINEL：
#   当 Agent 循环结束时，后台线程向 queue 放入一个特殊对象，
#   event_generator 读到它时就知道该结束 SSE 流了。

_STREAM_EVENT_SENTINEL = object()

class QueueStreamingLLM:
    """LLMBackend 实现：同步调用 LLM API，流式 token 推送到 asyncio.Queue。

    必须从后台线程调用 ``complete()``（如 ``loop.run_in_executor``），
    asyncio.Queue.put_nowait() 是线程安全的。

    工作流程：
        1. AgentRuntime 在后台线程中调用 complete(messages, tools)
        2. complete() 发起 HTTP 流式请求（stream=True）
        3. 逐行解析 SSE 响应（data: {...}）
        4. 文本 token → queue.put_nowait({"type": "token", "data": text})
        5. 工具调用 → 累积完整后 → queue.put_nowait({"type": "tool_call", ...})
        6. 返回完整 assistant 消息给 AgentRuntime 继续循环

    Attributes:
        _q: asyncio.Queue，主线程从此队列读取事件推给 SSE 客户端
        _url: LLM API 的 chat/completions 端点
        _cfg: 从环境变量加载的 LLM 配置
    """

    def __init__(self, queue: asyncio.Queue):
        self._q = queue
        self._cfg = _load_env()
        self._url = _chat_completions_url(self._cfg["base_url"])

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], cancel_token: Any = None,
    ) -> dict[str, Any]:
        # ── 1. 格式转换：agentcode 内部格式 → OpenAI API 格式 ──
        oa_msgs = _messages_to_openai(messages)
        oa_tools = _mcp_tools_to_openai(tools) if tools else []

        # ── 2. 构建 API 请求 payload ──
        payload: dict[str, Any] = {
            "model": self._cfg["model"],
            "messages": oa_msgs,
            "temperature": self._cfg["temperature"],
            "max_tokens": self._cfg["max_tokens"],
            "stream": True,          # 开启流式输出
        }
        if oa_tools:
            payload["tools"] = oa_tools     # 注入 function calling 工具定义

        headers = {"Content-Type": "application/json"}
        if self._cfg["api_key"]:
            headers["Authorization"] = f"Bearer {self._cfg['api_key']}"

        # ── 3. 流式解析 LLM API 响应 ──
        # LLM API 返回格式（SSE）：
        #   data: {"choices":[{"delta":{"content":"你"},"index":0}]}
        #   data: {"choices":[{"delta":{"content":"好"},"index":0}]}
        #   data: {"choices":[{"delta":{"tool_calls":[...]},"index":0}]}
        #   data: [DONE]
        #
        # 文本 token 累积在 accumulated_text 中
        # 工具调用 token 累积在 accumulated_tool_calls 字典里（按 index 分组）
        # 因为流式模式下 tool_calls 会分多次 chunk 返回
        accumulated_text = ""
        accumulated_reasoning = ""
        accumulated_tool_calls: dict[int, dict[str, Any]] = {}

        try:
            with httpx.Client(timeout=self._cfg["timeout_s"]) as client:
                with client.stream("POST", self._url, json=payload, headers=headers) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        choice = (chunk.get("choices") or [{}])[0]
                        delta = choice.get("delta") or {}

                        # 推理内容（deepseek 特有字段）
                        if "reasoning_content" in delta and delta["reasoning_content"]:
                            accumulated_reasoning += delta["reasoning_content"]

                        # 文本 token → 立即推送到 SSE 队列
                        if "content" in delta and delta["content"]:
                            text = delta["content"]
                            accumulated_text += text
                            self._q.put_nowait({"type": "token", "data": text})

                        # 工具调用 token → 累积完整再推送（流式下 tool_call 分多 chunk）
                        if "tool_calls" in delta:
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                if idx not in accumulated_tool_calls:
                                    accumulated_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                                if "id" in tc and tc["id"]:
                                    accumulated_tool_calls[idx]["id"] = tc["id"]
                                if "function" in tc:
                                    fn = tc["function"]
                                    if "name" in fn and fn["name"]:
                                        accumulated_tool_calls[idx]["name"] += fn["name"]
                                    if "arguments" in fn and fn["arguments"]:
                                        accumulated_tool_calls[idx]["arguments"] += fn["arguments"]
        except httpx.HTTPStatusError as exc:
            try:
                body = exc.response.text[:500]
            except Exception:
                body = "(unable to read response body)"
            # 错误也推送到 SSE 队列，让客户端感知
            self._q.put_nowait({"type": "error", "data": f"HTTP {exc.response.status_code}: {body}"})
            raise
        except Exception as exc:
            self._q.put_nowait({"type": "error", "data": str(exc)})
            raise

        # ── 4. 组装返回 assistant 消息 ──
        # 返回给 AgentRuntime 的内部格式：
        #   {"role": "assistant", "content": [{"type": "text", "text": "..."}, {"type": "tool_use", ...}]}
        blocks: list[dict[str, Any]] = []
        if accumulated_text.strip():
            blocks.append({"type": "text", "text": accumulated_text.strip()})

        if accumulated_tool_calls:
            # 按 index 排序，保证工具调用顺序
            for idx in sorted(accumulated_tool_calls.keys()):
                tc = accumulated_tool_calls[idx]
                fn_name = tc["name"]
                try:
                    fn_args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    fn_args = {}
                blocks.append({"type": "tool_use", "id": tc["id"] or f"call_{idx}", "name": fn_name, "input": fn_args})

            # 工具调用事件推送给 SSE 客户端（包含工具名和参数）
            self._q.put_nowait({"type": "tool_call", "data": {
                "calls": [{"name": b["name"], "input": b["input"]} for b in blocks if b.get("type") in ("tool_use",)],
            }})

        result: dict[str, Any] = {"role": "assistant", "content": blocks}
        if accumulated_reasoning:
            result["reasoning_content"] = accumulated_reasoning
        return result

# ═══════════════════════════════════════════════════════
# 请求 / 响应模型
# ═══════════════════════════════════════════════════════

class QueryRequest(BaseModel):
    """HTTP API 请求体。所有字段均有默认值，只需提供 user_message 即可。"""
    user_message: str                               # 用户输入的自然语言问题
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    mcp_config_path: str | None = None              # 覆盖默认 mcp_config.json 路径
    skill_ids: list[str] = Field(default_factory=list)  # Skill 白名单，空列表 = 全部启用
    tool_allowlist: list[str] = Field(default_factory=lambda: ["*"])  # 工具白名单 (fnmatch)，["*"]=全部
    max_turns: int = 50                             # LLM 最大对话轮数
    max_tool_calls: int = 70                        # 最大工具调用总次数
    max_time_s: int = 1200                          # 最大墙钟时间（秒），1200s = 20 分钟

class QueryResponse(BaseModel):
    """HTTP API 同步响应体。"""
    session_id: str
    status: str                                     # "ok" | "failed"
    payload: Any = None                             # 成功时的交付数据
    spent: dict[str, Any] = Field(default_factory=dict)  # {"turns": N, "tool_calls": M}
    error: dict[str, Any] | None = None             # {"code": "...", "message": "..."}

# ═══════════════════════════════════════════════════════
# FastAPI 应用
# ═══════════════════════════════════════════════════════
#
# 三个端点：
#   GET  /health            — 负载均衡 / K8s 探活用
#   POST /v1/query          — 同步模式：Agent 完整执行 → HTTP 200 JSON
#   POST /v1/query/stream   — 流式模式：Server-Sent Events，逐 token 推送

app = FastAPI(title="agentcode API", version="0.1.0")

# ── 会话构建器（共享逻辑） ──────────────────────────
#
# _build_agent() 负责将 HTTP 请求参数转换为 AgentRuntime 所需的全部依赖。
# 被 /v1/query 和 /v1/query/stream 共用，避免重复代码。
#
# 构建步骤：
#   1. 加载 MCP 配置文件 → 解析 MCP 服务器列表
#   2. 扫描 Skills 目录 → 发现所有可用 Skill
#   3. 在独立线程中初始化 ToolManager（连接 MCP 服务器）
#      ↑ 必须用 run_in_executor，因为 ToolManager 内部会创建
#        自己的 asyncio 事件循环，与 FastAPI 主循环冲突

async def _build_agent(req: QueryRequest):
    """构建 Agent 运行时上下文（在独立线程中初始化 MCP 连接，避免事件循环冲突）。

    Returns:
        (SessionConfig, SkillLoader, ToolManager) 三元组
    """
    mcp_path = Path(req.mcp_config_path) if req.mcp_config_path else _default_mcp_path()
    if not mcp_path.is_file():
        raise HTTPException(status_code=400, detail=f"MCP config not found: {mcp_path}")

    try:
        config: SessionConfig = build_config_from_mcp_json(str(mcp_path))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to load MCP config: {exc}")

    config.session_id = req.session_id
    config.trace_id = req.trace_id
    config.tool_policy = ToolPolicy(allow=req.tool_allowlist)
    config.budget = BudgetConfig(
        max_turns=req.max_turns,
        max_tool_calls=req.max_tool_calls,
        max_time_s=req.max_time_s,
    )

    skillset_dir = Path(__file__).resolve().parent / "skillsets"
    skill_loader = SkillLoader(skillset_dir=skillset_dir)
    config.skills = list(config.skills) + skill_loader.discover_skills()
    if req.skill_ids:
        config.skills = [s for s in config.skills if s.id in req.skill_ids]

    # MCP 连接内部创建自己的事件循环，必须在独立线程中执行
    def _init_tool_mgr():
        return ToolManager(config=config)

    loop = asyncio.get_running_loop()
    try:
        tool_mgr = await loop.run_in_executor(None, _init_tool_mgr)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MCP connection failed: {exc}")

    return config, skill_loader, tool_mgr

# ── 端点 ────────────────────────────────────────────

@app.get("/health")
async def health():
    """负载均衡 / K8s 健康探针。"""
    return {"status": "ok"}

@app.post("/v1/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    """同步查询：Agent 完整执行完后一次性返回结果。

    适用场景：批处理、需要完整结果的调用方。

    工作流程：
        1. _build_agent() 构建运行时上下文（MCP 连接、Skill 加载）
        2. 创建 _BlockingLLM（非流式）后端
        3. 在独立线程中运行 AgentRuntime.run_session()
        4. Agent 循环结束 → 包装为 QueryResponse 返回
    """
    config, skill_loader, tool_mgr = await _build_agent(req)

    # 使用同步后端：等待 LLM 完整回复，不做流式输出
    llm = _BlockingLLM()

    runtime = AgentRuntime(
        llm_backend=llm,
        tool_manager=tool_mgr,
        trace_sink=NoopTraceSink(),
        skill_loader=skill_loader,
    )

    # 在独立线程中运行 Agent 循环，避免阻塞 FastAPI 事件循环
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, runtime.run_session, config, req.user_message)

    return QueryResponse(
        session_id=req.session_id,
        status=result.status,
        payload=result.payload,
        spent=result.spent,
        error={"code": result.error.code.value, "message": result.error.message} if result.error else None,
    )

@app.post("/v1/query/stream")
async def query_stream(req: QueryRequest):
    """SSE 流式查询：通过 Server-Sent Events 实时推送执行过程。

    适用场景：前端交互、需要展示中间过程的调用方。

    SSE 事件类型（按发生顺序）：
        token       — LLM 逐字输出
        tool_call   — Agent 决定调用工具（含工具名+参数）
        tool_result — 工具执行完成（含结果摘要+耗时）
        result      — Agent 最终交付数据
        error       — 异常
        done        — 流结束

    核心机制：
        后台线程跑 AgentRuntime（同步循环）
          ├─ LLM 流式 token → asyncio.Queue（线程安全）
          └─ 工具调用结果 → on_tool_results 回调
        主线程从 Queue 读取 → yield SSE 事件
    """
    config, skill_loader, tool_mgr = await _build_agent(req)
    queue: asyncio.Queue = asyncio.Queue()

    llm = QueueStreamingLLM(queue=queue)

    # ── 工具结果回调：Agent 每次执行完工具后调用 ──
    # tool_result_events 列表由 on_tool_results 回调填充，
    # 然后由 event_generator 交错插入到 SSE 流中。
    tool_result_events: list[dict[str, Any]] = []
    def on_tool_results(results: list[dict[str, Any]], elapsed_ms: int) -> None:
        """AgentRuntime 每完成一批工具调用后触发此回调。

        将结果截断后存到 tool_result_events 列表中，
        后续由 event_generator 读出发送给 SSE 客户端。
        内容截断到 500 字符，避免单个事件过大。
        """
        for r in results:
            content = r.get("content")
            if isinstance(content, dict):
                summary = json.dumps(content, ensure_ascii=False)
                if len(summary) > 500:
                    summary = summary[:500] + "..."
            elif isinstance(content, str):
                summary = content[:500]
            else:
                summary = str(content)[:500]
            tool_result_events.append({
                "type": "tool_result",
                "data": {"name": r.get("name", ""), "is_error": r.get("is_error", False),
                         "content": summary, "elapsed_ms": elapsed_ms},
            })

    runtime = AgentRuntime(
        llm_backend=llm,
        tool_manager=tool_mgr,
        trace_sink=NoopTraceSink(),
        skill_loader=skill_loader,
        on_tool_results=on_tool_results,
    )

    async def event_generator():
        """异步生成器：从 Queue 读取事件 → 格式化为 SSE → yield。

        由于 AgentRuntime.run_session() 是同步调用（在后台线程中执行），
        这里通过 asyncio.Queue 作为线程安全桥梁：
            - 后台线程：queue.put_nowait(token/工具调用/哨兵)
            - 主线程（这里是 async generator）：queue.get() 读取

        tool_result_events 是 AgentRuntime 通过 on_tool_results 回调
        写入的列表，这里是读→yield。用 tr_idx 指针追踪已发送的位置。
        """
        loop = asyncio.get_running_loop()
        # 用 dict 做盒子：因为闭包不能修改外部 str/int 变量
        result_holder: dict[str, Any] = {}

        def _run():
            """在后台线程执行 Agent 循环。"""
            try:
                result_holder["value"] = runtime.run_session(config, req.user_message)
            except Exception as exc:
                result_holder["error"] = str(exc)
            finally:
                # Agent 循环结束 → 放入哨兵，通知 event_generator 停止等待
                queue.put_nowait(_STREAM_EVENT_SENTINEL)

        # 启动后台 Agent 执行线程
        task = loop.run_in_executor(None, _run)

        tr_idx = 0
        while True:
            # 阻塞等待下一个事件（token / tool_call / 哨兵）
            item = await queue.get()
            if item is _STREAM_EVENT_SENTINEL:
                break     # Agent 循环已结束，停止等待

            # 在 token 之间穿插发送已完成的工具结果
            while tr_idx < len(tool_result_events):
                yield f"event: tool_result\ndata: {json.dumps(tool_result_events[tr_idx], ensure_ascii=False)}\n\n"
                tr_idx += 1

            if item["type"] == "token":
                yield f"event: token\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
            elif item["type"] == "tool_call":
                yield f"event: tool_call\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
            elif item["type"] == "error":
                yield f"event: error\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"

        # 发送剩余未发送的工具结果
        while tr_idx < len(tool_result_events):
            yield f"event: tool_result\ndata: {json.dumps(tool_result_events[tr_idx], ensure_ascii=False)}\n\n"
            tr_idx += 1

        # 等待后台线程结束
        await task

        # 发送最终结果
        if "error" in result_holder:
            yield f"event: error\ndata: {json.dumps({'type': 'error', 'data': result_holder['error']}, ensure_ascii=False)}\n\n"
        elif "value" in result_holder:
            r = result_holder["value"]
            yield f"event: result\ndata: {json.dumps({'type': 'result', 'data': {'status': r.status, 'payload': r.payload, 'spent': r.spent, 'error': {'code': r.error.code.value, 'message': r.error.message} if r.error else None}}, ensure_ascii=False)}\n\n"

        # 流结束标识
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ═══════════════════════════════════════════════════════
# 同步 LLM 后端 (非流式，用于 /v1/query)
# ═══════════════════════════════════════════════════════

class _BlockingLLM:
    """同步 LLMBackend 实现：一次性发起 HTTP 请求，等待完整响应。

    与 QueueStreamingLLM 的区别：
        - stream=False（一次返回完整 JSON）
        - 不做任何 SSE 推送
        - 适合批量/同步场景

    实现了 LLMBackend 协议（与 streaming 版本接口完全一致），
    可以无缝替换到 AgentRuntime 中。
    """

    def __init__(self):
        self._cfg = _load_env()
        self._url = _chat_completions_url(self._cfg["base_url"])

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], cancel_token: Any = None,
    ) -> dict[str, Any]:
        # 格式转换 + 构建非流式请求（stream=False）
        oa_msgs = _messages_to_openai(messages)
        oa_tools = _mcp_tools_to_openai(tools) if tools else []

        payload: dict[str, Any] = {
            "model": self._cfg["model"],
            "messages": oa_msgs,
            "temperature": self._cfg["temperature"],
            "max_tokens": self._cfg["max_tokens"],
            "stream": False,         # ← 关键：非流式，一次返回全部
        }
        if oa_tools:
            payload["tools"] = oa_tools

        headers = {"Content-Type": "application/json"}
        if self._cfg["api_key"]:
            headers["Authorization"] = f"Bearer {self._cfg['api_key']}"

        try:
            with httpx.Client(timeout=self._cfg["timeout_s"]) as client:
                resp = client.post(self._url, json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"LLM API error {exc.response.status_code}: {exc.response.text[:500]}")
        except Exception as exc:
            raise RuntimeError(f"LLM API request failed: {exc}")

        # 解析非流式响应（choices[0].message 中包含完整内容）
        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}

        reasoning = msg.get("reasoning_content", "")
        content_text = msg.get("content") or ""

        # 组装与流式版本一致的返回格式
        blocks: list[dict[str, Any]] = []
        if content_text.strip():
            blocks.append({"type": "text", "text": content_text.strip()})

        tool_calls = msg.get("tool_calls") or []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            fn_name = fn.get("name", "")
            try:
                fn_args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                fn_args = {}
            blocks.append({"type": "tool_use", "id": tc.get("id", ""), "name": fn_name, "input": fn_args})

        result: dict[str, Any] = {"role": "assistant", "content": blocks}
        if reasoning:
            result["reasoning_content"] = reasoning
        return result


# ═══════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════
#
# pyproject.toml 中注册为 agentcode-api 命令，
# 也可直接 python -m agentcode.api

def main():
    """agentcode API 服务启动入口。

    用法:
        agentcode-api --host 0.0.0.0 --port 9180
    """
    import argparse
    parser = argparse.ArgumentParser(description="agentcode HTTP API server")
    parser.add_argument("--host", default="0.0.0.0", help="绑定地址 (默认: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9180, help="端口 (默认: 9180)")
    args = parser.parse_args()

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()