# 服务并发与 GPU 智能调度架构设计说明

> 状态：单机多 GPU 架构已落地，RTX 3090 环境已完成阶段性验证，目标 A40 环境仍需容量调优与验收
>
> 范围：Backend HTTP API、统一 MCP、文档解析、知识沉淀、Embedding、Confidence 及其统一执行基础设施

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

每次尝试单独记录 `attempt_id`、Worker、进程实例、GPU Lease、排队/开始/结束时间、耗时和错误。执行失败时按 `EXECUTION_MAX_ATTEMPTS` 重试；VLM PDF 的每个页段是独立子 Job，因此失败只会重试相应页段，而不是重新解析整份 PDF。

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

日志中应先看到每张选中 GPU 的 `ready and warm`，再看到 `runtime ready`。文档解析性能和 3090 阶段性测试结果见《VLM大文档解析性能对比报告》，A40 参数与吞吐结论必须以目标机实测为准。

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
