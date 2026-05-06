"""
端到端示例：启动本目录 MCP（后台线程）+ Mock LLM 两轮（先 tool 再 JSON 答案）+ agentcode 主循环。

运行（需在仓库中已安装 agentcode，且 PYTHONPATH 含本包）：

    python -m agentcode.examples.beijing_time.demo_agent

若 MCP 已单独启动（默认 http://127.0.0.1:8765/mcp），可指定：

    python -m agentcode.examples.beijing_time.demo_agent --mcp-url http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

from agentcode import AgentRuntime
from agentcode.mcp import McpClient
from agentcode.models import DeliveryConfig, McpServerConfig, SessionConfig, SkillRef
from agentcode.profiles.time_agent import resolve_default_beijing_skill_path
from agentcode.trace import JsonlTraceSink

from agentcode.mcp_servers.time.server import serve as serve_mcp

_PKG_DIR = Path(__file__).resolve().parent


class _DemoLLM:
    """第一轮请求工具；第二轮在收到 tool_result 后输出符合 delivery 的 JSON。"""

    def __init__(self, tool_full_name: str) -> None:
        self._tool_full_name = tool_full_name
        self._step = 0

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], cancel_token: Any = None
    ) -> dict[str, Any]:
        self._step += 1
        if self._step == 1:
            return {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": self._tool_full_name,
                        "input": {},
                    }
                ],
            }
        payload = {"answer": "已查询 MCP：当前北京时间见工具返回的 beijing_time_display。", "source": "mcp"}
        return {
            "role": "assistant",
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        }


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    _host, port = s.getsockname()
    s.close()
    return port


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mcp-url",
        default="",
        help="若已手动启动 mcp_server，传入完整 URL（如 http://127.0.0.1:8765/mcp）；留空则本脚本自动起服务",
    )
    args = parser.parse_args()

    if args.mcp_url.strip():
        mcp_url = args.mcp_url.strip()
        httpd = None
        thread = None
    else:
        port = _free_port()
        mcp_url = f"http://127.0.0.1:{port}/mcp"
        httpd = serve_mcp("127.0.0.1", port)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.15)

    skill_path = resolve_default_beijing_skill_path()
    if not skill_path:
        raise RuntimeError(
            "未找到 skillsets/common/beijing_time.md；请确认已安装本包（pip install -e agentcode）。"
        )
    config = SessionConfig(
        session_id="beijing-demo",
        trace_id="beijing-demo-trace",
        mcp_servers=[McpServerConfig(name="time", transport="http", url=mcp_url)],
        skills=[SkillRef(id="beijing-time", path=skill_path, inject_layer="developer")],
        tool_policy={"allow": ["time__*"]},
        budget={
            "max_turns": 8,
            "max_tool_calls": 8,
            "max_time_s": 60,
            "max_retries_per_tool": 0,
            "repeat_action_threshold": 3,
        },
        tool_execution="sequential",
        delivery=DeliveryConfig(
            output_schema_id="demo-answer-v1",
            json_schema={
                "type": "object",
                "properties": {
                    "answer": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["answer"],
            },
        ),
    )

    runtime = AgentRuntime(
        llm_backend=_DemoLLM(tool_full_name="time__get_beijing_time"),
        mcp_client=McpClient(config=config),
        trace_sink=JsonlTraceSink(path=str(_PKG_DIR / "demo_trace.jsonl")),
    )
    result = runtime.run_session(config, user_message="现在北京时间几点？")

    print("status:", result.status)
    print("payload:", json.dumps(result.payload, ensure_ascii=False, indent=2))
    print("spent:", result.spent)
    print("trace_ref:", result.trace_ref)
    if result.error:
        print("error:", result.error.model_dump())

    if httpd:
        httpd.shutdown()


if __name__ == "__main__":
    main()
