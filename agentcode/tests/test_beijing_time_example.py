"""集成：北京时间 MCP HTTP 服务 + AgentRuntime + Mock LLM。"""

from __future__ import annotations

import threading
import time

import httpx

from agentcode import AgentRuntime
from agentcode.mcp import McpClient
from agentcode.models import DeliveryConfig, McpServerConfig, SessionConfig, SkillRef
from agentcode.profiles import resolve_default_beijing_skill_path
from agentcode.trace import JsonlTraceSink

from agentcode.examples.beijing_time.demo_agent import _DemoLLM, _free_port
from agentcode.examples.beijing_time.mcp_server import serve as serve_mcp


def test_beijing_time_mcp_roundtrip(tmp_path):
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    httpd = serve_mcp("127.0.0.1", port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.15)

    try:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        with httpx.Client(timeout=5) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
        assert "result" in data
        names = [t["name"] for t in data["result"]["tools"]]
        assert "get_beijing_time" in names

        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_beijing_time", "arguments": {}}}
        with httpx.Client(timeout=5) as client:
            r2 = client.post(url, json=call)
            r2.raise_for_status()
            res = r2.json()["result"]
        assert "beijing_time_iso" in res
        assert "Asia/Shanghai" in res.get("timezone", "")
    finally:
        httpd.shutdown()


def test_beijing_time_demo_agent_session(tmp_path):
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    httpd = serve_mcp("127.0.0.1", port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.15)

    try:
        skill_path = resolve_default_beijing_skill_path()
        assert skill_path, "skillsets/common/beijing_time.md 应随包安装"
        config = SessionConfig(
            session_id="t",
            trace_id="t1",
            mcp_servers=[McpServerConfig(name="time", transport="http", url=url)],
            skills=[SkillRef(id="beijing-time", path=skill_path, inject_layer="developer")],
            tool_policy={"allow": ["time__*"]},
            budget={
                "max_turns": 8,
                "max_tool_calls": 8,
                "max_time_s": 60,
                "max_retries_per_tool": 0,
                "repeat_action_threshold": 3,
            },
            delivery=DeliveryConfig(
                output_schema_id="demo-answer-v1",
                json_schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}, "source": {"type": "string"}},
                    "required": ["answer"],
                },
            ),
        )
        runtime = AgentRuntime(
            llm_backend=_DemoLLM("time__get_beijing_time"),
            mcp_client=McpClient(config=config),
            trace_sink=JsonlTraceSink(path=str(tmp_path / "t.jsonl")),
        )
        result = runtime.run_session(config, "现在北京时间？")
        assert result.status == "ok"
        assert isinstance(result.payload, dict)
        assert "answer" in result.payload
    finally:
        httpd.shutdown()
