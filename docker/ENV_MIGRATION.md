# 新架构完整镜像部署迁移指引

本文适用于从 `a93e8c4^` 所代表的重构前版本迁移到统一并发执行架构，也可用于在一台全新 GPU 服务器上直接部署。重构前没有独立 vLLM 服务，也没有显式 worker/vLLM services；Backend 入口进程直接运行 MinerU 和其他算法。正式交付只有一种拓扑：`backend`、`agent`、`redis` 三个容器。`backend` 使用完整镜像，入口命令为 `python -m app.execution.supervisor`，不挂载源码。

模型仍由宿主机只读挂载到 `/models`，业务数据由 Docker volume 持久化。目标 8 卡 A40 服务器默认设置 `GPU_DEVICE_IDS=4,5,6,7`，只允许使用后四张卡；supervisor 仅在选中的卡内管理 API 进程、CPU worker、每卡一个常驻 vLLM engine、VLM dispatch worker 和共享 Embedding/Confidence worker。Compose 不枚举这些子进程。

## 1. 部署前提

- Linux x86_64、Docker Engine 和 Docker Compose v2。
- NVIDIA 驱动与 NVIDIA Container Toolkit 已安装，`docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu22.04 nvidia-smi` 可看到目标 GPU。
- 目标目录有足够空间保存完整 Backend 镜像及模型；不要把大型镜像归档和模型放在空间不足的 home 分区。
- Redis 必须可用。正式部署的计算通过异步队列和 Worker 执行，Redis 不可用时接口返回 `503`，不会退回 API 进程内执行；这不改变业务 HTTP API 默认同步返回结果的兼容行为。
- MySQL、Neo4j、Elasticsearch、文件服务和外部 LLM 地址可从容器访问。

生产目标为 4 张 A40 时，建议先使用默认配置：每卡一个 vLLM engine，`VLLM_GPU_MEMORY_UTILIZATION=0.82`，每卡一个共享辅助 worker。稳定运行后再根据实测显存余量调整，不要一开始把 vLLM 配到接近 100%。

## 2. 交付物

交付目录至少包含：

```text
deploy/
├── docker-compose.yml
├── .env.example
├── ENV_MIGRATION.md
├── README.md
├── models/
│   ├── bge-small-zh-v1.5/
│   ├── docling-models/
│   └── mineru/
│       ├── pipeline/
│       └── vlm/
├── qasystem-backend-latest.tar
└── redis-7-alpine.tar
```

完整镜像已包含应用源码、MinerU、vLLM、调度器和 Python/CUDA 运行依赖。目标机不需要 Git 仓库、`runtime/` 源码目录或额外 vLLM 镜像。

## 3. 环境变量迁移

保留旧 `.env` 中的数据库、Neo4j、Elasticsearch、文件服务、LLM、Agent 和密钥配置。不要用 `.env.example` 覆盖真实配置；先备份，再按下表合并。

### 基线变量的变化

| 旧变量/做法 | 新架构处理 | 原因 |
| --- | --- | --- |
| `GPU_DEVICE_IDS=4,5,6,7` | 保留，但不再由 Compose 映射成容器级 `CUDA_VISIBLE_DEVICES` | 旧版让整个 Backend 进程同时看到四卡；新版将它作为 supervisor 的资源边界，再为各子进程绑定单卡 |
| 数据库、Neo4j、Elasticsearch、文件服务、LLM、Agent 和密钥变量 | 原值迁移 | 外部依赖协议不因执行架构改变 |
| `MINERU_MODEL_*`、`VECTOR_MODEL_PATH`、`CONFIDENCE_SBERT_MODEL_PATH` | 保留并校验容器内路径 | 模型仍由宿主机只读挂载到 `/models` |

### 不要带入中间实验配置

`a93e8c4^` 基线本身没有独立 vLLM/worker services，也没有 `VLLM_IMAGE`、逐卡 vLLM URL 或 `source-mounted/` 部署。若测试环境曾使用这些中间实验配置，不要合并到新 `.env` 或 Compose：vLLM、Worker 和逐进程 `CUDA_VISIBLE_DEVICES` 均由 Backend supervisor 管理。MCP 继续由 Backend 的 `/mcp` 提供，Agent API 由 Backend 的 `/agent` 反向代理，不需要额外的 `MCP_PORT`、`AGENT_BIND` 或 `AGENT_HOST_PORT`。

### 必须新增或确认

| 变量 | 建议值 | 语义 |
| --- | --- | --- |
| `EXECUTION_MODE` | `async` | 业务计算进入统一执行队列；不表示 HTTP 默认立即返回 `202` |
| `EXECUTION_REDIS_URL` | `redis://redis:6379/1` | 执行状态、队列、事件和租约存储 |
| `MCP_EXECUTION_MODE` | `sync` | 生产默认；任务进入统一队列并等待完成，返回原 MCP 工具结果 |
| `HOST_MODELS_DIR` | `./models` | 宿主机模型目录，只读挂载到 `/models` |
| `VLLM_MODEL_PATH` | `/models/mineru/vlm` | supervisor 启动的 MinerU VLM 模型 |
| `MINERU_MODEL_SOURCE` | `local` | 禁止运行时下载模型 |
| `MINERU_MODEL_PIPELINE_PATH` | `/models/mineru/pipeline` | MinerU pipeline 模型路径 |
| `MINERU_MODEL_VLM_PATH` | `/models/mineru/vlm` | MinerU VLM 模型路径 |
| `VECTOR_MODEL_PATH` | `/models/bge-small-zh-v1.5` | 文本向量模型 |
| `CONFIDENCE_SBERT_MODEL_PATH` | `/models/bge-small-zh-v1.5` | 置信度模型，与向量化共享 encoder |
| `NL2CYPHER_EMBEDDING_MODEL_PATH` | `/models/bge-small-zh-v1.5` | NL2Cypher 向量模型 |

不要设置 `DOCLING_ARTIFACTS_PATH`；Docling 固定读取 `/models/docling-models`。

### 可选调优

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GPU_DEVICE_IDS` | `4,5,6,7` | supervisor 可使用的物理卡号；目标 8 卡 A40 仅选后四卡 |
| `API_WORKERS` | `2` | Uvicorn API 进程数 |
| `EXECUTION_CPU_WORKERS` | `auto` | CPU worker 数；4 卡 32 核机器自动取 8 |
| `EXECUTION_VLM_WORKERS_PER_GPU` | `auto` | 每卡 VLM dispatch worker 数；4 卡 32 核机器自动取 2 |
| `EXECUTION_AUX_WORKERS_PER_GPU` | `1` | 每卡共享 Embedding/Confidence worker 数 |
| `VLLM_GPU_MEMORY_UTILIZATION` | `0.82` | 每个 vLLM engine 可使用的显存比例 |
| `VLLM_MAX_NUM_SEQS` | `32` | 每个 vLLM engine 的并发序列上限 |
| `VLLM_WARMUP_ENABLED` | `true` | readiness 后执行多模态小请求预热 |
| `VLLM_WARMUP_TIMEOUT_SECONDS` | `300` | 单个 engine 预热超时 |
| `EXECUTION_VLM_SHARD_MIN_PAGES` | `8` | `auto` 分页的最小 PDF 页数 |
| `EXECUTION_VLM_SHARD_COUNT` | `4` | 默认目标页段数，仍受可用 worker 和请求上限约束 |
| `EXECUTION_GPU_MEMORY_REQUEST_MB_GPU_VLM` | `2048` | 单个 VLM 原子任务的调度显存预算，不是模型预分配量 |
| `EXECUTION_GPU_MEMORY_REQUEST_MB_GPU_EMBEDDING` | `2048` | Embedding 调度预算 |
| `EXECUTION_GPU_MEMORY_REQUEST_MB_GPU_CONFIDENCE` | `2048` | Confidence 调度预算 |

`EXECUTION_MODE=inline` 只用于单元测试或本地算法断点调试，不是部署兼容模式。

## 4. 全新目标机部署

在交付目录执行：

```bash
cd /path/to/deploy
sha256sum -c SHA256SUMS 2>/dev/null || true
docker load -i qasystem-backend-latest.tar
docker load -i redis-7-alpine.tar
cp .env.example .env
```

编辑 `.env`，至少填写外部服务地址、账号、密钥和 `HOST_MODELS_DIR=./models`。然后检查 GPU、模型挂载和 Compose：

```bash
docker run --rm --gpus all qasystem-backend:latest nvidia-smi
docker compose config --quiet
docker compose up -d
docker compose ps
docker compose logs -f backend
```

日志中应先出现每张可见 GPU 的 vLLM 启动和 warmup 完成信息，随后出现 runtime ready。首次启动需要加载模型，Backend 健康检查在全部进程可用前可能保持 `starting`。

验证：

```bash
curl -fsS http://127.0.0.1:8006/health
curl -fsS http://127.0.0.1:8006/agent/health
curl -fsS http://127.0.0.1:8006/api/v1/resources/gpus
```

## 5. 从旧部署迁移

先保存旧配置和持久数据，再切换交付目录：

```bash
cp .env .env.pre-supervisor.bak
docker compose ps
docker load -i qasystem-backend-latest.tar
docker load -i redis-7-alpine.tar
```

将旧 `.env` 按第 3 节合并到新 `.env`，使用本次交付的 `docker-compose.yml`。重构前 Compose 没有 worker/vLLM services；迁移的核心是将 Backend 入口从 Uvicorn 直接启动替换为 supervisor，并补充统一执行层配置，不是兼容或清理某个中间实验拓扑。确认配置后执行：

```bash
docker compose config --quiet
docker compose up -d --force-recreate backend agent redis
docker compose ps
docker compose logs --tail=300 backend
```

回滚时恢复 `.env.pre-supervisor.bak` 和重构前 Compose，并重新加载旧镜像；Redis 与业务数据库数据不应在迁移中删除。

## 6. API 协议漂移

业务 HTTP 路径、请求主体和默认同步返回保持兼容，所有变化均为增量能力：

| 项目 | 旧架构 | 新架构 | 兼容性 |
| --- | --- | --- | --- |
| 业务路径 | `/api/v1/...` 及 deprecated aliases | 原路径保留 | 路径兼容 |
| 业务 API 默认响应 | 同步返回原 HTTP 状态和业务 `data` | 保持不变，顶层增加 `execution` | 增量兼容 |
| 显式异步 | 无 | `Prefer: respond-async` 立即返回 `202` 和任务句柄 | 新增可选能力 |
| 显式有界等待 | 无 | `Prefer: wait=N`，`N` 最大 60 秒 | 新增；时限内完成返回原结果，否则返回 `202` |
| 通用 Jobs API | 无 | `POST /api/v1/jobs` 默认返回 `202` | 新增接口 |
| 响应 envelope | `code/info/data/error` | 增加顶层 `execution` | 加法兼容；严格 schema 客户端需更新 |
| 强制解析引擎 | `engine` 可能仍参与自动 fallback | `engine` 加同层 `force=true` 才严格强制 | 新增可选字段 |
| PDF 页段 | 整份 PDF 单任务 | `page_sharding/page_shard_size/max_page_shards` | 新增可选字段，默认 `auto` |
| 运行日志 | 容器日志为主 | 容器结构化日志和按任务 SSE | 新增 |
| MCP 重工具默认行为 | 入口进程直接执行并返回业务结果 | `MCP_EXECUTION_MODE=sync`，队列执行后返回原工具结果 | 返回协议兼容 |
| MCP 显式异步 | 无 | `MCP_EXECUTION_MODE=async` 时返回任务句柄 | 新增可选能力 |

显式异步请求或 Jobs API 收到 `202` 后：

```bash
JOB_ID='<响应 data.job_id>'
curl -fsS "http://127.0.0.1:8006/api/v1/jobs/${JOB_ID}"
curl -N "http://127.0.0.1:8006/api/v1/jobs/${JOB_ID}/events?min_level=INFO"
curl -fsS "http://127.0.0.1:8006/api/v1/jobs/${JOB_ID}/executions"
curl -fsS "http://127.0.0.1:8006/api/v1/jobs/${JOB_ID}/result"
```

收到 SSE `job.completed` 后调用 `result_url`。`result` 仅在任务 `succeeded` 后返回包含最终业务 `data` 和顶层 `execution` 的完整 envelope，否则返回 `409`。每个统一 JSON 响应的顶层 `execution` 提供任务状态、摘要、事件 URL、执行明细 URL 和 `result_url`；页段的 worker、进程实例、GPU、页范围、尝试次数、开始/结束时间可从执行明细和 SSE 获取。

文档解析新增配置示例：

```json
{
  "engine": "vlm",
  "force": true,
  "page_sharding": "auto",
  "page_shard_size": 0,
  "max_page_shards": 4,
  "mineru_options": {
    "language": "ch",
    "ocr_mode": "auto",
    "table_enable": true,
    "formula_enable": true
  }
}
```

- `force=false`：`engine` 仍可作为首选或 fallback 候选。
- `force=true`：必须显式指定兼容的非 `auto` 引擎，跳过自动选择和 fallback。
- `page_sharding=auto`：仅 VLM PDF 且达到最小页数时分页。
- `page_sharding=on/off`：强制启用或关闭分页；非 VLM 或非 PDF 不分页。
- `page_shard_size=0`：按目标 shard 数均衡分页；大于 0 时作为每段目标页数。
- `max_page_shards`：单请求页段上限，范围 1 到 32。

旧客户端不发送 `Prefer` 头即可继续同步获取原业务结果。需要释放 HTTP 连接时发送 `Prefer: respond-async`；只愿等待固定时间时发送 `Prefer: wait=60`。新接入方也可直接使用 `POST /api/v1/jobs`。

## 7. 验收与排障

检查 supervisor 自动派生的子进程，而不是在 Compose 中查找 worker service：

```bash
docker compose exec backend nvidia-smi
docker compose exec backend sh -lc 'ps -ef | grep -E "uvicorn|vllm|app.execution.worker" | grep -v grep'
curl -fsS http://127.0.0.1:8006/api/v1/resources/gpus
```

常见问题：

- API 返回 `503`：检查 Redis 健康状态和 `EXECUTION_REDIS_URL`。
- Backend 长时间 `starting`：查看 vLLM 模型路径、显存和 warmup 日志。
- GPU 选择不符：目标 8 卡 A40 应设置 `GPU_DEVICE_IDS=4,5,6,7`，修改后重建 Backend 容器。
- 显存不足：先降低 `VLLM_GPU_MEMORY_UTILIZATION` 或 `VLLM_MAX_NUM_SEQS`，不要增加辅助 worker。
- GPU 利用率低：确认 PDF 使用 `engine=vlm,force=true`，分页没有关闭，并查看 `execution`/SSE 是否发生排队、失败或单卡倾斜。
