# AgentCode

AgentCode 是 QASystem 仓库内的单智能体运行时和后台 Agent 服务。它负责把一次用户请求组织成完整的执行循环：调用 LLM、解析工具请求、执行 MCP/内置工具、回填结果、控制预算，并通过异步任务 API 返回最终结果和实时事件。

当前包版本：`0.3.0`。

## 文档导航

| 文档 | 适用场景 |
| --- | --- |
| [架构设计](docs/架构设计.md) | 理解组件关系、任务状态机、可靠队列、跨进程 SSE 和取消语义 |
| [开发扩展指南](docs/开发扩展指南.md) | 接入模型、MCP、Skill，修改 Agent loop 或编写测试 |
| [部署运维指南](docs/部署运维指南.md) | Compose 部署、环境变量、容量规划、监控和故障排查 |
| [流式接口说明](流式接口说明.md) | 面向调用方的 HTTP、任务状态和 SSE 事件协议 |
| [接口参考](接口参考.md) | 启动参数、请求字段、响应结构和调用示例 |

## 能力边界

AgentCode 当前负责：

- 单 Agent 的 LLM ↔ Tool 执行循环；
- OpenAI 兼容 `chat/completions` 模型适配；
- MCP `streamable_http`、HTTP、SSE、stdio 客户端；
- 内置 Shell 工具；
- Skill 发现、按需加载和提示词注入；
- 工具白名单、轮次/工具次数/墙钟时间预算；
- JSON Schema 终稿校验；
- Redis 或内存任务队列；
- 后台 Worker、任务取消、任务租约和崩溃恢复；
- Redis Stream/内存事件日志与 SSE；
- 任务级 Trace 和 JSONL Trace。

当前不负责：

- 业务数据库、知识图谱和文档处理的具体实现，这些由 QASystem MCP Server 提供；
- 跨请求的对话历史持久化。`session_id` 当前用于关联和追踪，不会自动恢复历史消息；
- 独立 Worker CLI。当前 Worker 由 FastAPI lifespan 在每个 API 进程中启动；
- 通用编码 Agent 所需的结构化文件编辑、Git、持久终端和审批工作流。

## 快速启动

### 使用仓库 Compose

先生成运行目录并构建镜像：

```powershell
.\package-deploy.ps1
docker build -t qasystem-backend:latest .
docker compose up -d
```

Compose 会启动：

- `backend`：QASystem HTTP 和 MCP Server，容器内端口 `8000`；
- `agent`：AgentCode API，默认宿主机端口 `9180`；
- `redis`：任务、租约和事件流存储。

检查服务：

```bash
curl http://127.0.0.1:9180/health
```

提交任务：

```bash
curl -X POST http://127.0.0.1:9180/v1/query \
  -H "Content-Type: application/json" \
  -d '{"user_message":"查询知识图谱中的采购指标定义"}'
```

返回示例：

```json
{
  "task_id": "6a97f5f4c4f94db5aeedecbc4c992a47",
  "status": "queued",
  "queue_position": 1
}
```

查询和取消：

```bash
curl http://127.0.0.1:9180/v1/task/6a97f5f4c4f94db5aeedecbc4c992a47
curl -X POST http://127.0.0.1:9180/v1/task/6a97f5f4c4f94db5aeedecbc4c992a47/cancel
```

订阅流式事件：

```bash
curl -N http://127.0.0.1:9180/v1/task/6a97f5f4c4f94db5aeedecbc4c992a47/stream
```

### 本地开发启动

```powershell
cd agentcode
..\.venv\Scripts\python.exe -m pip install -e ".[api,dev]"
$env:PYTHONPATH = "$PWD"
python -m agentcode.api --host 127.0.0.1 --port 9180
```

默认 MCP 配置位于：

```text
agentcode/agentcode/tools/mcp_tools/mcp_config.json
```

也可以设置：

```bash
AGENT_MCP_CONFIG=/absolute/path/to/mcp_config.json
```

MCP 连接池在服务启动时建立，不支持在单个请求中切换到另一份配置。

## 核心执行流程

```mermaid
flowchart TD
    A[HTTP 请求入队] --> B[Worker 可靠领取任务]
    B --> C[构建 SessionConfig 和系统提示词]
    C --> D[调用 LLM]
    D --> E{返回工具调用?}
    E -->|是| F[校验白名单与预算]
    F --> G[执行 MCP/内置工具]
    G --> H[工具结果写回上下文]
    H --> D
    E -->|否| I{Delivery Schema 合法?}
    I -->|否| J[注入修正要求]
    J --> D
    I -->|是| K[保存结果和终态]
```

每次 HTTP 请求都会创建独立的 `AgentState`。LLM 调用次数计入 `spent.turns`，实际发起的工具调用数量计入 `spent.tool_calls`。

## 任务状态

```text
queued → running → completed
                 ↘ failed
queued → cancelled
running → cancelling → cancelled
```

- `queued`：等待 Worker；
- `running`：已领取并执行；
- `cancelling`：收到取消请求，等待 LLM/工具在协作检查点退出；
- `completed`：成功产生最终结果；
- `failed`：预算、模型、工具或运行时错误；
- `cancelled`：任务取消完成。

Redis 模式使用 processing 队列和续租时间。Worker 异常退出后，超过 `TASK_STORE_VISIBILITY_TIMEOUT` 的任务会重新入队。

## 最小 Python 用法

```python
from agentcode import AgentRuntime, CancellationToken, SessionConfig
from agentcode.trace import JsonlTraceSink

runtime = AgentRuntime(
    llm_backend=my_llm_backend,
    tool_manager=my_tool_manager,
    trace_sink=JsonlTraceSink("./traces/session.jsonl"),
)

token = CancellationToken()
result = runtime.run_session(
    config=SessionConfig(session_id="s-1", trace_id="t-1"),
    user_message="分析这个问题",
    cancel_token=token,
)
```

`LLMBackend` 和 `ToolManager` 的契约见[开发扩展指南](docs/开发扩展指南.md)。

## 常用配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT_LLM_BASE_URL` | `http://localhost:9999` | OpenAI 兼容 API 根地址 |
| `AGENT_LLM_API_KEY` | 空 | 模型 API Key |
| `AGENT_LLM_MODEL` | `Qwen/Qwen3-8B` | 模型名 |
| `AGENT_TEMPLATE_TIMEOUT_SECONDS` | `120` | 单次模型请求超时 |
| `AGENT_API_WORKERS` | `1` | API 进程数 |
| `AGENT_WORKER_CONCURRENCY` | `1` | 每个 API 进程的 Worker 线程数 |
| `AGENT_WORKER_POOL_SIZE` | 等于 concurrency | 每进程 MCP 连接池大小 |
| `TASK_STORE_REDIS_URL` | `redis://localhost:6379/0` | Redis 地址；多进程必需 |
| `TASK_STORE_VISIBILITY_TIMEOUT` | `1500` | Worker 失联后的任务回收时间 |
| `TASK_STORE_TASK_TTL` | `86400` | 任务和结果保留时间 |
| `TASK_STORE_EVENT_MAXLEN` | `10000` | 每任务最大事件数 |
| `COMPACT_TOKEN_THRESHOLD` | `100000` | 触发上下文压缩的估算 token 数 |

完整配置和容器网络注意事项见[部署运维指南](docs/部署运维指南.md)。

## 测试

普通测试：

```powershell
$env:PYTHONPATH = "$PWD\agentcode"
.\.venv\Scripts\python.exe -m pytest agentcode/tests -q
```

Redis 集成测试：

```powershell
$env:TEST_REDIS_URL = "redis://127.0.0.1:6379/15"
.\.venv\Scripts\python.exe -m pytest agentcode/tests/test_task_store_redis.py -q
```

Shell 资源限制测试依赖 POSIX `resource`，在 Windows 上会自动跳过，应在 Linux 镜像或 CI 中执行。

## 源码入口

| 文件 | 作用 |
| --- | --- |
| `agentcode/runtime.py` | Agent 主循环、预算、取消和 Delivery 校验 |
| `agentcode/api.py` | FastAPI、请求入队、查询、取消和 SSE 端点 |
| `agentcode/worker.py` | Worker 消费循环和 LLM HTTP 适配 |
| `agentcode/task_store.py` | Redis/内存任务队列、租约、结果和事件日志 |
| `agentcode/stream_broker.py` | 面向 SSE 的游标事件读取封装 |
| `agentcode/pool.py` | ToolManager 连接池 |
| `agentcode/tools/mcp_tools/` | MCP 配置、连接和工具调度 |
| `agentcode/skillsets/` | 内置 Skill |
| `agentcode/trace.py` | JSONL 与任务事件 Trace |
