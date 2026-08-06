# 新架构完整镜像部署迁移指引

本文适用于从旧版单进程 Backend 迁移到统一并发执行架构，也可用于在一台全新 GPU 服务器上直接部署。正式交付只有一种拓扑：`backend`、`agent`、`redis` 三个容器。`backend` 使用完整镜像，入口命令为 `python -m app.execution.supervisor`，不挂载源码，也不复用旧版 worker 或 vLLM 容器。

模型仍由宿主机只读挂载到 `/models`，业务数据由 Docker volume 持久化。一个 Backend 容器获取所有可见 GPU；supervisor 自动探测 GPU，并管理 API 进程、CPU worker、每卡一个常驻 vLLM engine、VLM dispatch worker 和共享 Embedding/Confidence worker。Compose 不枚举这些子进程。

## 1. 部署前提

- Linux x86_64、Docker Engine 和 Docker Compose v2。
- NVIDIA 驱动与 NVIDIA Container Toolkit 已安装，`docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu22.04 nvidia-smi` 可看到目标 GPU。
- 目标目录有足够空间保存完整 Backend 镜像及模型；不要把大型镜像归档和模型放在空间不足的 home 分区。
- Redis 必须可用。正式部署固定使用异步执行，Redis 不可用时接口返回 `503`，不会退回 API 进程内执行。
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

### 删除或停止使用

| 旧变量/做法 | 新架构处理 | 原因 |
| --- | --- | --- |
| `CUDA_VISIBLE_DEVICES` | 不在 Compose 中设置 | supervisor 为每个子进程自动绑定 GPU |
| `GPU_DEVICE_IDS=4,5,6,7` 作为默认值 | 默认留空；仅需限卡时填写 | 空值表示使用容器内所有可见 GPU |
| `MCP_PORT` | 删除 | MCP 统一由 Backend 的 `/mcp` 提供 |
| `AGENT_BIND`、`AGENT_HOST_PORT` | 删除 | Agent API 统一由 Backend 的 `/agent` 反向代理 |
| 手工配置 `VLLM_GPU_DEVICE_*`、vLLM URL 列表 | 删除 | supervisor 自动按卡生成端口和进程环境 |
| 源码挂载、`source-mounted/` | 删除 | 正式交付只运行完整镜像 |
| 独立 worker/vLLM Compose services | 删除 | 子进程全部由 Backend supervisor 管理 |
| `VLLM_IMAGE` | 删除 | vLLM 已包含在完整 Backend 镜像中 |

### 必须新增或确认

| 变量 | 建议值 | 语义 |
| --- | --- | --- |
| `EXECUTION_MODE` | `async` | HTTP 重任务进入统一执行队列 |
| `EXECUTION_REDIS_URL` | `redis://redis:6379/1` | 执行状态、队列、事件和租约存储 |
| `MCP_EXECUTION_MODE` | `async` | MCP 重工具返回持久任务句柄 |
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
| `GPU_DEVICE_IDS` | 空 | 限制 supervisor 使用的物理卡号，如 `0,1,2,3` |
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

将旧 `.env` 按第 3 节合并到新 `.env`，使用本次交付的 `docker-compose.yml`，不要保留旧 Compose 中的 worker、vLLM 或源码挂载 service。确认配置后执行：

```bash
docker compose config --quiet
docker compose up -d --force-recreate backend agent redis
docker compose ps
docker compose logs --tail=300 backend
```

不要同时运行旧 worker/vLLM 容器和新 supervisor，它们会重复加载模型并争抢 GPU。回滚时恢复 `.env.pre-supervisor.bak` 和旧 Compose，并重新加载旧镜像；Redis 与业务数据库数据不应在迁移中删除。

## 6. API 协议漂移

业务 HTTP 路径和请求主体保持兼容，但默认执行方式发生了有意变化：

| 项目 | 旧架构 | 新架构 | 兼容性 |
| --- | --- | --- | --- |
| 业务路径 | `/api/v1/...` 及 deprecated aliases | 原路径保留 | 路径兼容 |
| 重任务响应 | 同步等待，通常 `200` | 默认 `202`，`data={job_id,status}` | 调用流程有漂移 |
| 有界同步等待 | 无 | `Prefer: wait=N`，`N` 最大 60 秒 | 新增；超时仍返回 `202` |
| 响应 envelope | `code/info/data/error` | 增加顶层 `execution` | 加法兼容；严格 schema 客户端需更新 |
| 强制解析引擎 | `engine` 可能仍参与自动 fallback | `engine` 加同层 `force=true` 才严格强制 | 新增可选字段 |
| PDF 页段 | 整份 PDF 单任务 | `page_sharding/page_shard_size/max_page_shards` | 新增可选字段，默认 `auto` |
| 运行日志 | 容器日志为主 | 容器结构化日志和按任务 SSE | 新增 |
| MCP 重工具 | 直接返回业务结果 | `MCP_EXECUTION_MODE=async` 时返回任务句柄 | 调用流程有漂移 |

异步请求收到 `202` 后：

```bash
JOB_ID='<响应 data.job_id>'
curl -fsS "http://127.0.0.1:8006/api/v1/jobs/${JOB_ID}"
curl -N "http://127.0.0.1:8006/api/v1/jobs/${JOB_ID}/events?min_level=INFO"
curl -fsS "http://127.0.0.1:8006/api/v1/jobs/${JOB_ID}/executions"
curl -fsS "http://127.0.0.1:8006/api/v1/jobs/${JOB_ID}/result"
```

`result` 仅在任务 `succeeded` 后返回业务结果，否则返回 `409`。每个统一 JSON 响应的顶层 `execution` 提供任务状态、摘要、事件 URL 和执行明细 URL；页段的 worker、进程实例、GPU、页范围、尝试次数、开始/结束时间可从执行明细和 SSE 获取。

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

旧客户端若必须临时保持同步形态，可发送 `Prefer: wait=60`，但长文档和并发请求必须按 `202 + job_id` 接入，不能依赖同步等待完成。

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
- 只想使用部分 GPU：设置 `GPU_DEVICE_IDS` 后重建 Backend 容器。
- 显存不足：先降低 `VLLM_GPU_MEMORY_UTILIZATION` 或 `VLLM_MAX_NUM_SEQS`，不要增加辅助 worker。
- GPU 利用率低：确认 PDF 使用 `engine=vlm,force=true`，分页没有关闭，并查看 `execution`/SSE 是否发生排队、失败或单卡倾斜。
