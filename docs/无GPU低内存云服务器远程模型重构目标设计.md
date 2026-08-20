# 无 GPU 低内存云服务器远程模型重构目标设计

## 1. 文档目的

本文定义 QASystem 算法服务面向甲方无 GPU、可用内存不足 3 GiB 云服务器的重构目标、系统边界、目标架构、外部服务契约、资源约束和验收标准，用于方案评审、任务拆分、接口联调和部署验收。

本文描述目标状态，不作为当前版本已经具备全部能力的声明。实际接口路径和字段应在甲方外部模型服务协议确认后固化，并同步更新[算法接口协议](算法接口协议.md)、[环境变量与配置加载说明](环境变量与配置加载说明.md)和契约测试。

## 2. 背景与约束

甲方部署环境具有以下硬约束：

- 云服务器不提供 GPU，算法容器不得依赖 CUDA、NVIDIA Runtime 或宿主机模型目录。
- 算法服务可用内存不足 3 GiB，应为操作系统、容器运行时和瞬时请求保留安全余量。
- LLM、Embedding、OCR、版面分析、VLM、重排序及其他依赖模型权重的能力均通过外部 URL 服务完成。
- 应用源代码继续通过 `QASYSTEM_SOURCE_DIR` 只读挂载，模型不得打入 `qasystem-env-v1`，也不得随源码发布。
- 应用团队服务统一使用 `APPLICATION_SERVICE_URL`；`FILE_SERVICE_URL` 只作为兼容回退变量。
- 外部服务端点集中注册，算法模块不得分散拼接第三方路径。
- 经过队列执行的外部认证调用只在任务记录中携带 `auth_context_id`，不得把 Authorization 等鉴权头写入任务 payload、日志、SSE、`execution` 或结果。
- SQL 可继续保留已配置的直连兼容；不得恢复 Neo4j 或 Elasticsearch 直连。

## 3. 重构目标

### 3.1 功能目标

重构完成后，同一套应用源码应支持两种明确的部署模式：

| 模式 | 适用环境 | 模型执行位置 | 模型挂载 | GPU 进程 |
| --- | --- | --- | --- | --- |
| `remote-only` | 甲方低内存 CPU 云服务器 | 全部在外部服务 | 不挂载 `/models` | 不启动 |
| `full-runtime` | 现有 GPU/离线部署环境 | 保留现行本地或外部组合 | 按现有规则只读挂载 | 按配置启动 |

`remote-only` 模式必须满足：

- 知识抽取、问答辅助和 Agent LLM 只调用外部 LLM URL。
- 文本和图谱向量化只调用外部 Embedding URL。
- PDF、OFD、Office、图片的 OCR、版面分析、公式识别、表格识别和 VLM 解析只调用外部文档解析 URL。
- 置信度、重排序等尚未开放的模型能力在未配置外部接口时明确返回“能力不可用”，不得静默加载本地模型或伪造结果。
- Markdown、TXT 等无需模型的轻量处理可继续在本地执行。
- 切片、图谱结果合并、JSON 校验、协议转换和任务编排继续由算法服务执行。

### 3.2 资源目标

甲方部署实例的目标资源预算如下：

| 指标 | 目标值 | 说明 |
| --- | --- | --- |
| 容器内存限制 | 不高于 2.75 GiB | 为宿主机和容器运行时保留空间 |
| 空闲稳定 RSS | 不高于 900 MiB | 服务启动且无任务时 |
| 单任务稳定 RSS | 不高于 2.0 GiB | 普通文本、文档转发和结果编排 |
| 短时峰值 RSS | 不高于 2.5 GiB | 不含甲方主动提交的超限大文件 |
| API Worker | 默认 1 | 避免每进程重复导入和缓存 |
| CPU 任务 Worker | 默认 1 | 后续以压测结果决定是否允许 2 |
| 默认重型任务并发 | 1 | 文档解析、批量向量化和知识抽取分别限流 |

资源验收以容器实际 RSS 和 OOM 记录为准，不能只依据 Python 对象统计或镜像大小判断。

### 3.3 兼容目标

- 保持现有 `/api/v1` 对外业务接口和顶层错误响应结构稳定。
- 保持任务队列、SSE 事件和 `execution` 字段的现有契约。
- 外部模型服务的供应商协议由适配层消化，不把供应商字段泄漏到公共业务接口。
- `full-runtime` 模式不因本次重构失去现有 GPU、MinerU、Docling 或本地 Embedding 能力。

## 4. 非目标

本次重构不负责：

- 在甲方 CPU 云服务器上运行任何量化模型或 CPU 推理模型。
- 新建或维护外部模型集群、GPU 调度平台以及模型权重发布系统。
- 恢复算法服务对 Neo4j、Elasticsearch 的直接访问。
- 在外部置信度、图谱写入、文档证据等接口未开放时实现功能性替代。
- 将应用源码、模型、密钥或运行数据烘焙进 `qasystem-env-v1`。

## 5. 当前状态与主要差距

| 能力 | 当前状态 | 目标状态 | 主要差距 |
| --- | --- | --- | --- |
| 知识抽取 LLM | 已支持外部 OpenAI 兼容 URL | 强制远程 | 增加启动校验、统一重试与可观测性 |
| 图谱检索 LLM | 已支持外部 URL | 强制远程 | 统一客户端、超时和错误映射 |
| Agent LLM | 已支持外部 URL | 强制远程 | 低并发默认值和启动校验 |
| 文本 Embedding | 本地 `SentenceTransformer` | 外部 Embedding 服务 | 移除远程模式下的 Torch 导入和本地模型加载 |
| 语义切片 Embedding | 复用本地向量模型 | 批量调用外部 Embedding 服务 | 控制批大小、请求大小和调用次数 |
| VLM 文档解析 | 已有 `vlm-http-client` 基础 | 完整远程解析 | 解开 MinerU 本地初始化与远程调用的耦合 |
| MinerU pipeline | 本地模型推理 | 外部文档解析服务 | 增加统一文件上传/任务轮询适配 |
| Docling | 本地模型和重型依赖 | 外部文档解析或禁用 | 避免服务启动时导入和检查本地模型 |
| 置信度计算 | 外部接口未开放，显式失败 | 保持显式失败或接入远程接口 | 固化外部契约后接入 |
| 健康检查 | 检查本地模型、MinerU、Docling、GPU 依赖 | 按运行模式检查 | `remote-only` 不应因本地模型缺失而失败 |
| 执行 Supervisor | 可启动 vLLM/GPU Worker | 远程模式仅启动 API 和 CPU Worker | 增加运行模式分支和资源上限 |

## 6. 目标架构

```text
调用方 / 网关
      |
      v
QASystem API（1 个进程）
      |
      +---- 轻量本地处理：校验、文本读取、切片编排、图合并、结果转换
      |
      +---- Redis 队列 ---- CPU Worker（默认 1 个）
                              |
                              +---- External LLM Client
                              +---- External Embedding Client
                              +---- External Document Parser Client
                              +---- Application Service Client
                                         |
                                         v
                           甲方应用服务 / 外部模型服务集群
```

目标架构遵循以下原则：

1. 业务算法依赖“能力接口”，不依赖具体供应商 SDK。
2. 外部 URL、固定路径、认证、超时、重试、响应校验和错误映射集中在 `external_services/`。
3. 算法代码只传递业务输入并消费规范化结果，不自行构造第三方 HTTP 路径。
4. 大文件优先使用临时文件、流式上传或分片，不在 API 和 Worker 间复制完整字节数组。
5. 同一能力默认只允许有限在途请求，避免外部服务变慢时任务在内存中堆积。

## 7. 目标模块边界

### 7.1 外部能力适配层

`external_services/application.py` 继续作为应用团队统一服务的端点注册和调用入口。若甲方模型服务不属于应用团队统一服务，应在 `external_services/` 下建立独立客户端模块，但仍须集中管理端点，禁止在 `algorithm/`、`backend/app/services/` 或 MCP 工具中硬编码路径。

建议形成以下稳定能力名：

| 能力名 | 用途 | 规范化返回 |
| --- | --- | --- |
| `models.chat.completions` | 知识抽取、图谱判断、Agent | 文本、结构化 JSON、usage、request_id |
| `models.embeddings.create` | 文本、切片、图谱向量化 | 与输入顺序一致的向量数组、维度、模型版本 |
| `documents.parse` | PDF/OFD/Office/图片解析 | Markdown、页信息、图片引用、解析器元数据 |
| `models.rerank` | 候选重排序 | 候选索引和分数 |
| `confidence.calculate` | 图谱关系置信度 | 对应关系的置信度和证据分量 |

尚未开放的能力注册为不可用。调用时应返回现有 `EXTERNAL_CAPABILITY_UNAVAILABLE` 语义，不影响服务启动。

### 7.2 算法层

算法层保留：

- 输入清洗与协议校验。
- Markdown/TXT 轻量解析。
- 文档切片规则和字符范围计算。
- Prompt、Ontology 和知识抽取结果校验。
- 图谱去重、合并与稳定序列化。
- 批处理调度、限流和部分失败汇总。

算法层移除或隔离：

- 顶层导入 `torch`、`sentence_transformers`、`transformers`、MinerU 和 Docling。
- `/models` 默认路径和模型目录存在性假设。
- CUDA 设备判断、显存租约和本地模型缓存。
- 供应商 URL 路径、认证头和私有响应字段。

### 7.3 执行层

`remote-only` 模式下执行层只启动：

- 1 个 API Worker。
- 1 个 CPU 队列 Worker。
- 必需的任务存储和事件组件。

不得启动：

- vLLM Supervisor 和 vLLM API Server。
- `gpu.vlm`、`gpu.embedding` 或其他 GPU Worker。
- GPU 租约、CUDA 探测和模型预热任务。

历史资源类可在公共接口中保持兼容，但远程模式下必须映射为受限的远程推理队列资源，不能因名称含 `gpu` 而等待不存在的 GPU。

## 8. 外部服务契约目标

### 8.1 通用要求

所有外部模型调用至少支持：

- 请求超时、连接超时和可配置重试。
- 全链路 `request_id` 或等价追踪标识。
- 明确区分 4xx 参数错误、401/403 鉴权错误、429 限流、5xx 服务错误和网络超时。
- 对响应体大小、JSON 格式、向量数量、向量维度和输入顺序进行校验。
- 幂等任务支持幂等键；非幂等请求默认不自动重试写操作。
- 日志记录能力名、耗时、状态码、重试次数和外部 request ID，不记录 Authorization、原始文件全文或敏感 Prompt。

### 8.2 Embedding 契约

优先兼容 OpenAI `/v1/embeddings` 语义，适配层对业务侧输出统一为：

```json
{
  "model": "embedding-model-name",
  "dimension": 1024,
  "vectors": [
    {"index": 0, "embedding": [0.1, 0.2]}
  ],
  "request_id": "external-request-id"
}
```

必须校验：

- 返回数量与输入数量一致。
- `index` 完整、唯一且可恢复为输入顺序。
- 同一响应中的向量维度一致。
- 维度与向量存储系统约定一致；模型或维度变更必须作为部署变更处理。

建议默认每批 16～32 条，另设总字符数或 Token 上限；不得把整份大型文档一次性发送给 Embedding 服务。

### 8.3 文档解析契约

文档解析服务至少接收：

- 文件流或对象存储引用。
- 文件名、MIME 类型和文件哈希。
- 页码范围。
- OCR、表格、公式和语言选项。
- 期望的返回格式。

规范化返回至少包含：

```json
{
  "markdown": "...",
  "pages": [{"page": 1, "text_length": 1200}],
  "parser": {
    "engine": "remote-vlm",
    "model": "model-name",
    "version": "model-version"
  },
  "request_id": "external-request-id"
}
```

大文档优先使用异步任务协议：提交文件后返回外部任务 ID，由 QASystem Worker 轮询或接收回调。轮询状态不得把外部 Authorization 写入任务 payload；鉴权信息仍通过 `auth_context_id` 在执行时恢复。

### 8.4 LLM 契约

现有 `KG_EXTRACT_LLM_*`、`GRAPH_LLM_*` 和 `AGENT_LLM_*` 可以继续支持不同服务地址和模型，但应共用统一 HTTP 客户端基础能力：

- 连接池、超时和代理配置。
- 429/5xx 的指数退避与抖动。
- JSON 输出校验与有限次数修复重试。
- 并发、QPS 和 QPM 限制。
- usage、模型版本和 request ID 记录。

知识抽取不得因外部 LLM 暂时不可用而回退到本地模型。

## 9. 鉴权与数据安全

外部调用遵循现有队列鉴权机制：

1. API 入口捕获白名单鉴权头并保存到独立上下文存储。
2. 队列记录、Stream envelope 和任务 payload 只保存 `auth_context_id`。
3. Worker 执行时按任务恢复鉴权上下文，完成外部调用后立即清理进程上下文。
4. 任务成功、取消或最终失败后删除鉴权上下文；重试期间按现有 TTL 保留。
5. 日志、SSE、错误详情、原始响应、`execution` 和结果对象不得包含 Authorization 或其可逆变体。

若模型服务使用系统级 API Key，应由部署环境以 Secret 注入，不能写入 `.env.example` 的真实值、Compose 文件、源码或构建产物。

上传文档可能包含敏感信息，接口评审必须确认：

- 数据是否离开甲方网络。
- 外部服务是否留存输入、Prompt、图片和解析结果。
- 日志脱敏和数据保留期限。
- HTTPS、证书校验和内网域名解析策略。

## 10. 内存控制设计

### 10.1 启动与导入

- 将模型相关依赖改为能力调用点懒导入。
- `remote-only` 启动路径不得导入 Torch、Sentence Transformers、Transformers、MinerU、Docling 或 vLLM。
- API 模块不得在 import 阶段实例化文档解析器或模型客户端。
- HTTP 客户端使用有界连接池，避免每任务重复创建大量连接和 TLS 状态。

### 10.2 文件处理

- 上传内容超过内存阈值后落盘到受控临时目录。
- API 到 Worker 优先传递临时文件引用或应用服务文件 ID，不复制完整文件字节。
- 外部上传使用流式读取；外部下载设置最大响应体。
- 临时文件在任务结束后清理，并设置异常任务兜底清理机制。
- 对单文件大小、页数、解压后大小和解析结果大小设置明确上限。

### 10.3 并发与背压

- 文档解析、Embedding、知识抽取分别使用独立信号量。
- 外部服务超时或 429 时降低消费速度，不在内存中无限积压待重试对象。
- 大批量 Embedding 使用小批次串行或有限并行，批次完成后立即释放中间结果。
- SSE 只发送必要的进度与结果引用，避免重复发送大型中间产物。

## 11. 配置目标

建议新增以下部署配置；最终名称可在实现评审时确认：

```env
# 运行模式
ALGORITHM_RUNTIME_PROFILE=remote-only
API_WORKERS=1
EXECUTION_CPU_WORKERS=1

# 应用团队统一服务
APPLICATION_SERVICE_URL=http://application-server
FILE_SERVICE_URL=

# 外部 Embedding
EMBEDDING_SERVICE_URL=http://model-gateway
EMBEDDING_SERVICE_API_KEY=
EMBEDDING_MODEL=embedding-model-name
EMBEDDING_TIMEOUT_SECONDS=60
EMBEDDING_MAX_BATCH_SIZE=32
EMBEDDING_MAX_CONCURRENCY=1

# 外部文档解析
DOCUMENT_PARSE_SERVICE_URL=http://document-parser
DOCUMENT_PARSE_SERVICE_API_KEY=
DOCUMENT_PARSE_TIMEOUT_SECONDS=900
DOCUMENT_PARSE_MAX_CONCURRENCY=1
DOCUMENT_PARSE_MAX_FILE_MB=100

# 远程模式禁用本地模型进程
VLLM_SUPERVISOR_ENABLED=false
LOCAL_MODEL_LOADING_ENABLED=false
```

配置规则：

- 可调默认值只放在根 `.env.example`，Compose 不重复维护同一批默认值。
- Compose 只描述服务拓扑、只读源码挂载、命令、端口和健康检查。
- `remote-only` 不要求 `HOST_MODELS_DIR`，也不挂载 `/models`。
- `full-runtime` 继续保留 `GPU_DEVICE_IDS`，生产 8 卡 A40 环境常用值仍为 `4,5,6,7`。
- 未配置的必需远程能力应在启动检查或调用时给出明确错误，不能自动下载模型。

## 12. 健康检查与可观测性

### 12.1 健康检查

`remote-only` 完整健康检查应关注：

- Redis、数据库和应用团队服务连通性。
- 已配置 LLM、Embedding 和文档解析服务的连通性。
- 必需系统转换工具是否存在；若 Office/OFD 转换也由外部解析服务负责，则本地 LibreOffice、Java 和 OFD 转换器可标记为跳过。
- 运行模式下不应检查 `/models`、CUDA、MinerU、Docling 或 vLLM。

外部置信度等非必需能力未配置时显示 `skipped/unavailable`，不应导致基础服务健康检查失败。

### 12.2 指标与日志

至少记录以下指标：

- 各外部能力请求数、成功率、错误类型和 P50/P95/P99 延迟。
- 429 次数、重试次数、超时次数和熔断状态。
- 队列长度、排队时间、执行时间和取消次数。
- Embedding 批大小、输入数量、向量维度和模型版本。
- 文档大小、页数、解析耗时和外部任务状态。
- 进程 RSS、容器内存使用量和 OOM 次数。

## 13. 错误处理目标

| 场景 | 对外行为 |
| --- | --- |
| 必需远程 URL 未配置 | 返回配置错误或能力不可用，不尝试本地模型 |
| 外部服务连接失败 | 返回外部请求失败，可按策略重试 |
| 外部服务超时 | 映射现有超时错误，保留可重试标识 |
| 外部服务 429 | 映射限流错误并执行有界退避 |
| Embedding 数量不一致 | 返回 `VECTOR_COUNT_MISMATCH` |
| Embedding 维度变化 | 明确失败并提示模型/索引配置不一致 |
| 文档解析结果为空 | 返回现有文档内容为空错误 |
| 外部返回非法 JSON | 返回响应无效错误，不把原始敏感内容直接透出 |
| 未开放置信度等能力 | 返回 `EXTERNAL_CAPABILITY_UNAVAILABLE` |

## 14. 实施阶段

### 阶段一：运行模式和依赖隔离

- 增加 `remote-only` 运行模式。
- 将模型依赖改为懒导入。
- 远程模式禁用 GPU Worker、vLLM Supervisor、本地模型健康检查和 `/models` 挂载要求。
- 建立 3 GiB 环境下的内存基线测试。

### 阶段二：Embedding 远程化

- 新增统一 Embedding 客户端和响应校验。
- 替换文本向量、图谱向量和语义切片中的本地模型调用。
- 加入批大小、并发、超时、重试和维度保护。
- 保留 `full-runtime` 的本地后端兼容开关。

### 阶段三：文档解析远程化

- 新增同步/异步文档解析适配。
- 将 MinerU pipeline、VLM 和 Docling 能力映射到远程解析服务。
- 改造文件流转，避免 API、队列和 Worker 多份复制。
- 明确 OFD/Office 转换由本地轻量工具还是远程解析服务负责。

### 阶段四：部署收敛与联调

- 提供 CPU-only Compose 拓扑或受控 Profile。
- 更新 `.env.example`、健康检查、部署文档和接口协议。
- 完成外部服务故障注入、资源压测和长时间稳定性测试。
- 形成甲方部署参数表、回滚步骤和验收报告。

## 15. 代码改造范围

预期重点涉及：

- `external_services/application.py` 及新增的外部推理客户端。
- `algorithm/embedding_runtime.py`。
- `algorithm/NL_to_cypher/vector.py` 和语义切片调用链。
- `backend/app/services/algorithm_capabilities.py`。
- `algorithm/preproc/preproc_src/main_processor.py`。
- `algorithm/preproc/preproc_src/processors/ocr_processor.py`。
- `backend/app/execution/supervisor.py`、Worker 和资源类映射。
- `backend/app/health.py`。
- 根 `.env.example`、Compose、依赖清单和部署文档。
- Embedding、文档解析、鉴权、健康检查、资源模式相关测试。

是否从 `qasystem-env-v1` 移除 Torch、MinerU、Docling 等环境依赖，需要单独决策：如果同一个环境镜像仍需支持 `full-runtime`，可以先保留依赖但保证远程模式不导入；只有在确定维护独立轻量环境镜像且环境依赖合同发生变化时，才重建并重新交付环境镜像。

## 16. 验收标准

### 16.1 功能验收

- 无 GPU、无 `/models` 挂载时服务可正常启动。
- LLM、Embedding 和文档解析均能通过外部 URL 完成端到端业务流程。
- Markdown/TXT、PDF、Office、图片至少各有一条成功用例。
- 外部服务不可用、超时、429、非法响应时返回稳定错误码。
- 未开放能力被调用时显式失败，不访问 Neo4j、Elasticsearch 或本地模型。
- 现有 `full-runtime` 关键回归用例继续通过。

### 16.2 安全验收

- 任务 payload、Redis Stream、SSE、日志、`execution` 和结果中均不存在 Authorization。
- 模型服务密钥未进入源码、镜像、Compose 和示例配置真实值。
- HTTPS 证书校验默认开启，关闭时需要显式配置并产生告警。

### 16.3 资源验收

- 在容器内存限制 2.75 GiB 下连续运行不少于 24 小时，无 OOM 和进程异常重启。
- 空闲、普通任务和约定上限文档的 RSS 满足第 3.2 节指标。
- 外部服务连续超时或限流时，队列和内存保持有界。
- 并发 1 的标准业务流程成功率和延迟满足甲方约定。

### 16.4 工程验收

- 所有新增外部路径均集中注册，无算法模块硬编码第三方路径。
- `.env.example` 包含全部部署变量及说明，Compose 不重复可调默认值。
- 单元测试、契约测试、集成测试和 CPU-only 启动检查通过。
- 构建和打包脚本在长操作中继续输出带时间戳的阶段进度。

CPU-only 部署使用根目录 `docker-compose.remote-only.yml` 和 `qasystem-env-v1-remote-only`。该 Compose 不声明 `gpus`，不挂载 `/models`，并为 Backend、Agent 和 Redis 设置可调内存上限。精简环境镜像不包含 CUDA、Torch、vLLM、MinerU、Docling 或 LibreOffice；应用源码仍由 `QASYSTEM_SOURCE_DIR` 只读挂载。

## 17. 待甲方确认事项

开始实现前，应优先确认以下接口和部署条件：

1. Embedding 服务是否兼容 OpenAI 协议，模型名、维度、最大批量和限流规则是什么。
2. 文档解析服务支持的文件类型、同步/异步模式、单文件大小、页数限制和返回结构是什么。
3. OCR、公式、表格、图片提取是否由同一个文档解析接口完成。
4. LLM、Embedding 和文档解析是否共用一个网关及认证方式。
5. 外部服务是否允许透传用户 Authorization，还是使用独立系统级凭证。
6. 甲方网络是否要求代理、双向 TLS、私有 CA 或固定出口 IP。
7. 敏感文档是否允许发送到外部服务，以及外部服务的数据留存策略。
8. 云服务器实际可用内存、容器内存限制、CPU 核数、磁盘临时空间和单文件上限。

## 18. 完成定义

当以下条件同时满足时，本次重构可视为完成：

- 甲方无 GPU、低于 3 GiB 内存的目标服务器完成部署并通过资源验收。
- 所有需要模型的生产能力均走已确认的外部 URL，运行期间没有本地模型加载行为。
- 公共业务接口保持兼容，外部服务差异被限制在适配层。
- 鉴权、日志、队列和失败语义满足现有安全边界。
- 现有 GPU 部署模式完成关键回归，没有因远程模式引入功能退化。
