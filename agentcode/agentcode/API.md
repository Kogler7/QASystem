# agentcode HTTP API 文档

agentcode 是一个 MCP Agent 运行时，提供智能问数和问题求解能力。本文档描述如何通过 HTTP API 启动和使用该服务。

---

## 目录

- [1. 启动方式](#1-启动方式)
- [2. 并发模型](#2-并发模型)
- [3. 环境变量配置](#3-环境变量配置)
- [4. API 端点一览](#4-api-端点一览)
- [5. 请求参数详解](#5-请求参数详解)
- [6. 响应格式](#6-响应格式)
- [7. SSE 流式事件](#7-sse-流式事件)
- [8. 错误码](#8-错误码)
- [9. 使用示例](#9-使用示例)
- [10. 架构说明](#10-架构说明)

---

## 1. 启动方式

### 安装依赖

```bash
cd agentcode
pip install -e ".[api]"        # API 服务（内嵌 worker，推荐）
pip install -e ".[worker]"     # 独立 Worker（可选，仅高级部署需要）
```

### 一键启动（推荐）

```bash
agentcode-api
```

服务启动后自动初始化 ToolManager 连接池并拉起工作线程，无需额外启动 worker 进程。

```
API worker: pool 4/4 就绪
API worker: 发现 2 个 skill
API worker: 4 个工作线程已启动, pool_size=4
INFO:     Uvicorn running on http://0.0.0.0:9180
```

### CLI 参数

| 参数 | 环境变量 | 默认值 | 说明 |
|------|---------|--------|------|
| `--host` | — | `0.0.0.0` | 绑定地址 |
| `--port` | — | `9180` | HTTP 端口 |
| `--workers` | `AGENT_API_WORKERS` | `1` | uvicorn worker 进程数（多进程模式需 Redis） |
| `--concurrency` | `AGENT_WORKER_CONCURRENCY` | `4` | 每进程工作线程数 |
| `--pool-size` | `AGENT_WORKER_POOL_SIZE` | `=concurrency` | ToolManager 连接池大小 |

### 启动示例

```bash
# 开发环境（单进程，可使用内存队列）
agentcode-api

# 单进程高并发
agentcode-api --concurrency 8

# 生产环境：2 进程 × 8 线程 = 16 并发（需 Redis）
agentcode-api --workers 2 --concurrency 8

# 高吞吐：4 进程 × 16 线程 = 64 并发（需 Redis）
agentcode-api --workers 4 --concurrency 16
```

### 独立 Worker 模式（高级）

API 和 Worker 分开部署时使用：

```bash
# 终端 1：仅 HTTP 入队（无 worker 线程）
AGENT_WORKER_CONCURRENCY=0 agentcode-api --port 9180

# 终端 2：独立 worker 进程
agentcode-worker --workers 2 --concurrency 8
```

| 参数 | 环境变量 | 默认值 | 说明 |
|------|---------|--------|------|
| `--workers` | `AGENT_WORKER_WORKERS` | `1` | Worker 进程数 |
| `--concurrency` | `AGENT_WORKER_CONCURRENCY` | `4` | 每进程工作线程数 |
| `--pool-size` | `AGENT_WORKER_POOL_SIZE` | `=concurrency` | ToolManager 连接池大小 |

---

## 2. 并发模型

### 默认模式（API 内嵌 worker）

```
agentcode-api --workers 2 --concurrency 8

uvicorn 主进程
  ├── API worker 进程 0 (pid=1001)
  │     ├── lifespan → 初始化 ToolManagerPool × 8
  │     ├── 线程 0: dequeue → borrow → execute → update → 循环
  │     ├── 线程 1: dequeue → borrow → execute → update → 循环
  │     ├── ...
  │     └── 线程 7: dequeue → borrow → execute → update → 循环
  │
  ├── API worker 进程 1 (pid=1002)
  │     ├── lifespan → 初始化 ToolManagerPool × 8
  │     └── 线程 0..7 ...
  │
  └── 共享 Redis 队列（BLPOP 自动分发）
```

**总并发 = `--workers` × `--concurrency`**

所有进程/线程竞争同一个 Redis 队列，无需额外协调。每个 uvicorn worker 进程独立管理自己的 ToolManager 连接池和工作线程，一个进程崩溃不影响其他。

### 独立 Worker 模式（API 与 Worker 分离部署）

```
agentcode-api (仅 HTTP 入队，AGENT_WORKER_CONCURRENCY=0)
     │
     └── Redis 队列 ──┬── agentcode-worker --workers 2 --concurrency 8
                      └── agentcode-worker --workers 2 --concurrency 8  (可再加)
```

适合需要独立扩缩 API 层和执行层的场景。

### 队列选择

| 模式 | 队列 | 说明 |
|------|------|------|
| 单进程 (`--workers 1`) | MemoryQueue 或 Redis | MemoryQueue 自动降级，进程重启丢失 |
| 多进程 (`--workers >= 2`) | Redis（必需） | 跨进程共享，需设置 `TASK_STORE_REDIS_URL` |

---

## 3. 环境变量配置

服务启动时优先加载 `agentcode/.env`，若不存在则回退到 `backend/.env`。

### LLM 配置

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

### 并发配置

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `AGENT_API_WORKERS` | `1` | API 服务的 uvicorn worker 进程数（`agentcode-api --workers`） |
| `AGENT_WORKER_WORKERS` | `1` | 独立 Worker 进程数（`agentcode-worker --workers`） |
| `AGENT_WORKER_CONCURRENCY` | `4` | 每进程工作线程数 |
| `AGENT_WORKER_POOL_SIZE` | `=concurrency` | ToolManager 连接池大小 |

### 队列/存储配置

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `TASK_STORE_REDIS_URL` | `redis://localhost:6379/0` | Redis 连接 URL（多进程模式必需） |
| `TASK_STORE_KEY_PREFIX` | `agentcode:task` | Redis key 前缀 |
| `TASK_STORE_TASK_TTL` | `86400` | 任务在 Redis 中的过期时间（秒） |
| `TASK_STORE_MAX_QUEUE` | `50000` | 队列最大长度 |

### 其他

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `COMPACT_TOKEN_THRESHOLD` | `100000` | 触发上下文压缩的 token 阈值 |

### .env 示例

```bash
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_MODEL=deepseek-v4-pro
AGENT_TEMPLATE_TEMPERATURE=0.2
AGENT_TEMPLATE_MAX_TOKENS=4096
AGENT_TEMPLATE_TIMEOUT_SECONDS=120

# 并发配置
AGENT_WORKER_CONCURRENCY=8
AGENT_WORKER_POOL_SIZE=8

# Redis（多进程模式必需）
TASK_STORE_REDIS_URL=redis://localhost:6379/0
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

## 4. API 端点一览

| 方法 | 路径 | Content-Type | 说明 |
|------|------|-------------|------|
| `GET` | `/health` | — | 健康检查（含队列深度、worker 状态） |
| `POST` | `/v1/query` | `application/json` | 提交异步任务，返回 202 + task_id |
| `GET` | `/v1/task/{task_id}` | — | 查询任务状态 |
| `GET` | `/v1/task/{task_id}/stream` | — | SSE 流式轮询任务状态 |

`/v1/query` 只负责入队（<1ms），由内嵌工作线程消费队列并执行 Agent 循环。

---

## 5. 请求参数详解

### QueryRequest

```json
{
  "user_message": "string (必需)",
  "session_id": "string (可选，默认自动生成 UUID)",
  "trace_id": "string (可选，默认自动生成 UUID)",
  "mcp_config_path": "string|null (可选，默认加载内置 mcp_config.json)",
  "skill_ids": ["string (可选，过滤启用的 Skill)"],
  "tool_allowlist": ["string (可选，默认 [\"*\"])"],
  "max_turns": "int (可选，默认 50)",
  "max_tool_calls": "int (可选，默认 70)",
  "max_time_s": "int (可选，默认 1200)"
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
| `max_turns` | `int` | 否 | `50` | 最大 LLM 对话轮数 |
| `max_tool_calls` | `int` | 否 | `70` | 最大工具调用次数 |
| `max_time_s` | `int` | 否 | `1200` | 最大墙钟时间（秒） |

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

## 6. 响应格式

### GET /health

**响应 200：**

```json
{
  "status": "ok",
  "pid": 12345,
  "queue_depth": 3,
  "concurrency": 8,
  "pool": {
    "total": 8,
    "in_use": 3,
    "available": 5,
    "total_errors": 0,
    "total_borrows": 42
  }
}
```

| 字段 | 说明 |
|------|------|
| `status` | 服务状态 |
| `pid` | 当前响应请求的进程 ID（多 worker 时每次可能不同） |
| `queue_depth` | 当前队列中等待处理的任务数 |
| `concurrency` | 当前进程的工作线程数 |
| `pool` | ToolManager 连接池状态（仅当前进程） |

---

### POST /v1/query — 提交异步任务

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

**响应 202 Accepted：**

```json
{
  "task_id": "a1b2c3d4e5f6...",
  "status": "queued",
  "queue_position": 3
}
```

**响应 503 — 队列不可用：**

```json
{"detail": "task_store unavailable: ..."}
```

---

### GET /v1/task/{task_id} — 查询任务状态

**响应 200 — 排队中：**

```json
{
  "task_id": "a1b2c3d4...",
  "status": "queued",
  "created_at": "2026-05-23T10:00:00",
  "updated_at": "2026-05-23T10:00:00",
  "result": null,
  "error": null,
  "queue_position": 2
}
```

**响应 200 — 执行完成：**

```json
{
  "task_id": "a1b2c3d4...",
  "status": "completed",
  "created_at": "2026-05-23T10:00:00",
  "updated_at": "2026-05-23T10:00:45",
  "result": {
    "payload": { /* 交付数据 */ },
    "spent": {"turns": 5, "tool_calls": 8}
  },
  "error": null,
  "queue_position": 0
}
```

**响应 200 — 执行失败：**

```json
{
  "task_id": "a1b2c3d4...",
  "status": "failed",
  "error": "BUDGET_EXCEEDED: Turn budget exceeded",
  "result": null
}
```

**响应 404 — 任务不存在：**

```json
{"detail": "task not found: a1b2c3d4..."}
```

---

## 7. SSE 流式事件

### GET /v1/task/{task_id}/stream — 流式轮询

**请求示例：**

```bash
curl -N http://localhost:9180/v1/task/a1b2c3d4/stream
```

### 响应头

| 响应头 | 值 | 说明 |
|--------|-----|------|
| `Content-Type` | `text/event-stream` | SSE 流 |
| `Cache-Control` | `no-cache` | 禁止缓存 |
| `X-Accel-Buffering` | `no` | 禁用 nginx 缓冲 |

### 事件类型

#### `status` — 状态变更

```json
event: status
data: {"task_id":"a1b2c3d4","status":"queued","queue_position":3}
```

当任务状态从 queued → running → completed/failed 变化时推送。

#### `error` — 任务不存在

```json
event: error
data: {"error": "task not found"}
```

### 时序

```
POST /v1/query                   → 202 + task_id（立刻）
GET  /v1/task/{id}/stream        → 开始 SSE 连接
  event: status  queued           ← 排队中
  ... 2 秒后重新检查 ...
  event: status  running          ← 开始执行
  ... 2 秒后重新检查 ...
  event: status  completed        ← 执行完成（data 含 result）
  连接关闭
```

---

## 8. 错误码

| 错误码 | 说明 | 触发条件 |
|--------|------|---------|
| `BUDGET_EXCEEDED` | 预算超限 | 达到 `max_turns`、`max_tool_calls` 或 `max_time_s` |
| `CONSTRAINT_VIOLATION` | 约束违规 | 尝试调用白名单之外的工具 |
| `DELIVERY_NOT_SATISFIED` | 交付格式不符 | LLM 输出不满足 delivery JSON Schema |
| `REPEAT_ACTION_DETECTED` | 重复操作 | 连续 N 次完全相同工具调用 |
| `HTTP 404` | 任务不存在 | task_id 无效或已过期 |
| `HTTP 503` | 队列不可用 | Redis 连接失败 + 内存队列未初始化 |
| `HTTP 504` | 任务超时 | `/v1/query/wait` 等待超过 `max_time_s + 120` 秒 |

---

## 9. 使用示例

### 9.1 提交任务 + 轮询结果

```bash
# 1. 提交任务
TASK=$(curl -s -X POST http://localhost:9180/v1/query \
  -H "Content-Type: application/json" \
  -d '{"user_message": "帮我查一下北京地铁1号线最近一周的故障情况"}')
TASK_ID=$(echo $TASK | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
echo "Task: $TASK_ID"

# 2. 轮询直到完成
while true; do
  STATUS=$(curl -s http://localhost:9180/v1/task/$TASK_ID | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "Status: $STATUS"
  [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ] && break
  sleep 2
done

# 3. 获取最终结果
curl -s http://localhost:9180/v1/task/$TASK_ID | python3 -m json.tool
```

### 9.2 SSE 流式等待

```bash
curl -N http://localhost:9180/v1/task/$TASK_ID/stream
```

### 9.3 Python 客户端示例

```python
import httpx
import time

def sync_query(message: str) -> dict:
    """提交任务并阻塞等待结果。"""
    with httpx.Client(timeout=600) as client:
        resp = client.post("http://localhost:9180/v1/query", json={"user_message": message})
        task_id = resp.json()["task_id"]

        while True:
            resp = client.get(f"http://localhost:9180/v1/task/{task_id}")
            info = resp.json()
            if info["status"] in ("completed", "failed"):
                return info
            time.sleep(2)

def stream_query(message: str):
    """SSE 流式等待。"""
    with httpx.Client(timeout=600) as client:
        resp = client.post("http://localhost:9180/v1/query", json={"user_message": message})
        task_id = resp.json()["task_id"]

        with client.stream("GET", f"http://localhost:9180/v1/task/{task_id}/stream") as stream:
            for line in stream.iter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    print(f"[{event}] {line[6:]}")
```

---

## 10. 架构说明

### 10.1 请求处理流程（默认内嵌模式）

```
HTTP 请求
  │
  ├─ GET /health ─── 返回 {"status":"ok", "pid":..., "concurrency":..., "pool":...}
  │
  └─ POST /v1/query
       └─ task_store.enqueue(task_id, request_data)  ← ~1ms
            └─ Redis RPUSH / MemoryQueue append
       └─ 202 {"task_id": "..."}

                          ┌──────────────────────────────────────┐
                          │  FastAPI lifespan 拉起的工作线程       │
                          │                                      │
                          │  线程 0: while not stop:             │
                          │    task = task_store.dequeue()        │ ← Redis BLPOP
                          │    pool.borrow(tm)                    │ ← 借 ToolManager
                          │    AgentRuntime.run_session()         │ ← 执行 Agent 循环
                          │    task_store.update()                │ ← 写回结果
                          │                                      │
                          │  线程 1..N: （同上，并发执行）         │
                          └──────────────────────────────────────┘

  GET /v1/task/{id}
       └─ task_store.get(task_id)  ← Redis HGETALL / MemoryQueue dict
       └─ 返回当前状态 + 结果

  GET /v1/task/{id}/stream
       └─ SSE 循环: task_store.get() 每 2 秒一次
       └─ 状态变化时推送 event
```

### 10.2 连接池模型

```
FastAPI lifespan (每 uvicorn worker 进程)
    │
    ├─ ToolManagerPool(size=N)      ← N = --pool-size
    │     ├─ ToolManager[0]         ← MCP 连接 (所有配置的服务器)
    │     ├─ ToolManager[1]
    │     ├─ ...
    │     └─ ToolManager[N-1]
    │
    └─ borrow/return: 线程安全的 Condition 等待
         线程 0 借 TM[0], 线程 1 借 TM[1], ...
         用完归还, 下一个线程复用
```

### 10.3 降级策略

| 组件 | 正常 | 降级 |
|------|------|------|
| task_store | Redis 队列（多进程共享） | MemoryQueue（单进程，进程重启丢失） |
| ToolManager | 连接池 N 个 | 逐个创建（慢，但能跑） |
| Worker | 内嵌线程（lifespan 管理） | 独立 `agentcode-worker` 进程（高级部署） |

### 10.4 相关文件

| 文件 | 职责 |
|------|------|
| `agentcode/api.py` | HTTP 网关 + lifespan 内嵌 worker 线程 |
| `agentcode/worker.py` | 工作线程核心函数 + 独立 worker CLI |
| `agentcode/pool.py` | ToolManager 连接池（线程安全） |
| `agentcode/task_store.py` | 任务队列 + 状态存储（Redis / MemoryQueue） |
| `agentcode/runtime.py` | Agent 执行循环（线程安全） |
| `agentcode/prompt.py` | 系统提示词构建 |
| `agentcode/models.py` | 数据模型 |
| `agentcode/adapter.py` | LLM 消息格式适配 + 环境加载 |