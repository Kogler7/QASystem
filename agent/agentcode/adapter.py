"""格式适配层 — agentcode 内部格式与 LLM API 格式的转换，以及环境配置。

职责：
  - LLM 环境变量加载
  - MCP 消息格式 ↔ OpenAI Chat Completions 格式互转
  - MCP 工具 schema → OpenAI function calling 格式
  - 路径工具

本模块被 api.py / cli.py / worker.py 三方共享，是项目唯一的格式转换入口。
"""

from __future__ import annotations
from docker.configuration import getenv as config_getenv

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def _load_env() -> dict[str, Any]:
    """从仓库根目录的唯一 .env 加载 Agent LLM 配置。"""
    root_env = Path(__file__).resolve().parents[2] / ".env"
    if root_env.exists():
        load_dotenv(root_env, override=False)
    return {
        "base_url": (config_getenv("AGENT_LLM_BASE_URL") or "http://localhost:9999").strip(),
        "api_key": (config_getenv("AGENT_LLM_API_KEY") or "").strip(),
        "model": (config_getenv("AGENT_LLM_MODEL") or "Qwen/Qwen3-8B").strip(),
        "temperature": float(config_getenv("AGENT_TEMPLATE_TEMPERATURE") or 0.2),
        "max_tokens": int(config_getenv("AGENT_TEMPLATE_MAX_TOKENS") or 4096),
        "timeout_s": int(config_getenv("AGENT_TEMPLATE_TIMEOUT_SECONDS") or 120),
    }


def _chat_completions_url(base_url: str) -> str:
    """将 base_url 标准化为 /v1/chat/completions 完整路径。"""
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _default_mcp_path() -> Path:
    """获取默认 MCP 配置文件路径，优先 AGENT_MCP_CONFIG 环境变量。"""
    for candidate in [
        Path(config_getenv("AGENT_MCP_CONFIG") or ""),
        Path(__file__).resolve().parent / "tools" / "mcp_tools" / "mcp_config.json",
    ]:
        if candidate.is_file():
            return candidate
    return Path(config_getenv("AGENT_MCP_CONFIG") or "mcp_config.json")


def _messages_to_openai(agent_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 agentcode 内部消息列表转换为 OpenAI Chat Completions 格式。"""
    out: list[dict[str, Any]] = []
    for msg in agent_messages:
        role = (msg.get("role") or "user").strip().lower()
        content = msg.get("content", [])
        if role == "developer":
            role = "system"

        if role in ("user", "tool") and isinstance(content, list) and content:
            if all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                for b in content:
                    tid = (b.get("tool_use_id") or b.get("id") or "").strip()
                    if b.get("is_error"):
                        text = json.dumps({"error": b.get("error"), "error_code": b.get("error_code")}, ensure_ascii=False)
                    else:
                        payload = b.get("content")
                        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False) if payload else ""
                    out.append({"role": "tool", "tool_call_id": tid, "content": text})
                continue

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
    """将 MCP 工具 schema 转换为 OpenAI function calling tools 格式。"""
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
