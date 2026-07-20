# QASystem 部署包

本目录是默认交付模式，可直接复制到目标服务器。运行镜像包含 Backend、Agent、应用源码、系统和 Python 依赖；Compose 不挂载外部源码。

## 目录内容

| 路径 | 说明 |
| --- | --- |
| `qasystem-backend-latest.tar` | QASystem Backend 与 Agent 离线镜像；仅在打包时指定 `-IncludeImage` 时生成 |
| `redis-7-alpine.tar` | Redis 离线镜像；仅在打包时指定 `-IncludeImage` 时生成 |
| `docker-compose.yml` | 启动 Backend、Agent 和 Redis 的 Compose 文件 |
| `.env.example` | 脱敏环境变量模板；部署前复制为 `.env` 并填写 |
| `source-mounted/` | 可选的源码挂载部署模式；与根目录镜像和 `models/` 共用 |
| `models/` | 本地模型挂载目录；按启用的能力准备相应模型 |
| `交付清单.json` | 文件、镜像标签和 SHA-256 校验信息 |

## 离线部署

```powershell
docker load -i .\qasystem-backend-latest.tar
docker load -i .\redis-7-alpine.tar
Copy-Item .env.example .env
# 编辑 .env：外部服务地址、LLM 密钥、端口和模型路径
docker compose up -d
curl http://127.0.0.1:8006/health
curl http://127.0.0.1:9180/health
```

`docker-compose.yml` 默认申请全部 NVIDIA GPU。CPU-only 部署时，删除 `backend` 服务中的 `gpus: all`；VLM/MinerU 能力仍需要根据实际可用硬件与模型配置评估。

## 模型与外部服务

`.env` 中的 `HOST_MODELS_DIR=./models` 指向本目录的模型挂载。使用 MinerU、VLM、向量化或置信度计算时，需要预先将对应模型复制到 `models/`。

如需将应用源码挂载到容器外部，使用 `source-mounted/` 中的 Compose 与环境模板；该模式应与根目录镜像和 `models/` 作为同一版本整体交付。

MySQL、Neo4j、Elasticsearch、文件服务和 LLM 服务是外部依赖；部署前将 `.env` 中的地址、账号和密钥替换为目标环境可访问的配置。不要交付包含真实密钥的 `.env`。

只使用文档解析和知识接口时，可以不启动 `agent` 与 `redis`；完整 Agent 异步任务和 SSE 流式接口需要 Redis。
