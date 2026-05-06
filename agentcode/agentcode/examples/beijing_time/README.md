# 示例：北京时间 MCP + Skill（兼容入口）

> 当前生产化目录已迁移到 `agentcode/mcp_servers/time`、`agentcode/skillsets/common`、`agentcode/profiles`。本目录保留为兼容入口与文档示例。

## 内容

| 文件 | 说明 |
|------|------|
| `mcp_server.py` | 兼容入口，转发至 `agentcode.mcp_servers.time.server`。 |
| `demo_agent.py` | 可选自动起 MCP + Mock LLM 跑通 `AgentRuntime`（两轮：tool → 最终 JSON）。 |
| Skill 正文 | 唯一来源：`agentcode/skillsets/common/beijing_time.md`（随包安装）。 |

## 手动启动 MCP

```bash
cd /path/to/qasystem/backend
source .venv/bin/activate
pip install -e ../agentcode
python -m agentcode.examples.beijing_time.mcp_server
```

默认：`http://127.0.0.1:8765/mcp`。

## 一键演示（含内置 Mock LLM）

```bash
PYTHONPATH=/path/to/qasystem/agentcode python -m agentcode.examples.beijing_time.demo_agent
```

或 MCP 已启动时：

```bash
PYTHONPATH=... python -m agentcode.examples.beijing_time.demo_agent --mcp-url http://127.0.0.1:8765/mcp
```

## 接到真实模型（本仓库 backend）

在 **`backend/.env`** 中设置（与 `backend/.env.example` 一致）：

- `LLM_BACKEND=openai`
- `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`（若需要）
- `AGENT_MCP_TIME_URL=http://127.0.0.1:8765/mcp`（或 `MCP_BEIJING_TIME_URL`）

启动 MCP 与 `uvicorn` 后，请求 **`GET /api/agent-template/config`** 应看到 `mcp_time_configured: true`；再 **`POST /api/agent-template/brief`**，`body: {"query":"现在北京时间几点？"}`。模型需支持 **OpenAI 风格的 `tools` / `tool_calls`**。

## 接到其它宿主

在 FastAPI 中把 `SessionConfig.mcp_servers` 的 `url` 指向上面的 `/mcp`，`tool_policy.allow` 含 `time__*`；`LLMBackend` 需把 MCP 工具转成 OpenAI `tools` 并把 `tool_calls` 与 `role=tool` 消息写回对话（可参考 `agentcode.integrations.fastapi.agent_template`）。
