# agentcode HTTP API 文档

agentcode 是一个 MCP Agent 运行时，提供智能问数和问题求解能力。本文档描述如何通过 HTTP API 启动和使用该服务。

---

## 目录

- [1. 启动方式](#1-启动方式)
- [2. 环境变量配置](#2-环境变量配置)
- [3. API 端点一览](#3-api-端点一览)
- [4. 请求参数详解](#4-请求参数详解)
- [5. 响应格式](#5-响应格式)
- [6. SSE 流式事件](#6-sse-流式事件)
- [7. 错误码](#7-错误码)
- [8. 使用示例](#8-使用示例)
- [9. 架构说明](#9-架构说明)

---

## 1. 启动方式

### 安装依赖

```bash
cd agentcode
pip install -e ".[api]"
```

### 方式一：uvicorn 直接启动

```bash
uvicorn agentcode.api:app --host 0.0.0.0 --port 9180
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `0.0.0.0` | 绑定地址 |
| `--port` | `9180` | HTTP 端口 |
| `--reload` | 无 | 可选，开发模式自动重载 |

### 方式二：CLI 入口启动

```bash
agentcode-api --host 0.0.0.0 --port 9180
```

### 验证服务已启动

```bash
curl http://localhost:9180/health
# 返回: {"status":"ok"}
```

---

## 2. 环境变量配置

服务启动时优先加载 `agentcode/.env`，若不存在则回退到 `backend/.env`。

| 变量名 | 必需 | 默认值 | 说明 |
|--------|------|--------|------|
| `LLM_BASE_URL` | 否 | `https://api.deepseek.com` | LLM API 地址（优先于 OPENAI_BASE_URL） |
| `OPENAI_BASE_URL` | 否 | — | 备选 LLM API 地址 |
| `LLM_API_KEY` | 否 | — | API 密钥（优先于 OPENAI_API_KEY） |
| `OPENAI_API_KEY` | 否 | — | 备选 API 密钥 |
| `LLM_MODEL` | 否 | `deepseek-v4-pro` | 模型名称（优先于 OPENAI_MODEL） |
| `OPENAI_MODEL` | 否 | — | 备选模型名称 |
| `AGENT_TEMPLATE_TEMPERATURE` | 否 | `0.2` | LLM 温度参数 |
| `AGENT_TEMPLATE_MAX_TOKENS` | 否 | `4096` | LLM 单次最大输出 token |
| `AGENT_TEMPLATE_TIMEOUT_SECONDS` | 否 | `120` | LLM API 调用超时（秒） |
| `COMPACT_TOKEN_THRESHOLD` | 否 | `100000` | 触发上下文压缩的 token 阈值（运行时） |

### .env 示例

```bash
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_MODEL=deepseek-v4-pro
AGENT_TEMPLATE_TEMPERATURE=0.2
AGENT_TEMPLATE_MAX_TOKENS=4096
AGENT_TEMPLATE_TIMEOUT_SECONDS=120
```

### MCP 配置文件

服务需要 MCP 配置文件来连接工具服务器。默认路径为 `agentcode/agentcode/tools/mcp_tools/mcp_config.json`，可通过请求参数 `mcp_config_path` 覆盖。

MCP 配置文件格式：

```json
{
  "mcpServers": {
    "sql-search": {
      "transport": "streamable_http",
      "url": "http://10.126.62.88:8889/mcp"
    },
    "intent-recognition": {
      "transport": "streamable_http",
      "url": "http://10.126.62.88:8891/mcp"
    },
    "information-get": {
      "transport": "streamable_http",
      "url": "http://10.126.62.88:51006/mcp"
    },
    "draw": {
      "transport": "streamable_http",
      "url": "http://10.126.62.88:8890/mcp"
    }
  }
}
```

已加载的 MCP 工具按服务分组：

| MCP 服务 | 功能 | 说明 |
|----------|------|------|
| `intent-recognition` | 意图识别 | 根据用户问题识别业务意图 |
| `information-get` | 知识图谱查询 | 实体提取、实体链接、属性查询、图搜索、业务规则 |
| `sql-search` | SQL 执行 | 对配置的数据源执行 SQL 查询 |
| `draw` | 数据可视化 | 生成柱状图、饼图、折线图、散点图、直方图、雷达图 |
| `base` | 命令行 | 在沙箱环境中执行受控命令行（内置安全黑名单） |

---

## 3. API 端点一览

| 方法 | 路径 | Content-Type | 说明 |
|------|------|-------------|------|
| `GET` | `/health` | — | 健康检查 |
| `POST` | `/v1/query` | `application/json` | 同步查询，返回完整结果 |
| `POST` | `/v1/query/stream` | `application/json` | SSE 流式查询，实时返回 token/工具调用 |

---

## 4. 请求参数详解

### QueryRequest

所有请求体字段：

```json
{
  "user_message": "string (必需)",
  "session_id": "string (可选，默认自动生成 UUID)",
  "trace_id": "string (可选，默认自动生成 UUID)",
  "mcp_config_path": "string|null (可选，默认加载内置 mcp_config.json)",
  "skill_ids": ["string (可选，过滤启用的 Skill)"],
  "tool_allowlist": ["string (可选，默认 [\"*\"])"],
  "max_turns": "int (可选，默认 30)",
  "max_tool_calls": "int (可选，默认 50)",
  "max_time_s": "int (可选，默认 600)"
}
```

| 字段 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|--------|------|
| `user_message` | `string` | ✅ | — | 用户输入的自然语言问题 |
| `session_id` | `string` | 否 | `uuid4().hex` | 会话 ID，用于追踪 |
| `trace_id` | `string` | 否 | `uuid4().hex` | 链路追踪 ID |
| `mcp_config_path` | `string\|null` | 否 | 内置配置 | MCP 配置文件路径，默认 `tools/mcp_tools/mcp_config.json` |
| `skill_ids` | `list[string]` | 否 | `[]` | Skill 白名单过滤；为空则加载全部发现的 Skill |
| `tool_allowlist` | `list[string]` | 否 | `["*"]` | 工具白名单（fnmatch 通配符），`["*"]` 表示全部允许 |
| `max_turns` | `int` | 否 | `30` | 最大 LLM 对话轮数 |
| `max_tool_calls` | `int` | 否 | `50` | 最大工具调用次数 |
| `max_time_s` | `int` | 否 | `600` | 最大墙钟时间（秒） |

### tool_allowlist 示例

```json
// 只允许 sql-search 和 draw 工具
{"tool_allowlist": ["sql-search__*", "draw__*"]}

// 只允许意图识别
{"tool_allowlist": ["intent-recognition__*"]}

// 全部允许（默认）
{"tool_allowlist": ["*"]}
```

---

## 5. 响应格式

### GET /health

**响应 200：**

```json
{"status": "ok"}
```

---

### POST /v1/query — 同步查询

**请求示例：**

```bash
curl -X POST http://localhost:9180/v1/query \
  -H "Content-Type: application/json" \
  -d '{
    "user_message": "查询上个月的故障率",
    "session_id": "sess-001",
    "max_turns": 20
  }'
```

**响应 200 — 成功：**

```json
{
  "session_id": "sess-001",
  "status": "ok",
  "payload": { /* 交付数据，格式由 delivery schema 决定 */ },
  "spent": {
    "turns": 5,
    "tool_calls": 8
  },
  "error": null
}
```

**响应 200 — 失败：**

```json
{
  "session_id": "sess-001",
  "status": "failed",
  "payload": null,
  "spent": {
    "turns": 30,
    "tool_calls": 50
  },
  "error": {
    "code": "BUDGET_EXCEEDED",
    "message": "Turn budget exceeded"
  }
}
```

**响应 400 — 请求错误：**

```json
{"detail": "MCP config not found: /path/to/mcp.json"}
```

**响应 503 — MCP 连接失败：**

```json
{"detail": "MCP connection failed: ..."}
```

### QueryResponse 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | `string` | 会话 ID |
| `status` | `"ok" \| "failed"` | 最终状态 |
| `payload` | `any \| null` | 成功时的交付数据 |
| `spent` | `object` | 消耗统计 |
| `spent.turns` | `int` | LLM 调用轮数 |
| `spent.tool_calls` | `int` | 工具调用总次数 |
| `error` | `object \| null` | 失败时的错误信息 |
| `error.code` | `string` | 错误码 |
| `error.message` | `string` | 错误描述 |

---

## 6. SSE 流式事件

### POST /v1/query/stream — SSE 流式查询

**请求示例：**

```bash
curl -N -X POST http://localhost:9180/v1/query/stream \
  -H "Content-Type: application/json" \
  -d '{
    "user_message": "查询上个月的故障率",
    "max_turns": 20
  }'
```

### 响应头

| 响应头 | 值 | 说明 |
|--------|-----|------|
| `Content-Type` | `text/event-stream` | SSE 流 |
| `Cache-Control` | `no-cache` | 禁止缓存 |
| `X-Accel-Buffering` | `no` | 禁用 nginx 缓冲 |

### 事件类型

共 6 种 SSE 事件类型，按典型收到顺序排列：

#### (1) `token` — LLM 逐字输出

```json
event: token
data: {"type":"token","data":"正在分析您的问题..."}
```

收到 LLM 流式返回的每个文本片段。一个 assistant 消息由多个 token 事件组成。

#### (2) `tool_call` — 工具调用

```json
event: tool_call
data: {
  "type": "tool_call",
  "data": {
    "calls": [
      {
        "name": "intent-recognition__recognize_intent",
        "input": {"query": "查询上个月的故障率"}
      }
    ]
  }
}
```

LLM 决定调用工具时触发。`calls` 数组可能包含多个并发工具调用。

#### (3) `tool_result` — 工具返回结果

```json
event: tool_result
data: {
  "type": "tool_result",
  "data": {
    "name": "intent-recognition__recognize_intent",
    "is_error": false,
    "content": "{\"intent\": \"fault_rate_query\", \"confidence\": 0.95}",
    "elapsed_ms": 234
  }
}
```

工具执行完成后触发。`is_error: true` 表示工具调用失败。

#### (4) `result` — Agent 最终结果

```json
event: result
data: {
  "type": "result",
  "data": {
    "status": "ok",
    "payload": { /* 交付数据 */ },
    "spent": {"turns": 5, "tool_calls": 8},
    "error": null
  }
}
```

Agent loop 正常结束时触发。

#### (5) `error` — 异常

```json
event: error
data: {"type":"error","data":"LLM API error 429: Rate limit exceeded"}
```

LLM API 调用或运行时异常时触发。收到此事件后 SSE 流可能继续或终止。

#### (6) `done` — 流结束

```json
event: done
data: {}
```

SSE 流结束标志，此后无更多事件。

### 完整 SSE 流时序图

```
时间 →

event: token      LLM 开始回答
event: token      ...答案流式输出...
event: token
event: tool_call   LLM 决定调用工具
event: tool_result 工具执行完成，返回结果
event: token      LLM 基于工具结果继续回答
event: token
event: tool_call   LLM 再次调用工具
event: tool_result
event: token      最终答案
event: result      最终结果（status/spent）
event: done        流结束
```

--- 一个 Agent loop 可能包含多轮 token→tool_call→tool_result 循环 —

---

## 7. 错误码

| 错误码 | 说明 | 触发条件 |
|--------|------|---------|
| `BUDGET_EXCEEDED` | 预算超限 | 达到 `max_turns`、`max_tool_calls` 或 `max_time_s` |
| `CONSTRAINT_VIOLATION` | 约束违规 | 尝试调用白名单之外的工具 |
| `DELIVERY_NOT_SATISFIED` | 交付格式不符 | LLM 输出不满足 delivery JSON Schema |
| `REPEAT_ACTION_DETECTED` | 重复操作 | 连续 N 次完全相同工具调用 |
| `HTTP 400` | 请求错误 | MCP 配置不存在或格式错误 |
| `HTTP 503` | 服务不可用 | 无法连接 MCP 服务器 |

---

## 8. 使用示例

### 8.1 同步查询

```bash
curl -X POST http://localhost:9180/v1/query \
  -H "Content-Type: application/json" \
  -d '{"user_message": "帮我查一下北京地铁1号线最近一周的故障情况"}'
```

### 8.2 流式查询

```bash
curl -N -X POST http://localhost:9180/v1/query/stream \
  -H "Content-Type: application/json" \
  -d '{"user_message": "分析上半年各线路的故障趋势，画一张折线图", "max_turns": 50, "max_time_s": 300}'
```

### 8.3 Python 客户端示例

```python
import httpx
import json

# 同步查询
def sync_query(message: str) -> dict:
    with httpx.Client(timeout=120) as client:
        resp = client.post(
            "http://localhost:9180/v1/query",
            json={"user_message": message},
        )
        return resp.json()

# 流式查询
def stream_query(message: str):
    with httpx.Client(timeout=300) as client:
        with client.stream(
            "POST",
            "http://localhost:9180/v1/query/stream",
            json={"user_message": message},
        ) as resp:
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    print(f"[{event_type}] {data}")
```

### 8.4 纯聊天模式（无 MCP 工具）

如果请求中指定的 MCP 配置文件指向的服务器不可用，服务会自动降级为纯聊天模式，使用内置的 Skill 指令（如 `smart_query`）指导 LLM 工作流。

---

## 9. 架构说明

### 9.1 请求处理流程

```
HTTP 请求
  │
  ├─ /health ─── 直接返回 {"status":"ok"}
  │
  └─ /v1/query 或 /v1/query/stream
       │
       ├─ 1. 加载 MCP 配置 → 连接所有 MCP 服务器
       ├─ 2. 发现并加载 Skill
       ├─ 3. 构建系统提示词 (prompt.py)
       ├─ 4. 创建 AgentRuntime
       │     ├── LLMBackend (流式或同步)
       │     ├── ToolManager (MCP + Shell)
       │     └── SkillLoader
       │
       ├─ 5. Agent Runtime Loop
       │     ┌─────────────────────────┐
       │     │ while (budget 未超) {   │
       │     │   LLM 推理              │
       │     │   → 工具调用? 执行工具  │
       │     │   → 最终答案? 返回结果  │
       │     │   → token 超限? 压缩    │
       │     │ }                      │
       │     └─────────────────────────┘
       │
       └─ 6. 返回 FinalResult (同步) / SSE 流 (流式)
```

### 9.2 流式实现核心机制

```
FastAPI event loop (主线程)
    │
    ├─ run_in_executor ──→ 后台线程
    │                       │
    │                       ├─ QueueStreamingLLM.complete()
    │                       │    │ 调用 LLM API (stream=True)
    │                       │    │ 每收到 token → queue.put_nowait({"type":"token","data":...})
    │                       │    ↓ 返回 assistant 消息
    │                       │
    │                       ├─ AgentRuntime._run_loop()
    │                       │    │ 发送 tool_call 事件
    │                       │    │ 执行工具 → on_tool_results 回调
    │                       │    ↓ 下一轮 LLM.complete()...
    │                       │
    │                       └─ run_session() 结束 → queue.put_nowait(SENTINEL)
    │
    ├─ async for item in queue ──→ SSE event: token / tool_call / tool_result / result / error / done
    │
    └─ StreamingResponse(event_generator())
```

- `asyncio.Queue.put_nowait()` 是线程安全的，确保后台线程和主事件循环之间的数据传递
- 工具调用事件来自 LLM 返回消息的解析
- 工具结果事件来自 `AgentRuntime` 的 `on_tool_results` 回调

### 9.3 相关文件

| 文件 | 职责 |
|------|------|
| `agentcode/api.py` | HTTP API 服务主体 |
| `agentcode/runtime.py` | Agent 执行循环 |
| `agentcode/prompt.py` | 系统提示词构建 |
| `agentcode/models.py` | 数据模型（SessionConfig, FinalResult 等） |
| `agentcode/interfaces.py` | LLMBackend / TraceSink 协议 |
| `agentcode/tools/mcp_tools/tool_manager.py` | MCP 工具注册与调度 |
| `agentcode/tools/mcp_tools/mcp_config.json` | MCP 服务器配置 |
| `agentcode/skillsets/` | Skill 定义目录 |
