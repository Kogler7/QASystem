# `backend/` 说明

`backend/` 是系统的 **HTTP 服务与执行层**：对外提供算法相关 HTTP 接口（文档、图谱、向量、知识沉淀等），
对内提供**异步执行框架**——把耗时任务按资源类排队交给 Worker 执行，并对外提供任务状态、结果、取消与事件流。

MCP 入口也由本服务承载：应用把统一 MCP 的 Streamable HTTP 应用挂载在根路径，因此 MCP 地址为 `/mcp`。

---

## 1. 目录结构

| 路径 | 作用 |
| --- | --- |
| `app/main.py` | FastAPI 应用装配：注册路由、健康检查、挂载 MCP、代理 Agent 入口 |
| `app/environment.py` | 统一加载仓库根 `.env` |
| `app/health.py` | 健康检查内容（依赖项状态汇总） |
| `app/responses.py` | 统一响应封装 |
| `app/schemas.py` | 请求/响应模型 |
| `app/routers/` | 对外路由（见第 2 节） |
| `app/services/` | 路由对应的业务编排（调用 `algorithm/`） |
| `app/execution/` | 异步执行框架（见第 3 节） |

---

## 2. 对外路由（`app/routers/`）

### 2.1 算法能力 `routers/algorithm_capabilities.py`

| 方法 | 路径 | 能力 |
| --- | --- | --- |
| POST | `/api/v1/documents/parse` | 文档解析为 Markdown |
| POST | `/api/v1/documents/chunk` | Markdown 语义切片 |
| POST | `/api/v1/graphs/extract` | 分片图谱抽取 |
| POST | `/api/v1/graphs/merge` | Graph 确定性去重合并 |
| POST | `/api/v1/embeddings/texts` | 多文本向量化 |
| POST | `/api/v1/embeddings/graphs` | Graph JSON 向量化 |

> 历史路径以 `deprecated` 别名保留，字段与 v1 一致。

### 2.2 知识沉淀 `routers/knowledge_precipitation.py`

| 方法 | 路径 | 能力 |
| --- | --- | --- |
| POST | `/api/v1/pipelines/knowledge-ingestion` | 解析→切片→抽取→向量化 的聚合流水线 |

### 2.3 置信度 `routers/confidence.py`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/graphs/confidence` | 保留路径；第三方能力开通前返回 503 |

### 2.4 执行与运维 `routers/execution.py`

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/api/v1/jobs` | 提交任务 |
| GET | `/api/v1/jobs/{job_id}` | 任务状态 |
| GET | `/api/v1/jobs/{job_id}/result` | 任务结果 |
| GET | `/api/v1/jobs/{job_id}/executions` | 执行明细 |
| GET | `/api/v1/jobs/{job_id}/events` | 单个任务的事件流（SSE） |
| DELETE | `/api/v1/jobs/{job_id}` | 取消任务 |
| GET | `/api/v1/events/stream` | 全局事件流（SSE） |
| GET / PUT | `/api/v1/observability/levels` | 观测级别读写 |
| GET | `/api/v1/resources/gpus` | GPU 资源情况 |

### 2.5 其它

- `GET /health`：健康检查；
- `/agent`、`/agent/{path}`：转发到 Agent 运行时的代理入口。

---

## 3. 异步执行框架（`app/execution/`）

| 文件 | 作用 |
| --- | --- |
| `manager.py` | 执行管理器：任务提交、状态流转、调度与等待（`submit` / `wait_blocking` / `result` / `execution_view` / `cancel`） |
| `worker.py` | Worker：按资源类从队列取任务并执行，处理重试与资源预留 |
| `queue.py` | 队列抽象（Redis 等后端） |
| `store.py` | 任务与执行记录的持久化存储 |
| `hub.py` | 事件中心：向任务事件流/全局事件流推送进度 |
| `operations.py` | **操作注册表**：把 operation 名映射到具体实现（文档解析、图谱抽取、向量化、MCP 工具入队执行等） |
| `resources.py` | 资源类与配额（CPU/GPU 等） |
| `supervisor.py` | Worker 监督与生命周期 |
| `models.py` | 执行域数据模型 |
| `api.py` | 执行域的对外能力封装 |
| `artifacts.py` | 任务产物（文件等）管理 |
| `document_sharding.py` | 大文档解析的分片编排（`orchestrate_document_parse`） |

### 3.1 资源类与队列

任务提交时携带**资源类**（如 `io.graph`、`io.llm`、`io.db`、`cpu.chart`、`gpu.vlm`），
执行管理器据此选择队列并匹配具备该资源的 Worker。

### 3.2 与 MCP 的关系

MCP 工具通过 `maybe_submit` 决定是否入队（见 [`mcp_services/README.md`](../mcp_services/README.md)）。
当以队列模式执行时，Worker 通过 `operations.py` 找到同名操作并执行**同一个工具函数**，
因此无论 inline 还是入队，工具的行为与返回契约完全一致。

```text
MCP 工具调用 ──maybe_submit──▶ 执行管理器 ──▶ 队列（按资源类）──▶ Worker
                                                              │
                                              operations.py 映射到具体实现
                                                              ▼
                                                          返回结果
```

---

## 4. 与服务层的关系

```text
routers/（HTTP 参数与契约）
   └──▶ services/（业务编排）
            └──▶ algorithm/（算法与业务实现）
                     └──▶ external_services/（第三方接口：图谱/向量/文件）
```

- 路由与编排层**不实现业务算法**，只做参数校验、调用与结果封装；
- 需要长耗时或占用特定资源的操作走执行框架，避免阻塞请求线程。

---

## 5. 本地运行与测试

### 本地运行

Backend 依赖仓库根 `.env` 中的配置（如 `KG_EXTRACT_LLM_*`、`GRAPH_LLM_*`、`AGENT_LLM_*`、`APPLICATION_SERVICE_URL` 等），
建议先复制 `.env.example` 并填入实际值：

```bash
cp .env.example .env
```

从仓库根目录启动：

```bash
python -m backend.app.main
```

默认监听 `0.0.0.0:8006`（由 `BACKEND_BIND` 与 `BACKEND_PORT` 控制，见 `.env.example`）。

容器化启动参考根目录 `docker-compose.yml`：

```bash
docker compose up -d backend
```

### 运行测试

根 `pytest.ini` 已配置测试路径，从仓库根目录执行：

```bash
pytest
```

即可运行 `backend/tests`、`agent/tests`、`algorithm/knowledge_extract/v2/tests` 等已注册测试。
若只跑后端测试：

```bash
pytest backend/tests
```

backend 的额外解析依赖（如 MinerU 相关包）见 `backend/requirements.mineru-extras.txt`，
按实际解析器按需安装。
