"""
agentcode CLI — 类 Claude Code 的交互式命令行。

用法:
    agentcode                       交互式 REPL（自动加载 skills/ 下所有 skill）
    agentcode -p "查一下数据库"      单次查询
    agentcode -c /path/to/mcp.json  指定 MCP 配置文件
"""

from __future__ import annotations

import argparse
import json
import os
import readline  # noqa: F401 — enables input() line editing
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from .tools.mcp_tools.tool_manager import ToolManager
from .models import SessionConfig, ToolPolicy
from .tools.mcp_tools.mcp_loader import build_config_from_mcp_json
from .skillsets.loader import SkillLoader
from .interfaces import LLMBackend
from .adapter import _load_env, _chat_completions_url, _default_mcp_path, _messages_to_openai, _mcp_tools_to_openai


_C = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "bold": "\033[1m",
    "magenta": "\033[35m",
}


def _cprint(color: str, *args: Any, **kwargs: Any) -> None:
    """打印带颜色的文本。"""
    text = " ".join(str(a) for a in args)
    end = kwargs.get("end", "\n")
    flush = kwargs.get("flush", False)
    sys.stdout.write(f"{_C[color]}{text}{_C['reset']}{end}")
    if flush:
        sys.stdout.flush()


def _dim(text: str) -> str:
    return f"{_C['dim']}{text}{_C['reset']}"



class _StreamingLLM(LLMBackend):
    """流式 LLM 后端：逐 token 打印，同时返回完整 assistant 消息。"""

    def __init__(self) -> None:
        self.cfg = _load_env()
        self.url = _chat_completions_url(self.cfg["base_url"])

    def stream_complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        oa_msgs = _messages_to_openai(messages)
        oa_tools = _mcp_tools_to_openai(tools) if tools else []

        payload: dict[str, Any] = {
            "model": self.cfg["model"],
            "messages": oa_msgs,
            "temperature": self.cfg["temperature"],
            "max_tokens": self.cfg["max_tokens"],
            "stream": True,
        }
        if oa_tools:
            payload["tools"] = oa_tools

        headers = {"Content-Type": "application/json"}
        if self.cfg["api_key"]:
            headers["Authorization"] = f"Bearer {self.cfg['api_key']}"

        accumulated_text = ""
        accumulated_reasoning = ""
        accumulated_tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason = ""

        try:
            with httpx.Client(timeout=self.cfg["timeout_s"]) as client:
                from external_services.llm.service import model_call
                with model_call(self.cfg.get('service_snapshot'), payload,
                                timeout=self.cfg['timeout_s']) as remaining, client.stream("POST", self.url, json=payload, headers=headers, timeout=remaining) as resp:
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
                        finish_reason = choice.get("finish_reason") or finish_reason

                        if "reasoning_content" in delta and delta["reasoning_content"]:
                            accumulated_reasoning += delta["reasoning_content"]

                        if "content" in delta and delta["content"]:
                            text = delta["content"]
                            accumulated_text += text
                            sys.stdout.write(f"{_C['green']}{text}{_C['reset']}")
                            sys.stdout.flush()

                        # 同 worker.py：网关可能显式下发 "tool_calls": null
                        for tc in (delta.get("tool_calls") or []):
                            idx = tc.get("index", 0)
                            if idx not in accumulated_tool_calls:
                                accumulated_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                            if "id" in tc and tc["id"]:
                                accumulated_tool_calls[idx]["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                accumulated_tool_calls[idx]["name"] += fn["name"]
                            if fn.get("arguments"):
                                accumulated_tool_calls[idx]["arguments"] += fn["arguments"]
        except httpx.HTTPStatusError as exc:
            try:
                body = exc.response.read().decode("utf-8")[:500]
            except Exception:
                body = "(unable to read response body)"
            _cprint("red", f"\n✗ HTTP {exc.response.status_code}: {body}")
            raise
        except Exception as exc:
            _cprint("red", f"\n✗ 请求失败: {exc}")
            raise

        if accumulated_text:
            print()

        blocks: list[dict[str, Any]] = []
        if accumulated_text.strip():
            blocks.append({"type": "text", "text": accumulated_text.strip()})

        if accumulated_tool_calls:
            print()
            for idx in sorted(accumulated_tool_calls.keys()):
                tc = accumulated_tool_calls[idx]
                fn_name = tc["name"]
                try:
                    fn_args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    fn_args = {}
                _cprint("cyan", f"🔧 调用工具: {fn_name}")
                if fn_args:
                    _cprint("dim", f"   └─ 输入: {json.dumps(fn_args, ensure_ascii=False, indent=2)}")
                blocks.append({"type": "tool_use", "id": tc["id"] or f"call_{idx}", "name": fn_name, "input": fn_args})

        result: dict[str, Any] = {"role": "assistant", "content": blocks}
        if accumulated_reasoning:
            result["reasoning_content"] = accumulated_reasoning
        return result

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], cancel_token: Any = None) -> dict[str, Any]:
        return self.stream_complete(messages, tools)


class Conversation:
    """维护多轮对话上下文，agent 循环委托给 AgentRuntime。"""

    def __init__(self, config: SessionConfig, skill_loader: SkillLoader) -> None:
        self.config = config
        self.llm = _StreamingLLM()
        self.skill_loader = skill_loader
        self.tool_mgr: ToolManager | None = None
        self.tools: list[dict[str, Any]] = []
        try:
            self.tool_mgr = ToolManager(config=config)
            self.tools = self.tool_mgr.list_tools()
        except Exception as exc:
            _cprint("yellow", f"  ⚠ MCP 服务不可用, 以纯聊天模式运行: {exc}\n")

    def send(self, user_message: str) -> None:
        if not self.tool_mgr:
            self._chat_no_tools(user_message)
            return

        from .runtime import AgentRuntime
        from .trace import NoopTraceSink

        runtime = AgentRuntime(
            llm_backend=self.llm,
            tool_manager=self.tool_mgr,
            trace_sink=NoopTraceSink(),
            skill_loader=self.skill_loader,
            on_tool_results=self._on_tool_results,
        )
        try:
            result = runtime.run_session(self.config, user_message)
        except Exception as exc:
            _cprint("red", f"\n  ✗ 运行时错误: {exc}")
            return
        self._display_result(result)

    @staticmethod
    def _on_tool_results(results: list[dict[str, Any]], elapsed_ms: int) -> None:
        for result in results:
            if result.get("is_error"):
                _cprint("red", f"   ✗ 错误: {result.get('error', 'unknown')}")
            else:
                content = result.get("content")
                if isinstance(content, dict):
                    summary = json.dumps(content, ensure_ascii=False)
                    if len(summary) > 300:
                        summary = summary[:300] + "..."
                    _cprint("yellow", f"   ✓ 结果: {summary}")
                elif isinstance(content, str):
                    _cprint("yellow", f"   ✓ 结果: {content[:300]}")
                else:
                    _cprint("yellow", f"   ✓ 结果已返回")
        _cprint("dim", f"   ⏱ {elapsed_ms}ms")

    def _chat_no_tools(self, user_message: str) -> None:
        layers = self.skill_loader.resolve(self.config.skills)
        metadata_text = self.skill_loader.build_metadata_text(self.config.skills)
        msgs: list[dict[str, Any]] = []
        system_text = "\n\n".join(filter(None, [
            "You are a single-agent runtime executor.",
            metadata_text,
            *layers["system"],
        ]))
        dev_text = "\n\n".join(filter(None, [
            "Respect session tool policy, budget and delivery schema.",
            *layers["developer"],
        ]))
        task_text = "\n\n".join(layers["task"]).strip()
        if system_text:
            msgs.append({"role": "system", "content": [{"type": "text", "text": system_text}]})
        if dev_text:
            msgs.append({"role": "developer", "content": [{"type": "text", "text": dev_text}]})
        if task_text:
            msgs.append({"role": "developer", "content": [{"type": "text", "text": task_text}]})
        msgs.append({"role": "user", "content": [{"type": "text", "text": user_message}]})
        self.llm.complete(msgs, [])

    @staticmethod
    def _display_result(result: Any) -> None:
        if result.status == "failed":
            err = result.error
            if err:
                _cprint("red", f"\n  ✗ [{err.code.value}] {err.message}")

    def clear(self) -> None:
        _cprint("dim", "对话已清除（每次查询均为独立会话）")



def _print_banner(config: SessionConfig, mcp_path: Path) -> None:
    cfg = _load_env()
    _cprint("bold", f"\n  agentcode v0.3.0  —  MCP Agent 运行时\n")
    _cprint("dim", f"  模型: {cfg['model']}  |  MCP: {mcp_path}  |  {len(config.mcp_servers)} 个服务\n")
    for s in config.mcp_servers:
        _cprint("dim", f"    • {s.name} → {s.url}")
    if config.skills:
        _cprint("dim", f"  Skill ({len(config.skills)}):")
        for sk in config.skills:
            _cprint("dim", f"    • {sk.name or sk.id}  [{sk.inject_layer}]")
    _cprint("dim", "\n  输入 /help 查看命令, /quit 退出, Ctrl+C 中断\n")


def _print_help() -> None:
    print(f"""
{_C['bold']}命令:{_C['reset']}
  {_C['cyan']}/help{_C['reset']}        显示帮助
  {_C['cyan']}/tools{_C['reset']}       列出可用 MCP 工具
  {_C['cyan']}/skills{_C['reset']}      列出已加载 Skill
  {_C['cyan']}/config{_C['reset']}      显示当前配置
  {_C['cyan']}/clear{_C['reset']}       清除对话历史
  {_C['cyan']}/quit, /exit{_C['reset']}  退出

{_C['bold']}快捷键:{_C['reset']}
  {_C['cyan']}Ctrl+C{_C['reset']}       中断当前回答
  {_C['cyan']}Ctrl+D{_C['reset']}       退出
""")


def interactive_mode(config: SessionConfig, skill_loader: SkillLoader) -> None:
    _print_banner(config, _default_mcp_path())

    conv = Conversation(config, skill_loader)

    while True:
        try:
            user_input = input(f"\n{_C['cyan']}› {_C['reset']}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            cmd = user_input.lower().split()[0]
            if cmd in ("/quit", "/exit"):
                _cprint("dim", "再见 👋")
                break
            elif cmd == "/help":
                _print_help()
            elif cmd == "/tools":
                if not conv.tool_mgr:
                    _cprint("dim", "  MCP 未连接")
                else:
                    tools = conv.tool_mgr.list_tools()
                    if not tools:
                        _cprint("dim", "  没有可用工具")
                    else:
                        for t in tools:
                            _cprint("cyan", f"\n  • {t['name']}")
                            _cprint("dim", f"    {t.get('description', '(无描述)')[:120]}")
                        print()
            elif cmd == "/skills":
                if not config.skills:
                    _cprint("dim", "  没有加载 Skill")
                else:
                    for sk in config.skills:
                        _cprint("cyan", f"\n  • {sk.name or sk.id}")
                        _cprint("dim", f"    id: {sk.id}  注入层: {sk.inject_layer}")
                        if sk.description:
                            _cprint("dim", f"    {sk.description[:120]}")
                        if sk.path:
                            _cprint("dim", f"    路径: {sk.path}")
                        print()
            elif cmd == "/config":
                cfg = _load_env()
                _cprint("dim", f"  模型: {cfg['model']}")
                _cprint("dim", f"  地址: {cfg['base_url']}")
                _cprint("dim", f"  MCP:  {_default_mcp_path()} ({len(config.mcp_servers)} 个服务)")
                _cprint("dim", f"  预算: 最多 {config.budget.max_turns} 轮, {config.budget.max_tool_calls} 次工具调用")
            elif cmd == "/clear":
                conv.clear()
            else:
                _cprint("yellow", f"  未知命令: {cmd}，输入 /help 查看帮助")
            continue

        try:
            conv.send(user_input)
        except KeyboardInterrupt:
            _cprint("dim", "\n  已中断")
            continue
        except Exception as exc:
            _cprint("red", f"\n  ✗ 错误: {exc}")
            continue


def one_shot_mode(config: SessionConfig, skill_loader: SkillLoader, query: str) -> None:
    conv = Conversation(config, skill_loader)
    try:
        conv.send(query)
        print()
    except KeyboardInterrupt:
        _cprint("dim", "\n已中断")
    except Exception as exc:
        _cprint("red", f"\n✗ 错误: {exc}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="agentcode — MCP Agent 运行时 CLI")
    parser.add_argument("-p", "--prompt", type=str, default="", help="单次查询（非交互模式）")
    parser.add_argument("-c", "--mcp-config", type=str, default="", help="MCP 配置文件路径")
    parser.add_argument("query", nargs="*", help="单次查询（直接跟问题）")
    args = parser.parse_args()

    mcp_path = Path(args.mcp_config) if args.mcp_config else _default_mcp_path()
    if not mcp_path.is_file():
        _cprint("red", f"✗ MCP 配置文件不存在: {mcp_path}")
        _cprint("dim", "  设置 AGENT_MCP_CONFIG 环境变量 或 在 tools/mcp_tools/mcp_config.json 放置配置")
        sys.exit(1)

    try:
        config = build_config_from_mcp_json(str(mcp_path))
    except Exception as exc:
        _cprint("red", f"✗ 加载 MCP 配置失败: {exc}")
        sys.exit(1)

    if config.tool_policy.allow:
        pass
    else:
        config.tool_policy = ToolPolicy(allow=["*"])

    skill_loader = SkillLoader()
    config.skills = list(config.skills) + skill_loader.discover_skills()

    query = args.prompt or " ".join(args.query) if args.query else ""

    if query:
        one_shot_mode(config, skill_loader, query)
    else:
        interactive_mode(config, skill_loader)


if __name__ == "__main__":
    main()
