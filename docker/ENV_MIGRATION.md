# 双镜像部署迁移与环境变量指引

本文描述当前 `qasystem-base-v1` + `qasystem-runtime-v1` 部署方式。目标机运行的仍是完整 runtime 容器，双镜像是构建和交付分层，不是启动两个应用容器。

## 迁移原则

- 已有 `qasystem-base-v1` 时，代码更新只传 `runtime-context.tar`，不再传 base。
- 已经上传旧完整镜像但尚无 base 时，运行 `migrate-legacy-image-to-base.sh` 一次即可复用，不需要重新上传大镜像。
- 不导出 `qasystem-runtime-v1`；`docker save` 会重复包含 base 层。
- `.env` 是运行配置唯一来源，升级部署文件时保留现有 `.env`，按模板增量合并。
- Compose 不再逐项覆盖调度参数。修改 `.env` 后重建容器即可生效。

## 交付方操作

普通代码发布：

```powershell
.\build-image.ps1
.\package-deploy.ps1
```

首次离线交付或 base 契约升级：

```powershell
.\build-image.ps1 -BuildBase
.\package-deploy.ps1 -IncludeBaseImage -IncludeRedisImage
```

`-BuildBase` 和 `-IncludeBaseImage` 都是显式按需操作，不会在普通打包中自动发生。

## 目标机首次部署

```bash
sha256sum -c SHA256SUMS
docker load -i qasystem-base-v1.tar       # 仅交付包包含时执行
docker load -i redis-7-alpine.tar         # 仅交付包包含时执行
cp .env.example .env
# 填写真实外部服务、密钥、模型路径和 GPU 范围
chmod +x apply-runtime.sh
./apply-runtime.sh
```

若 base 来自已上传旧镜像：

```bash
QASYSTEM_BASE_TMPDIR=/mnt/data3/qasystem-base-tmp \
  ./migrate-legacy-image-to-base.sh qasystem-backend:latest qasystem-base-v1
./apply-runtime.sh
```

旧完整镜像名称只在这一迁移步骤使用。确认新 runtime 健康后，再由运维按磁盘策略清理旧镜像和迁移临时文件。

## 日常升级

替换交付目录中的 `runtime-context.tar`、`SHA256SUMS`、`交付清单.json` 及实际变更的部署脚本，保留 `.env` 和 `models/`，然后执行：

```bash
./apply-runtime.sh
```

脚本保留旧 runtime rollback 标签，并在新服务健康检查失败时自动恢复。只验证镜像而不切服务可执行 `./apply-runtime.sh --build-only`。

## 必须保留或确认的变量

| 变量 | 建议值 | 说明 |
| --- | --- | --- |
| `BACKEND_IMAGE` | `qasystem-runtime-v1` | Backend 和 Agent 使用同一 runtime |
| `GPU_DEVICE_IDS` | `4,5,6,7` | 8 卡 A40 目标机只允许使用后四卡 |
| `HOST_MODELS_DIR` | `./models` | 交付目录相对路径，挂载到容器 `/models` |
| `EXECUTION_MODE` | `async` | 统一执行队列；不改变业务 API 默认同步返回 |
| `EXECUTION_REDIS_URL` | `redis://redis:6379/1` | 队列、状态、事件和租约 |
| `TASK_STORE_REDIS_URL` | `redis://redis:6379/0` | Agent 任务存储 |
| `MCP_EXECUTION_MODE` | `sync` | MCP 进入队列后等待原工具结果 |
| `VLLM_MODEL_PATH` | `/models/mineru/vlm` | 常驻 vLLM 模型 |
| `MINERU_MODEL_SOURCE` | `local` | 部署时禁止在线下载 |

数据库、Neo4j、Elasticsearch、文件服务、LLM、Agent 密钥以及 `MINERU_MODEL_*`、`VECTOR_MODEL_PATH`、`CONFIDENCE_SBERT_MODEL_PATH` 继续按原值迁移。

## GPU 与分页调优

首轮 A40 参数保持模板默认值：`VLLM_GPU_MEMORY_UTILIZATION=0.82`、`VLLM_MAX_NUM_SEQS=32`、`EXECUTION_VLM_SHARD_COUNT=auto`。supervisor 只在 `GPU_DEVICE_IDS` 指定范围内探测和启动进程；Worker 数、显存预算、Lease 和分片均从 `.env` 读取，无需修改 Compose 或镜像。

`EXECUTION_VLM_SHARD_COUNT=auto` 会按存活 VLM worker 生成约 1.5 轮页段，并受 PDF 页数和请求 `max_page_shards` 限制。显存快照和 Lease 控制实际在途并发，页段数本身不是显存配额。

## 验收

```bash
docker compose config --quiet
docker compose ps
docker compose logs --tail=300 backend
curl -fsS http://127.0.0.1:8006/health
curl -fsS http://127.0.0.1:8006/agent/health
curl -fsS http://127.0.0.1:8006/api/v1/resources/gpus
```

日志应显示所选 GPU 的 vLLM readiness/warmup 和最终 runtime ready。Backend 长时间处于 starting 时，优先检查模型挂载、GPU 范围、显存和 warmup 日志。
