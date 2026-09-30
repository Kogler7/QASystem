# 服务并发与 GPU 智能调度架构设计说明

> 状态：单机多 GPU 架构已落地，RTX 3090 环境已完成阶段性验证，目标 A40 环境仍需容量调优与验收
>
> 范围：Backend HTTP API、统一 MCP、文档解析、知识沉淀、Embedding、Confidence 及其统一执行基础设施；Agent 服务的并发模型在第 7.3 节概述，完整细节见 `docs/Agent/架构设计.md`

本文说明当前 `develop` 分支已经实现的服务并发、任务队列、GPU 调度、VLM PDF 分片、可观测性和部署架构。它描述的是现行实现，不再作为重构计划或未来组件选型清单。

## 1. 设计原则

当前架构遵循以下原则：

1. 客户端描述业务意图，不负责指定进程、GPU 或页段并行度。
2. 所有重计算先按资源能力进入共享队列，由对应 Worker 消费；资源不足时排队等待，不在 API 进程中抢跑。
3. GPU 原子任务在 Worker 领取后才根据实时 GPU 快照和已发放租约申请容量，不在任务提交时静态预绑定 GPU。
4. VLM PDF 的分片是服务端执行计划。实际解析路线为 VLM 且达到分片阈值时，即使客户端不传引擎和分片参数，也会自动拆分。
5. 分片数量用于形成可跨 Worker 抢占的工作 backlog；显存预算用于限制在途任务数。二者不是同一个控制量。
6. 旧业务 HTTP API 默认保持同步返回语义；异步句柄是调用方显式选择的增量能力。
7. 任务状态、原子执行、GPU 分配和日志事件集中持久化，并通过查询接口、SSE 和响应顶层 `execution` 暴露。
8. 正式部署失败关闭：Redis 执行层不可用时返回 `503`，不退回 API 进程内执行。

## 2. 单容器运行拓扑

正式 Backend 镜像由一个 supervisor 入口管理容器内全部子进程，Compose 不枚举 API、vLLM 或 Worker 副本。

```text
Backend container
└── RuntimeSupervisor
    ├── Uvicorn API processes
    ├── CPU workers
    │   └── document orchestration / chunk / graph / chart / LLM-I/O / DB-I/O
    ├── GPU 4: vLLM engine + VLM dispatch workers + auxiliary workers
    ├── GPU 5: vLLM engine + VLM dispatch workers + auxiliary workers
    ├── GPU 6: vLLM engine + VLM dispatch workers + auxiliary workers
    └── GPU 7: vLLM engine + VLM dispatch workers + auxiliary workers

Redis
├── Redis Streams capability queues
├── job / execution / event state
├── short-lived authentication contexts
├── worker heartbeat registry
└── GPU and worker-slot leases

Application service
└── files / vectors / authorized Cypher query
```

supervisor 的启动顺序为：

1. 启动 API 和 CPU Worker；此时 `/health` 仍可报告运行时正在启动。
2. 在 `GPU_DEVICE_IDS` 选中的每张卡上启动一个常驻 vLLM engine。
3. 并行等待每个 engine 就绪，并执行一次小型多模态 warmup 请求。
4. 所有 engine 完成 warmup 后，启动各卡 VLM dispatch Worker 和 Embedding/Confidence 辅助 Worker。
5. 写入 runtime ready 标记；任一受管子进程异常退出时，supervisor 结束本次运行并统一清理子进程。

API、CPU Worker、每卡 VLM Worker 数量由环境变量控制；其中 CPU 和 VLM Worker 支持 `auto`，根据主机 CPU 数和选中 GPU 数派生。当前实现不会在运行期间根据负载动态增减 vLLM engine 或 Worker 进程。

### 2.1 一次请求的处理生命周期

以下是一次典型重计算请求在系统中的完整路径，也是理解各组件如何协作的主线：

```text
客户端
  -> API 进程
       鉴权中间件捕获用户请求头 -> ContextVar（不落日志、不落 payload）
       路由映射到资源类别
       ExecutionManager.submit：
         1. 鉴权头写入带 TTL 的独立 Redis KV，得到随机 auth_context_id
         2. 业务 payload 写入 ArtifactStore，队列消息只携带引用
         3. XADD 到资源类别共享 Stream
       同步语义：在独立线程中轮询结果（Prefer: wait=N 限时等待）；异步语义：立即返回 202
  -> Worker（先查本 Worker 定向 Stream 处理重试回补，再从共享 Stream 抢占）
       GPU 任务：Redis Lua 脚本原子申请 GPU Lease / Worker slot（见 §5.2）
       容量不足：任务回到共享队列，Job 进入 waiting_for_capacity
       load_auth_context -> bind_user_headers 恢复用户上下文
       执行算法；页段级子 Job 由编排器并行下发并合并
       delete_auth_context；Lease 释放
       结果与事件写回 Redis（ArtifactStore / Event Hub）
  -> 客户端
       轮询/限时等待返回，或 SSE 事件流（Last-Event-ID 断线续传），或 result_url 拉取 envelope
```

这条路径上有三个不变量贯穿始终：

1. **凭证不进队列**：队列消息、Job 记录、SSE 和结果中只出现 `auth_context_id`；Authorization 头只存在于带 TTL 的独立 KV 和 Worker 执行期间的内存上下文中。
2. **重计算不进 API 进程**：API 进程只做协议转换、入队和等待，天然无状态，Uvicorn 副本数可以远小于执行进程数。
3. **执行现场完全可追溯**：每一次尝试都能从 Job 关联到 Worker 实例、物理 GPU、Lease、显存预算和排队/调度/执行/持久化分段耗时（见 §8）。

## 3. 统一执行层

### 3.1 资源能力队列

业务路由将操作映射到资源类别，主要包括：

| 资源类别 | 典型操作 | 消费者 |
| --- | --- | --- |
| `cpu.document_orchestrator` | 文档引擎规划、VLM 子任务编排、结果合并 | CPU Worker |
| `cpu.chunk` / `cpu.graph` / `cpu.chart` | 切片、图处理、图表生成 | CPU Worker |
| `io.llm` / `io.db` / `io.graph` | 外部 LLM、数据库和图数据库操作 | CPU/I/O Worker |
| `workflow.knowledge` | 知识沉淀流程 | CPU Worker |
| `gpu.vlm` | VLM PDF 页面或页段解析 | 各 GPU 的 VLM dispatch Worker |
| `gpu.embedding` | 向量化 | 各 GPU 的辅助 Worker |

生产模式使用 Redis Streams。普通任务写入资源类别共享 Stream；Worker 优先检查自己的定向 Stream，再从共享 Stream 领取任务。因此任务不会在提交时永久绑定某张卡，任何具备该能力且获得租约的 Worker 都可以处理，形成跨 GPU work stealing。

任务 payload 和结果由 ArtifactStore 持久化，队列消息只携带任务和产物引用。用户鉴权头保存在带 TTL 的独立 Redis KV 中，队列只携带随机 `auth_context_id`；Worker 恢复上下文后调用应用团队 API，凭证不会进入任务结果、SSE 或 `execution`。Job、执行尝试、事件、Worker 心跳及 Lease 也保存在 Redis 执行命名空间中。内存队列仅用于单元测试或本地算法调试，不是生产降级路径。

图谱读取仍进入 `io.graph` 队列，但 Worker 通过应用团队的鉴权 Cypher API 查询，不再连接 Neo4j。SQL 是当前唯一保留的数据库直连兼容能力。图谱写入、置信度和文档证据检索在第三方接口开通前不会注册执行实现，调用时明确返回能力不可用。

### 3.2 任务生命周期

典型状态流为：

```text
accepted -> queued -> running -> succeeded
                    |          -> failed
                    |          -> retry_wait -> queued
                    -> waiting_for_capacity -> queued
                    -> cancelling/cancelled
```

每次尝试单独记录 `attempt_id`、Worker、进程实例、GPU Lease、排队/开始/结束时间、耗时和错误。执行失败时按 `EXECUTION_MAX_ATTEMPTS` 重试，且重试尊重错误码协议：业务异常携带非 retryable 错误码时（参数类 1xxx、解析/切片结果类 2xxx/3xxx、本体无效 4006 等确定性失败）不再白重试，错误对象中标记 `retryable=false`；未知异常（网络/依赖抖动）保持可重试。VLM PDF 的每个页段是独立子 Job，因此失败只会重试相应页段，而不是重新解析整份 PDF。

Worker 还内置 PEL 滞留消息重投（崩溃恢复）：每个 Worker 周期收割本 Worker 名下共享队列与定向队列中 idle 超过 `EXECUTION_REAPER_MIN_IDLE_SECONDS`（默认 600 秒）的滞留消息并重新入队（attempt+1）；滞留期间被取消的任务直接终态化为 `cancelled`，达到 `EXECUTION_MAX_ATTEMPTS` 的按失败终态化。默认 `EXECUTION_REAPER_ENABLED=true`、`EXECUTION_REAPER_INTERVAL_SECONDS=60`。这是崩溃恢复路径，正常完成的消息不受影响。

取消采用协作式语义：未开始任务可直接取消；运行中的父任务会向仍在等待的页段子任务传播取消请求，已经进入算法执行的工作在可返回的边界结束。

## 4. VLM PDF 自动分片

### 4.1 路由与分片发生的位置

文档请求先进入 `cpu.document_orchestrator`。编排器只解析一次实际引擎计划：

```text
document.parse
  -> resolve actual engine plan
  -> non-VLM route: execute selected parser in orchestrator
  -> VLM PDF route:
       count pages
       build ordered page ranges
       submit document.parse.shard jobs to shared gpu.vlm queue
       monitor child jobs
       merge Markdown by shard_index
```

因此，是否进入 VLM 队列取决于服务端解析出的实际执行路线，而不是客户端是否显式填写 `engine=vlm`。旧客户端仅上传 PDF，且自动规划最终选择 VLM 时，同样会自动分片。非 PDF 或实际路线不是 VLM 时不会错误进入 `gpu.vlm` 队列。

默认行为如下：

- `page_sharding=auto`；PDF 页数达到 `EXECUTION_VLM_SHARD_MIN_PAGES` 后启用。
- `EXECUTION_VLM_SHARD_COUNT=auto` 时，目标分片数约为当前存活 VLM Worker 数的 1.5 倍，并受文档页数和 `max_page_shards` 限制。
- `max_page_shards` 默认 16，允许范围 1 到 32。
- 页段为连续区间，完成顺序不影响结果；合并时按 `shard_index` 恢复原页序。
- `page_shard_size` 非零时可以提高目标分片数，但仍受 `max_page_shards` 限制。

自动生成“约 1.5 轮工作量”是为了让先完成的 Worker 继续领取剩余页段，减少固定四大段时最慢页拖住整份文档的尾部效应。它同时服务两类场景：并发文档可在同一共享队列公平推进；单个长文档也能让多卡、多 Worker 同时工作。

### 4.2 分片数与显存容量的边界

当前实现不根据空闲显存计算“每个 Worker 应分几页”。页面复杂度、视觉 token 数和推理耗时并不只由页数决定，而 vLLM 的模型常驻显存和 KV Cache 也不能简单换算成页段大小。

架构将两个问题分开处理：

| 控制量 | 决定什么 | 当前依据 |
| --- | --- | --- |
| 分片数/页段范围 | 队列中有多少可独立抢占、重试和合并的工作单元 | 页数、存活 VLM Worker 数、分片阈值和上限 |
| GPU Lease/Worker slot | 同一时刻允许多少原子任务进入执行 | Worker 心跳快照、显存余量、任务显存预算、安全余量、设备和 Worker Lease 上限 |
| vLLM 并发 | 单个 engine 内的请求排队、连续批处理和 KV Cache 使用 | `VLLM_MAX_NUM_SEQS`、`VLLM_GPU_MEMORY_UTILIZATION` 及 vLLM 运行时 |

这使显存负责准入和防止超配，细粒度页段负责跨卡负载均衡。当前尚未实现根据历史页面复杂度预测耗时、运行中继续二次拆分慢页段，或根据反馈自动调整下一份文档分片粒度；这些能力不应被视为现有架构保证。

### 4.3 知识沉淀流水线的可选解析页分片

`document.parse` 的分片是执行框架能力；知识沉淀流水线（`workflow.knowledge`，`POST /api/v1/pipelines/knowledge-ingestion`）内部的 PDF 解析默认仍走整篇编排，可通过 `KNOWLEDGE_PIPELINE_PARSE_SHARDING=true` 打开页分片加速（复用 `EXECUTION_VLM_SHARD_MIN_PAGES` / `EXECUTION_VLM_SHARD_COUNT` 阈值，按存活 VLM Worker 并行）。非 PDF 文件、`options` 显式 `page_sharding=off`、inline 执行环境（无队列）会自动回退整篇解析。默认 `false`；该开关只影响流水线内解析编排，不改变 `document.parse` 的默认行为。

## 5. GPU 容量与租约调度

### 5.1 Worker 注册与实时快照

GPU Worker 定期向 Redis 注册：

- `worker_id` 和进程生命周期唯一的 `worker_instance_id`；
- 支持的资源类别；
- 绑定的物理 GPU；
- GPU UUID、总显存、已用显存、利用率等快照；
- 对应 vLLM endpoint（VLM Worker）。

心跳过期的 Worker 不再作为候选。每个 GPU 子进程在导入 Torch/MinerU 前设置 `CUDA_VISIBLE_DEVICES`，确保算法只能看到 supervisor 分配的单张卡。

### 5.2 Worker 自主领取与准入

共享队列中的任务没有预设 GPU。某个 Worker 领取 GPU 任务后，调度器只为该 Worker 绑定的设备尝试创建 Lease，准入同时检查：

- 心跳中的可用显存；
- 同设备所有有效 Lease 已预留的显存预算；
- `EXECUTION_GPU_SAFETY_MARGIN_MB`；
- `EXECUTION_GPU_MAX_LEASES_PER_DEVICE`；
- `EXECUTION_GPU_MAX_LEASES_PER_WORKER`；
- 对应资源类别的 `EXECUTION_GPU_MEMORY_REQUEST_MB_*`。

Redis Lua 脚本原子检查并写入 GPU Lease 和 Worker-slot Lease。拿不到容量时，任务回到共享能力队列并进入 `waiting_for_capacity`，其他 GPU Worker 可以继续领取；它不是算法失败，也不会因此消耗一次算法重试。执行结束或失败后 Lease 释放，过期 Lease 由 TTL 兜底。

候选排序优先考虑有效空闲显存，其次参考 GPU 利用率、设备 Lease 数、Worker Lease 数和 Worker ID。`/api/v1/resources/gpus` 可查询当前 GPU 快照、Worker 和 Lease 状态。

### 5.3 当前调度边界

当前实现是单机、基于 Redis 的容量调度器，不包含以下能力：

- 根据队列压力动态启动或销毁 vLLM engine；
- 同卡自动创建多个模型副本；
- 跨主机 GPU 调度；
- 基于租户权重、deadline 或预测耗时的全局最优调度；
- 自动学习并修改显存预算或 vLLM 参数。

这些边界不会妨碍现有共享队列在选定 GPU 之间 work stealing，但生产参数仍需通过目标 A40 压测确定。

## 6. Readiness 与 warmup

每张卡的 vLLM engine 启动后，supervisor 反复执行 readiness 脚本。探针先检查服务健康，再提交一个最小多模态请求，并使用与 engine PID 关联的 marker 避免重复预热。

启动期间端口尚未监听产生的连接拒绝属于预期重试，retry loop 默认不逐次打印失败；只有超过 `VLLM_STARTUP_TIMEOUT_SECONDS`、engine 提前退出或最终无法预热时，supervisor 才将启动判定为失败。只有全部 engine 打印 `ready and warm` 后 GPU Worker 才开始接单，避免首个用户请求承担模型编译和冷启动开销。

VLM 端点注入跟随引擎启动计划：supervisor 只对本次运行计划启动的 engine 注入 `MINERU_VLM_BACKEND=vlm-http-client` 与 `MINERU_VLM_SERVER_URL`（API 进程取第一个端点，CPU Worker 按索引轮转各端点，每卡 VLM Worker 取本卡端点）。引擎不启动时（remote-only 模式或 `VLLM_ENABLED=false`）绝不注入指向空地址的 URL，否则解析选中 vlm-http-client 后必然连接失败，还会因「URL 已配置」跳过本地 MinerU 4.x 的自动回退。

## 7. HTTP 与 MCP 协议

### 7.1 响应 envelope

现有业务 API 的请求路径和业务 `data` 保持不变，统一响应只在顶层增加 `execution`：

```json
{
  "code": 0,
  "info": "文档解析完成",
  "data": {},
  "error": null,
  "execution": {
    "mode": "async",
    "job_id": "job-id",
    "status": "succeeded",
    "stage": "completed",
    "summary": {},
    "events_url": "/api/v1/jobs/job-id/events",
    "executions_url": "/api/v1/jobs/job-id/executions",
    "result_url": "/api/v1/jobs/job-id/result"
  }
}
```

`execution.mode=async` 表示任务由 Redis 执行层调度，不等于 HTTP 必须返回 `202`。

### 7.2 同步兼容与显式异步

| 调用方式 | HTTP 行为 |
| --- | --- |
| 旧业务 API，不传 `Prefer` | 任务进入共享队列，服务端等待完成，返回原 `200 + data` 语义 |
| `Prefer: respond-async` | 立即返回 `202 + job_id` |
| `Prefer: wait=N` | 最多等待 N 秒；完成则返回业务结果，超时则返回 `202`，任务继续执行；N 最大 60 秒 |
| `POST /api/v1/jobs` | 显式异步任务入口，默认返回 `202` |

异步调用在 SSE 收到 `job.completed` 后访问 `execution.result_url`，得到包含最终业务 `data` 和最终 `execution` 的完整 envelope。结果未成功完成时该接口返回 `409`。

同步兼容语义有明确的容量含义：默认 `wait` 在 ASGI 线程池中轮询等待，长任务高并发下大量等待请求会占满线程池并推高 API 延迟。高并发场景应优先使用 `respond-async` 或 `wait=N` 上限，把结果等待转移到客户端轮询或 SSE。

作业接口包括：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/jobs` | 显式提交通用任务 |
| `GET` | `/api/v1/jobs/{job_id}` | 查询状态、进度和页段摘要 |
| `GET` | `/api/v1/jobs/{job_id}/result` | 获取最终结果 envelope |
| `DELETE` | `/api/v1/jobs/{job_id}` | 请求取消 |
| `GET` | `/api/v1/jobs/{job_id}/executions` | 分页查询执行尝试和资源分配 |
| `GET` | `/api/v1/jobs/{job_id}/events` | SSE 订阅任务事件 |

MCP 生产默认使用 `MCP_EXECUTION_MODE=sync`：重工具进入同一执行队列并等待原 MCP 结果，现有 Agent 不必改调用协议。只有显式配置 `async` 才返回任务句柄；`inline` 仅用于测试或本地算法断点调试。

### 7.3 Agent 服务的并发模型

Agent 服务是与 Backend 并列的独立任务型入口，复用同一套"入队返回句柄"范式，但有自己独立的并发层次（完整细节、幂等与取消约束见 `docs/Agent/架构设计.md`）：

- **总并发 = W × min(C, P)**：W 个 uvicorn 进程（`AGENT_API_WORKERS`；多进程必须由 Redis 承载共享队列，否则拒绝启动）× 每进程 C 个工作线程（`AGENT_WORKER_CONCURRENCY`，生产默认 4）× ToolManager 连接池大小 P（`AGENT_WORKER_POOL_SIZE`，默认等于 C）。每个 ToolManager 绑定专属的事件循环线程，通过 `run_coroutine_threadsafe` 调用 MCP。
- **任务队列是"至少一次"语义**：Redis List 主队列 + processing 队列，Worker 每 30 秒续约租约，超过 `TASK_STORE_VISIBILITY_TIMEOUT`（默认 1500s）未完成的处理中任务，由其他 Worker 在领取任务前扫描 processing 时自动重新入队。崩溃恢复路径简单，代价是有副作用的工具必须自身幂等。
- **流式输出跨进程**：Worker 线程把 LLM 的逐 token delta 同步发布到任务事件 Stream；SSE 订阅端按游标独立读取，支持 `Last-Event-ID` 断线重放，发布者与订阅者可以不在同一进程。
- **与统一执行层的关系**：生产默认 `MCP_EXECUTION_MODE=sync`，Agent 线程发起的重型工具调用仍进入 Backend 的统一执行队列并等待原结果，因此 Agent 工作线程不会被单卡计算长时间独占。

## 8. 执行可观测性

### 8.1 原子执行记录

每次执行尝试记录：

- `task_id`、父任务、页段序号和页范围；
- `attempt_id` 和尝试次数；
- `worker_id`、`worker_instance_id`、PID、runtime 和 host；
- GPU UUID/物理索引、Lease ID、显存预算、模型和 endpoint；
- 创建、排队、调度、Lease 获取、执行开始、执行结束和结果持久化时间；
- queue、dispatch、execution、persist 和 total 耗时；
- 最终状态和结构化错误。

父文档的 `execution.subtasks` 汇总页段状态及最新分配；完整 attempts 通过 `/executions` 分页查询，避免把大量明细直接塞入业务 `data`。

### 8.2 Event Hub 与 SSE

Execution Event Hub 是任务状态输出的集中枢纽。事件先写入 Redis，再按配置输出结构化 JSON 日志，并供 SSE 查询。业务状态不依赖散落的 `print` 文本。

典型事件包括：

```text
job.accepted
task.queued
task.waiting_for_capacity
task.scheduled
gpu.lease_acquired
task.started
task.completed / task.failed
task.retry_scheduled
shard.queued / shard.running / shard.succeeded / shard.failed
gpu.lease_released
job.completed / job.failed / job.cancelled
```

事件包含作业内单调递增的 `sequence` 和可恢复的 `event_id`。SSE 支持 `Last-Event-ID` 断线续传，并支持 `min_level=TRACE|DEBUG|INFO|WARN|ERROR` 过滤。

日志级别通过以下接口集中管理，并带 TTL，避免临时 TRACE 长期生效：

- `GET /api/v1/observability/levels`
- `PUT /api/v1/observability/levels`

当前已实现按 Job 的 SSE；全局 `/api/v1/events/stream` 因租户鉴权和 fan-out 尚未接入而明确返回 `501`，不能作为现有能力对外承诺。

### 8.3 指标、request_id 与告警

进程内可观测性由 `backend/app/observability.py` 提供：

- **request_id 上下文**：HTTP 入口与执行层通过 contextvars 绑定 request/job id，服务层日志经 `context_prefix(request_id=...)` 附加，未绑定时不输出；用户可凭 `request_id` 与 `stats.extraction.empty_reason` 定位空结果问题。
- **指标端点**：`GET /api/v1/observability/metrics`（不鉴权，暴露方式与 `/health` 一致）输出 `queue_depth`（Redis 队列按各资源流未确认消息数估算）、`job_status_counts`、告警与空抽取计数 `counters`、各阶段近期耗时分位数 `stage_duration_samples`（进程内环形缓冲，每阶段最近 512 次）。执行层不可用时对应字段降级为空值。
- **告警 webhook**：`QASYSTEM_ALERT_WEBHOOK` 配置钉钉/企业微信机器人（text 消息格式），留空则静默跳过；同 event+request_id 60 秒内去重，发送失败只记 ERROR 日志，绝不影响主流程。
- **慢阶段预算**：`QASYSTEM_ALERT_STAGE_SLOW_PARSE_SECONDS` / `_CHUNK_` / `_EXTRACT_` 配置知识沉淀流水线各阶段耗时预算（秒），超出即发 `slow_stage` 告警——只告警、不中断任务；留空或 0 表示不告警。
- **空抽取告警**：抽取结果 0 实体 0 关系且分类出原因时，写入 `stats.extraction.empty_reason`、计数 `empty_extraction_total` 并发送 `empty_extraction` 告警。

## 9. 配置与目标 A40 部署

目标服务器有 8 张 A40，但本服务只使用后四张卡：

```env
GPU_DEVICE_IDS=4,5,6,7
```

该变量是 supervisor 的硬资源边界，必须保留。容器可以具备 NVIDIA GPU 访问能力，但 supervisor 只为上述卡启动 vLLM 和 GPU Worker，并为每个子进程设置单卡可见性。

主要运行参数：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `EXECUTION_MODE` | `async` | 使用统一队列执行；不改变业务 API 默认同步响应 |
| `EXECUTION_REDIS_URL` | `redis://redis:6379/1` | 队列、状态、事件和租约存储 |
| `API_WORKERS` | `2` | Uvicorn 进程数 |
| `EXECUTION_CPU_WORKERS` | `auto` | CPU Worker 数 |
| `EXECUTION_VLM_WORKERS_PER_GPU` | `auto` | 每卡 VLM dispatch Worker 数 |
| `EXECUTION_AUX_WORKERS_PER_GPU` | `1` | 每卡 Embedding/Confidence Worker 数 |
| `VLLM_GPU_MEMORY_UTILIZATION` | `0.82` | 每卡 vLLM engine 显存比例 |
| `VLLM_MAX_NUM_SEQS` | `32` | vLLM engine 并发序列上限 |
| `VLLM_WARMUP_ENABLED` | `true` | readiness 后执行多模态 warmup |
| `EXECUTION_VLM_SHARD_MIN_PAGES` | `8` | 自动分片最小页数 |
| `EXECUTION_VLM_SHARD_COUNT` | `auto` | 按存活 VLM Worker 形成约 1.5 轮页段 backlog |
| `EXECUTION_MAX_ATTEMPTS` | `3` | 单任务最大尝试次数（含崩溃重投） |
| `EXECUTION_REAPER_ENABLED` | `true` | PEL 滞留消息重投开关（崩溃恢复） |
| `EXECUTION_REAPER_MIN_IDLE_SECONDS` | `600` | 消息滞留多久后收回重投 |
| `EXECUTION_REAPER_INTERVAL_SECONDS` | `60` | 重投扫描间隔 |
| `KNOWLEDGE_PIPELINE_PARSE_SHARDING` | `false` | 知识沉淀流水线内 PDF 解析页分片开关 |
| `QASYSTEM_ALERT_WEBHOOK` | 空 | 告警 webhook；留空不发送任何告警 |
| `QASYSTEM_ALERT_STAGE_SLOW_PARSE_SECONDS` / `_CHUNK_` / `_EXTRACT_` | 空 | 各阶段慢预算（秒）；留空或 0 不告警 |
| `EXECUTION_GPU_MAX_LEASES_PER_WORKER` | `1` | 单 Worker 同时执行的 GPU 原子任务数 |
| `EXECUTION_GPU_MEMORY_REQUEST_MB_GPU_VLM` | `2048` | VLM 原子任务调度预算，不是模型常驻显存 |

`VLLM_GPU_MEMORY_UTILIZATION`、`VLLM_MAX_NUM_SEQS`、各资源显存预算、Worker 数和分片阈值仍需在 4 张 A40 上用真实混合流量调优。不能仅因 A40 显存更大就把 vLLM 配到接近 100%；还需为 CUDA context、页预处理峰值、Embedding/Confidence 和安全余量保留空间。

## 10. 故障语义与运维检查

- Redis 不可用：任务不能可靠入队，业务接口返回 `503`，不执行 inline 回退。
- 无即时 GPU 容量：任务回到共享队列并产生 `task.waiting_for_capacity`；VLM 父任务最终受 `EXECUTION_SHARD_TIMEOUT_SECONDS` 控制的页段汇聚超时约束。
- 页段算法失败：该子 Job 按最大尝试次数重试；耗尽后父文档失败并保留失败页段信息。
- vLLM 未就绪：GPU Worker 不启动，runtime 不进入 ready。
- Worker 心跳过期：不再出现在调度候选中；Lease 由释放路径或 TTL 回收。
- 结果尚未完成：`result_url` 返回 `409`，而不是返回不完整 Markdown。
- 客户端断开：已接受的 Redis 任务继续执行，不因 HTTP 连接关闭自动取消。

部署后至少检查：

```bash
curl -fsS http://127.0.0.1:8006/health
curl -fsS http://127.0.0.1:8006/api/v1/resources/gpus
docker compose exec backend sh -lc 'ps -ef | grep -E "uvicorn|vllm|app.execution.worker" | grep -v grep'
docker compose logs --tail=300 backend
```

日志中应先看到每张选中 GPU 的 `ready and warm`，再看到 `runtime ready`。文档解析性能和 3090 阶段性测试结果见 [VLM 大文档解析性能对比报告](../算法/VLM大文档解析性能对比报告.md)，A40 参数与吞吐结论必须以目标机实测为准。

## 11. 已实现能力与后续演进边界

当前交付已经实现：

- 单 Backend 容器 supervisor 自动管理全部 API、CPU、vLLM 和 GPU Worker 进程；
- Redis Streams 共享能力队列、状态、事件、心跳和 Lease；
- VLM PDF 服务端自动分片、跨 GPU 抢占、按页序合并和页段级重试；
- 基于实时显存快照、预算、安全余量、设备 Lease 和 Worker slot 的准入；
- vLLM readiness/warmup；
- 业务 API 同步兼容、显式异步 Jobs API、SSE 和顶层 `execution`；
- MCP 默认同步兼容并进入统一执行层；
- 目标 A40 机器通过 `GPU_DEVICE_IDS=4,5,6,7` 限定后四卡。

尚未实现、不得写入现行能力承诺的演进项包括：运行时副本自动扩缩、同卡多 vLLM 副本自动部署、跨节点调度、基于页面复杂度的预测分片、慢页在线二次拆分，以及自动学习调度参数。是否引入 Kubernetes、Ray Serve、Triton、HAMi 或 NVIDIA Dynamo，应在单机 A40 容量验收后根据确切瓶颈决定；它们不是当前架构的运行依赖。

## 12. 架构设计决策与权衡

以下决策把系统从"一个 API 进程跑所有算法"的常见形态，约束为今天这套多进程、队列化、准入化的执行架构。每一项都记录了当时的取舍，后续演进应尊重这些取舍而不是绕过它们：

| 决策 | 理由 | 代价/边界 |
| --- | --- | --- |
| 重计算不进 API 进程，全部队列化 | API 进程保持无状态、低延迟，副本数可以远小于执行进程数；崩溃影响面以单个任务为界 | Redis 成为吞吐枢纽和单点；同步语义需要线程池等待 |
| 任务不预绑定 GPU，领取后才申请 Lease | 负载均衡由运行时显存快照决定，形成跨卡 work stealing，避免热点卡排队、空闲卡闲置 | 准入依赖心跳快照时效性（nvidia-smi 1s 缓存 + Worker 5s 心跳） |
| 分片数量与显存准入分成两个控制量 | 分片负责跨卡负载均衡与页段级重试，显存负责防超配，二者解耦后可独立调优 | 不按页面复杂度预测耗时，慢页段无法在线二次拆分 |
| 正式部署失败关闭（Redis 不可用返回 503） | 避免半初始化状态下 inline 降级产生不可信结果或部分副作用 | Redis 不可用期间全部任务型接口不可用 |
| 鉴权上下文独立存储，队列只携带随机 ID | 凭证不进入任务产物、日志、SSE 和结果，交付与审计边界清晰 | 执行前必须成功恢复上下文，否则任务失败重试 |
| vLLM engine 常驻 + readiness/warmup 前置 | 首个请求不承担模型编译与冷启动，GPU Worker 只接"已就绪"的请求 | engine 数量静态，不随队列压力扩缩 |
| Worker 单循环同步执行、以进程为并发单位 | 算法库保持同步实现，进程崩溃隔离干净，调试路径单一 | IO 型长尾任务（`io.llm`/`io.db`）会占满整个 Worker，需靠 Worker 数量覆盖 |
| "至少一次"执行语义 + 显式重试与退避 | 崩溃恢复不需要分布式事务，失败路径简单可观测 | 业务副作用需要幂等，重复执行是允许行为 |

这些取舍共同保证了四件事：容量可以被计算（Lease 预算），负载可以被观察（execution 分段耗时），故障可以被重放（事件序列 + SSE 断线续传），演进可以被约束（§11 的边界清单）。这也是当前架构区别于"异步装饰过的单体服务"的核心。
