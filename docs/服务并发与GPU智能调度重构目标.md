# 服务并发与 GPU 智能调度重构目标

> 状态：单机目标架构已落地，待真实 A40 容量验收
>
> 范围：Backend HTTP API、统一 MCP、知识沉淀流水线、MinerU/VLM、Embedding、Confidence 及其依赖服务
>
> 目的：记录生产并发目标、已落地组件边界、GPU 调度原则和后续验收标准

当前代码已经落地单机目标架构：统一 HTTP 作业协议、Redis Streams Worker、Execution Event Hub/SSE、顶层 `execution` 字段、失败关闭策略、GPU 容量与 Lease 调度、MCP 重工具统一排队及可选异步句柄、VLM PDF page-sharding、vLLM readiness/warmup，以及单 Backend 容器 supervisor 自动管理 API、CPU Worker、每卡一个 vLLM engine、VLM dispatch Worker 和共享 Embedding/Confidence Worker。正式 Compose 不枚举子进程，也不保留源码挂载。曾用于验证的显式 worker/vLLM services 不属于迁移基线。

## 1. 重构前基线

重构前目标主机有八张 NVIDIA A40，配置选择 `4,5,6,7` 后四卡，但 Backend 内只有一个 Uvicorn 进程。系统没有独立 vLLM 服务或显式 worker/vLLM services，文档解析、切片、图谱抽取、向量化、置信度计算和 MCP 工具均由 Backend 入口进程直接同步执行或通过进程内线程池执行。

当时只完成能力联调，不具备面向多租户并发请求的资源治理能力，主要问题如下：

- `gpus: all` 只让进程看见多张 GPU，不会自动产生多卡调度和并行推理。
- 文档解析使用全局处理器和互斥锁，批量解析最终仍被串行化。
- MinerU 当前固定使用进程内 `vlm-transformers`，没有跨请求连续批处理。
- 一个 HTTP 请求同步完成解析、切片、LLM 抽取、向量化和存储，耗时长且无法可靠恢复。
- 上传文件会整体读入内存，缺少流式落盘和作业级临时目录。
- Embedding、Confidence 等能力分别持有模型和进程内锁，无法统一治理 GPU 显存。
- LLM 并发和速率限制是进程内状态，增加 API 进程后会失效。
- MCP 重任务直接占用 MCP/Backend 进程，无法提供隔离、背压和持久重试。
- 健康检查只验证依赖和模型目录，不能证明 CUDA、模型驻留和 Worker 可接单。

本次重构不以“增加几个线程”或“每张 GPU 固定一个 Worker”为目标，而是建立覆盖所有 HTTP/MCP 能力的持久任务与资源调度架构。

## 2. 已确认的目标决策

1. 所有入口服务无状态化，可以部署多个 API/MCP 实例。
2. 重任务脱离入口进程，通过持久任务或工作流异步执行。
3. 知识沉淀改为可恢复的多阶段工作流，同时保留业务 HTTP 默认同步返回，并提供显式异步提交能力。
4. MinerU 升级到经兼容性验证的新版本，并允许同步升级必要依赖。
5. MinerU VLM 从进程内 Transformers 推理切换为独立 vLLM 服务。
6. vLLM 负责单模型实例内部排队、连续批处理和 KV Cache；平台不重复实现这些能力。
7. 平台统一调度 MinerU VLM、Embedding、Confidence 及后续 GPU 算法。
8. GPU 分配必须显式、可观测、有显存预算，不能依赖 CUDA 自动选卡。
9. 对外采用增量协议：现有业务 API 默认同步兼容，`Prefer: respond-async` 和新增 Jobs API 提供显式异步作业协议，`Prefer: wait=N` 提供显式有界等待。
10. 必须具备租户隔离、优先级、配额、背压、取消、幂等、重试和完整指标。
11. 每个原子任务及其每次执行尝试都必须记录调度决策、Worker/进程、GPU Lease 和完整时间线，并可通过日志、SSE 和查询接口观察。
12. 状态和日志通过集中式 Execution Event Hub 输出；业务进程不得直接使用 `print` 作为状态通道。

## 3. 非目标

- 不在入口进程中为每个接口分别创建线程池或进程池。
- 不通过单纯增加 Uvicorn `--workers` 解决模型并发。
- 不假设容器可见四张 GPU 就能自动利用四张 GPU。
- 不根据一次 NVML 采样的“空闲显存”直接启动模型进程。
- 不要求所有轻量查询都进入任务队列；低延迟 I/O 可以使用异步连接池直接执行。
- 不在本次重构中改变算法的业务语义、图谱协议或知识来源约束。

## 4. 目标架构

```text
HTTP Client / MCP Client / Agent
                │
                ▼
┌─────────────────────────────────────────────┐
│ Stateless API & MCP Gateway                 │
│ 鉴权、校验、流式上传、配额检查、提交/查询任务 │
└──────────────────────┬──────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────┐
│ Durable Workflow & Task Layer               │
│ Temporal：状态、阶段、重试、超时、取消、补偿 │
└───────┬──────────┬──────────┬───────────────┘
        │          │          │
        ▼          ▼          ▼
  CPU Workers   I/O Workers   GPU Capacity Controller
  转换/切片     LLM/DB/存储    预算、租约、路由、扩缩容
                                  │
                   ┌──────────────┴──────────────┐
                   ▼                             ▼
             vLLM VLM Pool              Embedding/Confidence Pool
             MinerU 页面推理             Triton 或专用批处理服务

所有组件 ───────────────▶ Execution Event Hub
                         事件持久化、级别过滤、结构化日志、SSE

共享基础设施：对象存储、PostgreSQL、Redis、Prometheus、DCGM Exporter、Trace
```

### 4.1 入口层

FastAPI Backend 和 MCP Server 只承担：

- 身份认证、租户解析和请求参数校验；
- 文件流式写入对象存储或共享文件存储；
- 幂等键和配额检查；
- 创建任务、查询状态、取消任务和读取结果；
- 对轻量 I/O 操作使用异步连接池；
- 对旧同步调用提供有界等待。

入口层不得加载 MinerU、SentenceTransformer 或其他 CUDA 模型，也不得直接执行文档转换、图谱抽取和图表渲染。

### 4.2 工作流层

推荐使用 Temporal 作为持久工作流引擎。Redis 继续用于分布式限流、配额、短期缓存和协调，但不自行承担工作流可靠性。

Temporal 负责：

- 工作流和阶段状态持久化；
- Worker 异常或节点重启后的恢复；
- 按错误类型执行重试和退避；
- 阶段超时、整单超时和取消传播；
- 并行分支、结果汇聚和失败补偿；
- 工作流版本兼容和执行历史审计。

### 4.3 数据层

- 上传文件、大型中间结果和最终产物存入 MinIO/S3 或共享存储。
- 任务消息只传对象引用、校验和和必要的小型元数据，不传大型 PDF、图片或向量数组。
- PostgreSQL 保存业务任务索引、租户、配额和可检索的结果元数据。
- Redis 保存分布式限流令牌、短期状态缓存和热点查询结果。
- 每个任务使用独立 scratch 目录，路径包含 `tenant_id/job_id/attempt`，成功、失败和取消都必须进入清理流程。

## 5. 能力与资源分类

| 能力 | 资源类别 | 目标执行位置 | 并发策略 |
| --- | --- | --- | --- |
| 文档上传 | Storage I/O | Gateway + 对象存储 | 流式写入、大小限制、校验和 |
| Office/OFD/PDF 转换 | CPU/内存 | CPU Worker | 进程隔离、按核数和内存限并发 |
| PDF 页面渲染和预处理 | CPU/内存 | CPU Worker | 页面流水线、预取、避免 GPU 等待 |
| MinerU VLM | GPU | vLLM Pool | 连续批处理、页面/页段并发 |
| MinerU Pipeline/OCR | CPU/GPU | 对应专用 Worker | 按实际后端建立资源画像 |
| Markdown 切片 | CPU | CPU Worker | 批任务、进程池 |
| Graph Merge | CPU/内存 | CPU Worker | 确定性执行、限制输入规模 |
| 图表生成 | CPU/文件 I/O | Chart Worker | 进程隔离、输出到对象存储 |
| 图谱抽取 | 外部 LLM I/O | LLM Worker | 异步请求、分布式 QPS/TPM 限流 |
| Text/Graph Embedding | GPU 优先 | 统一模型服务 | 动态批处理，不在业务进程逐条 encode |
| Confidence 语义评分 | GPU + ES I/O | I/O Worker + 统一模型服务 | ES 异步查询、文本批量 encode |
| SQL/MCP 数据工具 | DB I/O | DB Worker 或 Gateway | 异步连接池、查询超时、只读策略 |
| Neo4j/MCP 图谱工具 | DB/LLM I/O | DB/LLM Worker | 连接池、并发上限、调用链追踪 |
| Agent | 外部 LLM/MCP I/O | 独立 Agent 服务 | 保留异步任务语义，接入统一配额 |

## 6. MinerU 与 vLLM 重构

### 6.1 运行边界

MinerU 负责文档类型判断、页面准备、结构化结果处理和结果合并。VLM 模型由独立 vLLM 服务常驻加载，MinerU Worker 通过稳定的内部推理协议调用它。

```text
PDF/图片
  → CPU 页面渲染与预处理
  → 页面或页段任务
  → vLLM 连续批处理
  → 页面结构化结果
  → 按原页码顺序合并
```

同一个大文档允许按页段并行，但必须保证：

- 页码、坐标和跨页元素信息可恢复；
- 分片失败只重试对应页段；
- 合并结果稳定且与执行完成顺序无关；
- 分片粒度可配置并根据基准测试调整；
- 对确有跨页依赖的步骤保留合并后处理。

### 6.2 依赖升级原则

MinerU、vLLM、Torch、CUDA 和 Transformers 必须形成经过验证的版本矩阵，不能在统一 Backend 镜像中独立升级。

目标镜像至少拆分为：

| 镜像 | 主要依赖 |
| --- | --- |
| API/MCP Gateway | FastAPI、MCP、轻量客户端，不包含 CUDA 模型 |
| MinerU Worker | 新版 MinerU、文档转换和结果处理依赖 |
| vLLM Server | 官方兼容的 vLLM、Torch、CUDA、VLM 模型 |
| Embedding Server | Triton 或专用推理运行时、向量模型 |
| CPU Worker | LibreOffice、OFD、切片、合并、图表依赖 |

版本升级必须通过离线构建、模型校验、真实文档回归和性能门禁。生产环境禁止启动时联网下载模型。

## 7. GPU 智能调度

### 7.1 调度职责边界

GPU 调度分为三个时间尺度：

1. 毫秒级：vLLM 在单实例内完成排队、连续批处理和显存缓存管理。
2. 秒级：请求路由器选择模型实例，执行页面分配、优先级和准入控制。
3. 分钟级：容量控制器根据队列压力和 SLO 扩缩模型服务副本。

平台不得绕过 vLLM 再实现请求级 batcher，也不得因为瞬时 GPU 利用率低就创建重复模型副本。

### 7.2 GPU 资源画像

每个算法或模型版本必须登记资源画像：

```json
{
  "resource_class": "gpu",
  "model": "mineru-vlm",
  "model_version": "pinned-version",
  "resident_memory_mb": 0,
  "request_peak_memory_p99_mb": 0,
  "supports_batching": true,
  "max_concurrency": 0,
  "cold_start_seconds": 0,
  "priority_class": "batch"
}
```

数值由目标 A40 上的基准测试生成，不在代码中凭经验写死。

### 7.3 显存预算与租约

vLLM 会根据 `gpu_memory_utilization` 预留 KV Cache，因此 NVML 中的已用显存不等于实时业务负载。GPU Capacity Controller 必须同时使用：

- 配置的模型常驻预算；
- vLLM KV Cache 预算；
- 该任务类型的 P99 峰值显存；
- 已发放但尚未反映到 NVML 的 GPU Lease；
- 驱动、CUDA Context 和安全余量；
- NVML 实际显存、利用率、温度、功耗和错误状态。

准入条件为：

```text
可分配预算 - 活跃租约 >= 任务 P99 峰值 + 安全余量
```

NVML 用于校验、异常检测和模型画像更新，不能作为唯一准入依据。

### 7.4 扩缩容策略

扩容主要依据排队和业务 SLO：

```text
队列持续积压
AND P95 排队时间超过目标
AND 到达速率持续高于当前稳定吞吐
AND 存在满足显存预算的 GPU
AND 上一次扩容带来了有效吞吐增益
```

扩容顺序为：

1. 调整单 vLLM 实例的动态批量和并发序列；
2. 增加空闲 GPU 上的 vLLM 服务副本；
3. 只有压测证明有收益且显存隔离可靠时，才允许同卡部署第二个相同模型副本；
4. 单机容量不足时扩展到其他 GPU 节点。

缩容必须设置空闲窗口和冷却时间，只在任务边界排空副本。至少保留一个热 VLM 副本，避免每次请求触发模型冷启动。

### 7.5 模型亲和与公平性

路由评分至少考虑：

- 目标模型是否已经驻留；
- 当前队列等待和活跃批次；
- 显存预算和碎片风险；
- 任务优先级和 deadline；
- 冷启动与模型切换成本；
- 租户权重、已用配额和公平性；
- GPU 健康、拓扑和历史吞吐。

在线交互任务优先于批量知识导入。批任务使用页面或页段作为让位边界，不强杀正在执行的 CUDA Kernel。

### 7.6 四张 A40 的初始策略

初始部署不永久绑定“某张卡只属于某个算法”，而是设置最低热容量和可借用容量：

- 至少一个 VLM 热副本；
- 至少一个统一 Embedding/Confidence 热服务；
- 其余容量由 VLM、Embedding 和后续 GPU 算法按队列压力借用；
- 同卡多模型必须先通过显存和吞吐压测；
- VLM 模型单卡可容纳时不使用 Tensor Parallel，优先使用数据并行副本；
- 每个服务副本在启动前完成 GPU 绑定，readiness 通过后才接收流量。

具体的初始卡数和 `gpu_memory_utilization` 不在目标文档中固定，由性能基线决定。

## 8. 组件选型边界

| 层次 | 目标组件 | 负责 | 不负责 |
| --- | --- | --- | --- |
| 业务工作流 | Temporal | 阶段状态、重试、取消、恢复 | CUDA 调度、模型 batching |
| 模型服务控制 | Ray Serve | 副本生命周期、路由、扩缩容 | 严格显存隔离、业务工作流 |
| VLM 推理 | vLLM | 连续批处理、模型执行、KV Cache | 文档工作流、租户配额 |
| 向量模型推理 | Triton 或专用服务 | 动态批处理、模型实例、指标 | 业务任务编排 |
| GPU 容量控制 | GPU Capacity Controller | 预算、Lease、亲和、优先级 | 单模型内部 batching |
| 集群服务入口 | KServe（Kubernetes 阶段） | 发布、流量、灰度、集群扩缩 | 业务阶段状态 |
| GPU 共享隔离 | HAMi（验证后采用） | 显存/算力切分、设备感知调度 | 推理吞吐优化 |
| 多节点高级推理 | NVIDIA Dynamo（规模化阶段评估） | 智能路由、Planner、多节点编排 | 当前单机必选依赖 |

MinerU 自带 Router 可以作为 MinerU 领域内的兼容入口，但不能与 Ray Serve/GPU Capacity Controller 同时独立决定副本和全局 GPU 分配。系统级调度只有一个权威控制面。

## 9. 任务与接口协议

### 9.1 通用任务字段

```json
{
  "job_id": "uuid",
  "tenant_id": "tenant",
  "operation": "document.parse",
  "resource_class": "gpu.vlm",
  "priority": 50,
  "idempotency_key": "caller-provided-or-derived",
  "payload_ref": "s3://bucket/object",
  "result_ref": null,
  "status": "queued",
  "stage": "preprocess",
  "progress": {"completed": 0, "total": 40, "unit": "page"},
  "attempt": 1,
  "deadline_at": null,
  "created_at": "RFC3339",
  "started_at": null,
  "finished_at": null,
  "error": null
}
```

状态至少包括：

```text
accepted → queued → running → succeeded
                         ├── failed
                         ├── cancelling → cancelled
                         └── retry_wait → queued
```

### 9.2 原子任务执行记录

工作流拆出的每个页面、页段、文档、切片、批次或存储操作都是一个可追踪原子任务。任务的每次执行或重试使用不同的 `attempt_id`，禁止用后一次重试覆盖前一次记录。

```json
{
  "task_id": "uuid",
  "parent_task_id": "uuid-or-null",
  "attempt_id": "uuid",
  "attempt": 1,
  "operation": "document.vlm.page_range",
  "partition": {"document_id": "doc-id", "page_start": 0, "page_end": 7},
  "status": "running",
  "queue": "gpu.vlm.standard",
  "allocation": {
    "scheduler_id": "gpu-scheduler-1",
    "worker_id": "vlm-worker-2",
    "worker_instance_id": "instance-uuid",
    "process_id": 1234,
    "runtime": "container-or-pod-id",
    "host_id": "gpu-node-1",
    "gpu_allocations": [
      {
        "device_uuid": "GPU-stable-uuid",
        "physical_index": 2,
        "logical_index": 0,
        "lease_id": "lease-uuid",
        "memory_budget_mb": 28672
      }
    ],
    "model": "mineru-vlm",
    "model_version": "pinned-version",
    "model_instance_id": "vllm-replica-2",
    "inference_request_id": "request-uuid",
    "batch_id": "optional-engine-batch-id"
  },
  "timestamps": {
    "created_at": "RFC3339",
    "queued_at": "RFC3339",
    "scheduled_at": "RFC3339",
    "lease_acquired_at": "RFC3339",
    "worker_started_at": "RFC3339",
    "execution_started_at": "RFC3339",
    "execution_finished_at": null,
    "result_persisted_at": null
  },
  "durations_ms": {
    "queue": 0,
    "dispatch": 0,
    "execution": null,
    "persist": null,
    "total": null
  },
  "error": null
}
```

字段语义要求：

- `worker_id` 表示逻辑 Worker，`worker_instance_id` 标识一次具体进程生命周期。
- `process_id` 只在该运行实例内有意义，跨重启定位使用 `worker_instance_id`。
- GPU 主键使用稳定的 `device_uuid`；物理和逻辑序号仅用于现场排障。
- 一个任务可能使用多张 GPU，因此使用 `gpu_allocations` 数组，而不是单个 `gpu_id`。
- vLLM 请求必须透传 `inference_request_id`；能稳定获得批次标识时记录 `batch_id`，不能依赖私有引擎字段作为契约必填项。
- 时间戳使用 UTC RFC 3339；耗时使用同进程单调时钟计算，避免系统时钟校准产生负值。
- 调度失败、租约拒绝和取消也必须产生执行记录，不得只记录成功分配。

### 9.3 HTTP 协议

目标接口包括：

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| `POST` | `/api/v1/jobs` | 通用任务提交 |
| `GET` | `/api/v1/jobs/{job_id}` | 状态、阶段和进度 |
| `GET` | `/api/v1/jobs/{job_id}/result` | 获取结果或结果引用 |
| `DELETE` | `/api/v1/jobs/{job_id}` | 请求取消 |
| `GET` | `/api/v1/jobs/{job_id}/events` | SSE 任务事件 |
| `GET` | `/api/v1/jobs/{job_id}/executions` | 分页查询原子任务、执行尝试和资源分配 |

现有业务路径继续保留且默认同步等待，返回原 HTTP 状态和原业务 `data`，顶层只增量增加 `execution`。调用方发送 `Prefer: respond-async` 时立即获得 `202 Accepted` 和 `job_id`；发送 `Prefer: wait=<seconds>` 时请求显式有界等待，在时限内完成则返回原业务结果，否则返回 `202`，任务继续执行。新增 `POST /api/v1/jobs` 是显式任务入口，默认返回 `202`。

服务必须限制最大等待时间，客户端断开不能导致持久任务自动取消。

任务提交响应只能返回当时已经确定的信息，不能预先声称任务会分配到某个 GPU。分配发生后，调用方通过以下方式获得调度信息：

- `GET /jobs/{job_id}` 返回当前阶段和精简的最新分配状态；
- `GET /jobs/{job_id}/events` 通过 SSE 实时推送排队、分配、开始、进度、结束和重试事件；
- `GET /jobs/{job_id}/executions` 分页返回完整原子任务和全部 attempts；
- 最终结果的响应 envelope 顶层 `execution.summary` 返回总任务数、重试数、总排队/执行时间、参与的逻辑 Worker 和 GPU；
- 详细记录不嵌入大型业务结果，顶层 `execution` 同时返回 `executions_url`。

所有统一响应都保留原有顶层 `code`、`info`、`data` 和 `error` 字段，并额外增加顶层 `execution` 字段。新增字段不改变 `data` 的业务结构。以下是调用方显式请求 `Prefer: respond-async` 后的任务句柄示例：

```json
{
  "code": 0,
  "info": "文档解析完成",
  "data": {},
  "error": null,
  "execution": {
    "mode": "async",
    "job_id": "job-uuid",
    "status": "running",
    "stage": "document.vlm",
    "latest_event_id": "job-uuid:42",
    "summary": {
      "total_tasks": 40,
      "completed_tasks": 12,
      "retry_count": 1,
      "queue_ms": 1820,
      "execution_ms": null
    },
    "events_url": "/api/v1/jobs/job-uuid/events",
    "executions_url": "/api/v1/jobs/job-uuid/executions",
    "result_url": "/api/v1/jobs/job-uuid/result"
  }
}
```

`execution.mode` 描述任务实际由统一执行层调度，不决定业务 HTTP 接口是否立即返回。默认同步业务响应中任务已完成，保留原业务 `data`；只有 `Prefer: respond-async`、有界等待超时或显式 Jobs API 才返回 `202` 和任务句柄。显式异步调用在 SSE `job.completed` 后请求 `execution.result_url`，得到包含最终业务 `data` 与最终 `execution` 的完整 envelope。原有调用方忽略未知顶层 `execution` 字段即可继续解析 `data`。

所有正式 `/api/v1` 接口和 deprecated aliases 都遵循该规则：请求在参数校验或配额检查阶段被拒绝且尚未创建任务时，`execution` 为 `null`；任务一旦接受，后续成功、失败、取消和超时响应都必须保留 `execution` 对象。

SSE 至少包含以下事件：

```text
job.accepted
task.queued
task.scheduled
gpu.lease_acquired
task.started
task.progress
task.completed | task.failed | task.cancelled
task.retry_scheduled
job.completed | job.failed | job.cancelled
```

事件必须包含单调递增的作业内 `sequence` 和唯一 `event_id`，支持客户端通过 `Last-Event-ID` 断线续传。API 返回的执行明细使用游标分页，防止超长文档将数千个页面任务一次性塞入响应。

外部普通调用方默认只看到逻辑 Worker、模型实例和脱敏 GPU 标识。物理主机名、容器 ID、PID、完整 GPU UUID 和显存快照仅对运维权限开放，避免暴露基础设施拓扑。

### 9.4 MCP 协议

MCP 工具分为两类：

- 轻量原子查询：使用异步连接池直接返回，但必须有超时、并发上限和租户配额。
- 解析、批量抽取、图表等重任务：返回任务句柄，并提供 `get_job`、`get_job_result` 和 `cancel_job` 原子工具。

为兼容现有 Agent，原工具可进行服务端有界等待；超过上限返回可轮询任务句柄。Agent 不应通过长时间占用一个 MCP 请求等待批任务完成。

## 10. 知识沉淀工作流

目标工作流为：

```text
文件流式落盘
  → 类型识别和转换
  → 页面渲染/预处理
  → VLM 页段并行推理
  → 按页合并 Markdown
  → 文档切片
  → LLM 分片抽取
  → Graph Merge
  → Embedding
  → 图谱/向量/文件存储
  → 清理临时资源
```

要求：

- 每个阶段拥有独立超时和重试策略；
- 解析页段和 LLM 分片可以单独重试；
- 外部存储写入使用幂等键；
- 已完成阶段不因 Worker 重启重复执行副作用；
- 支持部分进度查询，但正式结果只在一致性条件满足后发布；
- 取消请求传播到未开始的子任务，正在执行的 GPU 页段在安全边界结束；
- 清理活动采用独立重试策略，不能因主任务失败而跳过。

## 11. 背压、限流和失败模型

### 11.1 背压

平台按租户和资源类别限制：

- 在途任务数；
- 排队任务数；
- 单任务文件数、文件大小和页数；
- GPU 页段并行度；
- LLM QPS、TPM/QPM 和并发；
- 数据库连接和查询并发；
- 结果保存时间和存储配额。

超过配额时明确返回 `429`；平台容量不可用时返回 `503` 和可重试信息。禁止无限制接收任务后在内存中排队。

### 11.2 重试

- 参数、格式和确定性算法错误不重试。
- 网络超时、依赖限流和 Worker 丢失使用指数退避和随机抖动。
- GPU OOM 首次触发画像修正和降并发；不得在相同条件下无限重试。
- 数据库和对象存储写入必须先具备幂等性再允许自动重试。
- 达到最大尝试次数后保存结构化错误和阶段上下文。

### 11.3 故障隔离

- Gateway 故障不影响已提交任务。
- 单个 Worker 或 GPU 故障只迁移对应任务或页段。
- vLLM 副本 readiness 失败时不接流量，并从可选路由中移除。
- Redis 故障影响限流和缓存时采用失败关闭策略，不绕过租户配额。
- Temporal、对象存储和业务数据库必须纳入备份和恢复演练。

## 12. 可观测性

所有指标至少按 `operation`、`resource_class`、`model_version`、`worker_pool` 和脱敏后的 `tenant` 聚合。

### 12.1 任务指标

- 提交量、成功量、失败量、取消量和重试量；
- 队列深度、队列等待时间和端到端延迟；
- 各工作流阶段耗时；
- 每页、每文档和每批次吞吐；
- 幂等命中、配额拒绝和背压次数。

### 12.2 GPU/vLLM 指标

- GPU 利用率、显存使用、功耗、温度和错误；
- vLLM Running/Waiting 请求数；
- 实际批大小、并发序列和 token/image 吞吐；
- 模型副本、冷启动、扩缩容和路由分布；
- GPU Lease、预算、超配拒绝和 OOM；
- 页面预处理完成到 GPU 开始执行的等待时间。

### 12.3 外部依赖指标

- LLM 请求延迟、限流、重试和 token 使用量；
- SQL/Neo4j/ES 连接池等待和查询延迟；
- 对象存储吞吐、错误和空间使用；
- Temporal Activity/Workflow 延迟和失败。

### 12.4 Execution Event Hub

Execution Event Hub 是状态打印、执行审计和前端实时展示的唯一事件枢纽。Worker、Scheduler、Gateway 和 Workflow Activity 通过统一事件客户端上报，不直接 `print` 任务状态。

事件枢纽至少提供以下能力：

- 接收并校验统一事件 envelope，补充服务、实例、版本和时间信息；
- 将事件写入按 `job_id`/`workflow_id` 分区的持久流，支持游标和断线续传；
- 根据级别、租户权限和任务范围过滤事件；
- 将同一事件分发到结构化 stdout、日志收集器、OpenTelemetry 和 SSE；
- 对慢速或断开的 SSE 客户端执行有界缓冲，客户端通过 `Last-Event-ID` 从持久流补发；
- 配置事件保留期、归档、脱敏和采样策略；
- 在枢纽不可用时按事件类型采取策略：任务状态审计事件短暂缓冲或失败关闭，普通调试日志可降级丢弃。

统一事件 envelope 示例：

```json
{
  "event_id": "event-uuid",
  "sequence": 42,
  "timestamp": "RFC3339",
  "level": "INFO",
  "event": "task.started",
  "source": {
    "service": "gpu-worker",
    "worker_id": "vlm-worker-2",
    "worker_instance_id": "instance-uuid"
  },
  "job_id": "job-uuid",
  "workflow_id": "workflow-uuid",
  "task_id": "task-uuid",
  "attempt_id": "attempt-uuid",
  "operation": "document.vlm.page_range",
  "stage": "document.vlm",
  "message": "页段开始执行",
  "details": {
    "gpu_device_uuid": "GPU-stable-uuid",
    "logical_gpu_index": 0,
    "model_instance_id": "vllm-replica-2"
  }
}
```

结构化 stdout Sink 使用逐行 JSON 和无缓冲/及时 flush 配置，以便容器日志采集器实时接收；这仍然是 Event Hub 的输出，不是业务代码直接打印。

事件审计和普通日志分开处理：原子任务生命周期事件不采样；高频诊断日志允许按级别和采样策略控制。

调度器在以下状态变化时必须立即向枢纽生成结构化事件：

- 原子任务创建和入队；
- Scheduler 选中或拒绝候选 Worker；
- GPU Lease 获取、续租、释放或过期；
- Worker/进程接收任务；
- 算法或推理实际开始和结束；
- 结果持久化完成；
- 失败、取消、超时和安排重试。

事件先写入可靠事件源或通过事务 Outbox 提交，再异步输出为 JSON 日志、Trace Span 和 SSE。标准输出只是一个可配置 Sink，不是状态真相来源，避免出现“日志打印成功但任务状态未持久化”的不一致。

### 12.5 日志级别与实时订阅

支持的级别从低到高为：

```text
TRACE < DEBUG < INFO < WARN < ERROR
```

默认级别为 `INFO`。级别配置必须支持全局、服务、Worker Pool、租户和单个 `job_id` 的覆盖，并有自动过期时间，防止为单个任务打开 TRACE 后长期产生海量日志。

建议提供以下管理和订阅能力：

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| `GET` | `/api/v1/observability/levels` | 查询当前级别配置 |
| `PUT` | `/api/v1/observability/levels` | 运维权限修改级别和过期时间 |
| `GET` | `/api/v1/jobs/{job_id}/events?min_level=INFO` | 按任务实时订阅事件 SSE |
| `GET` | `/api/v1/events/stream?scope=tenant` | 运维或管理员订阅范围事件 SSE |

前端通常订阅任务级 SSE：

```text
event: execution
id: job-uuid:42
data: {"level":"INFO","event":"task.started",...}
```

`TRACE` 事件可包含候选 GPU、调度评分、显存快照、批大小和路由原因等诊断字段；`INFO/WARN/ERROR` 只返回前端展示所需的摘要。前端不应依赖日志文本解析状态，而应使用 `event`、`status`、`sequence` 和结构化字段。

每条事件和结构化日志至少携带：

```text
timestamp, level, event, event_id, sequence,
trace_id, job_id, workflow_id, task_id, parent_task_id, attempt_id,
operation, stage, queue, priority,
scheduler_id, worker_id, worker_instance_id, process_id,
host_id, runtime, device_uuid, logical_gpu_index, lease_id,
model, model_version, model_instance_id, inference_request_id,
status, duration_ms, error_type
```

页面级原子任务的审计事件不得采样；Trace 可以按策略采样，但失败、超时、重试和 OOM Trace 必须保留。Prometheus 指标不得把 `job_id`、`task_id`、`attempt_id` 等高基数字段作为 Label，高基数检索由日志、Trace 和执行记录接口承担。

详细执行记录设置可配置的保留期并支持归档。任务元数据删除前必须解除对象存储引用；涉及审计要求的租户可以配置更长保留期。

日志和 Trace 不得记录文件正文、密钥或完整数据库连接串。详细基础设施字段遵守运维权限和脱敏规则。

## 13. 健康与就绪

健康检查拆分为：

- Liveness：进程事件循环或 Worker 主循环仍运行。
- Readiness：依赖可连接、队列可消费、模型已加载并完成最小预热。
- Capacity：当前是否仍能接收对应资源类别的新任务。

GPU Worker readiness 至少验证：

- CUDA 可用且绑定的 GPU 与配置一致；
- 模型版本和校验和正确；
- 完成一次小型真实推理；
- 显存预算未超限；
- Worker 已注册且能续租。

`/health` 不应同步执行所有昂贵检查；完整诊断使用独立的 readiness/capacity 接口或缓存探针结果。

## 14. 安全与多租户

- 所有任务、对象和结果都绑定 `tenant_id`，查询必须重新鉴权。
- 对象存储路径和签名 URL 不允许跨租户复用。
- SQL MCP 默认只读，并限制语句类型、执行时间和返回行数。
- 文件处理在隔离目录执行，禁止使用原始文件名拼接路径。
- 限制压缩包展开大小、页面数、图片尺寸和解析总时长。
- 任务事件和错误详情必须脱敏。
- 管理面操作和用户任务使用不同权限及网络入口。

## 15. 部署演进

### 15.1 八卡主机使用后四卡的目标部署

第一目标环境是一台八卡 A40 服务器，默认 `GPU_DEVICE_IDS=4,5,6,7`，只允许使用后四卡。交付为一个完整 Backend 镜像加 Agent 和 Redis；Backend supervisor 仅在选中的卡内启动每卡一个常驻 vLLM engine，并根据 CPU 和所选 GPU 数量派生 API、CPU、VLM dispatch 和辅助 Worker 数量。运维只管理一个 Backend 容器，不手工枚举子进程；模型以只读宿主机目录挂载，执行数据和 Redis 使用持久卷。

该拓扑已经包含单机生产所需的进程隔离、队列背压、原子任务重试、显存/利用率/Lease 调度和任务级可观测性。`GPU_DEVICE_IDS=4,5,6,7` 是目标机资源边界，不由 supervisor 扩展；Worker 数量和 vLLM 容量参数默认由入口进程在这四卡范围内智能派生。

### 15.2 多节点可选演进

- 单机容量或故障域无法满足 SLO 时，再将相同任务协议迁移到 Kubernetes、KServe 或 Ray Serve；
- KEDA/自定义控制器可根据队列与 SLO 指标扩缩容器副本；
- HAMi 是否用于显存/算力切分，必须先验证 A40、驱动、CUDA 和 vLLM 兼容性；
- Volcano/Kueue 用于批量导入等离线任务的配额和优先级；
- 多节点规模达到需要时再评估 NVIDIA Dynamo，当前不作为单机重构前置依赖。

## 16. 迁移阶段

迁移按能力闭环推进，正式交付只保留统一架构和唯一 Compose，不维护额外部署变体。

### 阶段 A：建立基线

- 对当前版本记录阶段耗时、GPU 指标、CPU/内存、吞吐和结果质量；
- 使用固定真实文档集，覆盖文本 PDF、扫描件、表格、公式、Office 和 OFD；
- 建立单文档和并发 `1/2/4/8/16` 的可重复测试。

### 阶段 B：基础平台（已完成）

- 建立任务协议、Redis 持久队列、执行产物存储和可观测性；
- Gateway 实现提交、状态、结果、取消和 SSE；
- 建立 CPU、I/O 和 GPU Worker 注册与健康模型。

### 阶段 C：MinerU/vLLM（已完成代码与 3090 验证，待 A40 验收）

- 确定 MinerU/vLLM/Torch/CUDA 兼容矩阵；
- 将模型推理从 Backend/MinerU 进程拆出；
- 完成页面流水线、vLLM 动态批处理和结果顺序合并；
- 完成单卡参数搜索和四卡路由压测。

### 阶段 D：迁移全部能力（统一执行入口已完成，混合流量容量验收待完成）

- Embedding/Confidence 迁入统一批处理模型服务；
- LLM 抽取使用分布式限流；
- 图表、转换、切片、Graph Merge 迁入 CPU Worker；
- SQL、Neo4j、ES 和存储接入连接池、配额和 Trace；
- MCP 重工具切换为任务句柄协议。

### 阶段 E：切流与清理（交付结构已完成，生产切流待执行）

- 影子流量比较新旧结果和性能；
- 按租户灰度切换；
- 验证回滚和在途任务兼容；
- 删除全局模型锁、进程内任务执行和重复调度器；
- 更新接口、部署、运维和离线交付文档。

## 17. 验收标准

### 17.1 正确性

- 固定回归集的新旧解析结果达到约定的一致性或质量提升标准；
- 并行页段合并结果稳定，不因完成顺序变化；
- 重试不产生重复图谱、向量或文件记录；
- 取消、超时、Worker 重启和 GPU 故障均有确定状态。
- 每个原子任务及其每次重试都能查询到独立执行记录，记录的资源分配和终态与实际执行一致。

### 17.2 性能

- 分别建立当前 `vlm-transformers`、单卡 vLLM 和四卡 vLLM 的基线；
- 队列充足时，四卡聚合吞吐达到经评审的单卡扩展效率目标；
- 相比当前串行实现，吞吐提升可以超过四倍，但不得将其误写为相对已饱和单卡的线性物理上限；
- GPU 饱和度、页吞吐和 P95 延迟同时达标，不能只优化 GPU 利用率；
- CPU 预处理能持续供给 GPU，不出现 GPU 长时间等待输入；
- 所有 HTTP/MCP 能力完成独立和混合流量压测。

具体数值在阶段 A 基线完成后写入性能验收表，并作为上线门禁。

### 17.3 稳定性

- 持续压测期间无 GPU OOM、无无限队列、无入口进程内存持续增长；
- 单 Worker、单 GPU 和单 Gateway 故障不丢失已接受任务；
- 达到配额时按协议背压，不拖垮健康请求；
- 模型扩缩容不会导致请求路由到未就绪实例；
- 临时文件和过期结果按策略清理。

### 17.4 运维

- 可以按 `job_id` 定位完整执行链路；
- 可以实时订阅原子任务的排队、分配、开始、进度和结束事件，断线后按 `Last-Event-ID` 恢复；
- 可以查看每张 GPU 当前模型、预算、Lease、队列和吞吐；
- 可以调整租户配额、任务优先级和模型容量而无需修改业务代码；
- 离线环境能够构建、安装、启动和完成真实样本验收。

## 18. 上线前仍需确认的参数

以下内容属于压测和环境决策，不阻塞目标架构确认：

1. 已锁定的 MinerU、vLLM、Torch 和 CUDA 版本在目标 A40 驱动上的兼容验证。
2. A40 上 vLLM 的最优 `gpu_memory_utilization`、批大小、并发序列和精度类型。
3. PDF 页段大小及跨页元素合并规则。
4. 各租户的 SLO、优先级、并发和存储配额。
5. `Prefer: respond-async` 和 `Prefer: wait` 的调用方采用范围。
6. 单机容量不足时是否进入 Kubernetes/Ray Serve 多节点演进。

上述参数必须通过基准测试、故障演练和容量评审确定，不能用默认值直接进入生产。

## 19. 参考项目

- [MinerU](https://github.com/opendatalab/MinerU)：文档解析、VLM Engine 和 MinerU Router。
- [vLLM](https://github.com/vllm-project/vllm)：VLM/LLM 连续批处理和模型服务。
- [Temporal](https://temporal.io/)：持久工作流和 Activity 执行。
- [Ray Serve](https://docs.ray.io/en/latest/serve/)：模型服务部署、路由和自动扩缩。
- [NVIDIA Triton Inference Server](https://github.com/triton-inference-server/server)：动态批处理、多模型推理和指标。
- [KServe](https://github.com/kserve/kserve)：Kubernetes 模型服务控制面。
- [HAMi](https://github.com/Project-HAMi/HAMi)：Kubernetes GPU 共享、隔离和设备感知调度。
- [Volcano](https://github.com/volcano-sh/volcano)：Kubernetes 批任务队列和调度。
- [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo)：多节点推理编排、智能路由和容量规划。
