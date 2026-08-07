# 环境镜像与外置源码迁移指引

当前部署使用 `qasystem-env-v1` 环境镜像和宿主机外置源码。旧混合镜像不再转换或复用，直接加载新交付的干净环境镜像。

## 目录

```text
qasystem-deploy/
├── docker-compose.yml
├── .env
├── runtime/
│   ├── backend/
│   ├── algorithm/
│   ├── mcp_services/
│   ├── agent/
│   └── docker/
└── models/
```

Compose 分别把四个源码目录挂载到容器 `/app` 下。源码挂载为只读；Backend 数据和 Agent 输出继续写入 Docker volume，不写回源码目录。

## 首次切换

```bash
docker load -i qasystem-env-v1.tar
cp .env .env.before-env-image
# 按新 .env.example 增量合并，不要覆盖真实密钥
docker compose config --quiet
docker compose up -d --force-recreate backend agent redis
```

必须确认：

| 变量 | 值/说明 |
| --- | --- |
| `QASYSTEM_ENV_IMAGE` | `qasystem-env-v1` |
| `QASYSTEM_SOURCE_DIR` | 交付目录使用 `./runtime` |
| `HOST_MODELS_DIR` | 交付目录使用 `./models` |
| `GPU_DEVICE_IDS` | 8 卡 A40 目标机使用 `4,5,6,7` |
| `EXECUTION_MODE` | `async` |
| `EXECUTION_REDIS_URL` | `redis://redis:6379/1` |
| `TASK_STORE_REDIS_URL` | `redis://redis:6379/0` |
| `MCP_EXECUTION_MODE` | `sync` |

数据库、Neo4j、Elasticsearch、文件服务、LLM 和模型路径变量保留实际部署值。

## 代码更新

环境依赖未变化时只发布新的完整 `runtime/`：

```bash
sha256sum -c SHA256SUMS
docker compose up -d --force-recreate backend agent
```

为了保证切换一致性，不要把新旧源码混在同一目录逐文件更新。保留上一份 runtime 目录即可快速回滚。

## 环境更新

依赖变化时重新加载新的 `qasystem-env-v1.tar`，再重建容器。镜像不包含模型和源码，因此同一个环境镜像可配合不同代码发布使用；代码与依赖的兼容性由交付前挂载源码的完整校验保证。

## 验收

```bash
docker compose config --quiet
docker compose ps
docker compose logs --tail=300 backend
curl -fsS http://127.0.0.1:8006/health
curl -fsS http://127.0.0.1:8006/agent/health
curl -fsS http://127.0.0.1:8006/api/v1/resources/gpus
```
