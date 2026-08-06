# QASystem 部署包

本目录是默认交付模式，可直接复制到目标服务器。运行镜像包含 Backend、Agent、应用源码、系统和 Python 依赖；Compose 不挂载外部源码。

## 目录内容

| 路径 | 说明 |
| --- | --- |
| `qasystem-backend-20260806.tar` | QASystem Backend 与 Agent 完整离线镜像 |
| `redis-7-alpine.tar` | Redis 7 Alpine 离线镜像 |
| `docker-compose.yml` | 启动自包含 Backend、Agent 和 Redis；Backend 入口自动管理 API、Worker 与每卡一个 vLLM engine |
| `download-models.ps1` | 在联网机器将 Docling、MinerU 模型下载到 `models/` |
| `ENV_MIGRATION.md` | 已有部署更新时需要合并的模型相关 `.env` 变更 |
| `.env.example` | 脱敏环境变量模板；部署前复制为 `.env` 并填写 |
| `deploy/models/` | 本地模型挂载目录；按启用的能力准备相应模型 |
| `交付清单.json` | 文件、镜像标签和 SHA-256 校验信息 |

## 离线部署

```powershell
docker load -i .\qasystem-backend-20260806.tar
docker load -i .\redis-7-alpine.tar
Copy-Item .env.example .env
# 编辑 .env：外部服务地址、LLM 密钥、端口和模型路径
docker compose up -d
curl http://127.0.0.1:8006/health
curl http://127.0.0.1:8006/agent/health
```

模型不包含在 Docker 镜像中。请先在可访问模型仓库的机器执行：

```powershell
.\download-models.ps1 -ImageName qasystem-backend:latest
```

将生成的 `deploy/models/` 目录与镜像和部署文件一并复制到离线服务器。默认下载 Docling、MinerU Pipeline/VLM 和 512 维 BGE-small-zh；BGE-small-zh 同时用于向量化和置信度语义评分。如只使用部分能力，可使用 `-SkipDocling`、`-SkipMineruPipeline`、`-SkipMineruVlm` 或 `-SkipEmbedding` 跳过对应模型。

`.env.example` 默认将 Backend 发布到 `0.0.0.0:8006`，可由其他主机访问。生产环境应通过宿主机防火墙、安全组或反向代理限制来源；只需本机访问时设为 `BACKEND_BIND=127.0.0.1`。

`docker-compose.yml` 将 GPU 设备能力交给 Backend 容器，但 supervisor 只使用 `GPU_DEVICE_IDS` 选中的卡。目标 8 卡 A40 服务器默认设置 `GPU_DEVICE_IDS=4,5,6,7`，只占用后四卡；入口进程在这四张卡上各启动一个常驻 vLLM engine，并按 CPU/GPU 数量生成 VLM dispatch worker、共享 Embedding/Confidence worker 和 CPU worker。无需在 Compose 中枚举进程；可用 `EXECUTION_CPU_WORKERS`、`EXECUTION_VLM_WORKERS_PER_GPU` 和 `EXECUTION_AUX_WORKERS_PER_GPU` 覆盖自动数量。

## 模型与外部服务

`.env` 中的 `HOST_MODELS_DIR=./deploy/models` 指向交付目录下的模型挂载。模型目录包括 `deploy/models/docling-models`、`deploy/models/mineru/pipeline`、`deploy/models/mineru/vlm` 和 `deploy/models/bge-small-zh-v1.5`。

MySQL、Neo4j、Elasticsearch、文件服务和 LLM 服务是外部依赖；部署前将 `.env` 中的地址、账号和密钥替换为目标环境可访问的配置。不要交付包含真实密钥的 `.env`。

只使用文档解析和知识接口时，仍需启动 Redis，但它不会暴露宿主机端口。`EXECUTION_MODE=async` 表示计算由统一队列和 Worker 执行，不表示业务 HTTP API 默认立即返回 `202`：未携带 `Prefer` 时仍同步返回原状态码和原业务 `data`，只新增顶层 `execution`；`Prefer: respond-async` 才立即返回任务句柄，`Prefer: wait=N` 请求显式有界等待。显式异步调用在 SSE `job.completed` 后通过 `execution.result_url` 获取完整最终 envelope。MCP 生产默认 `MCP_EXECUTION_MODE=sync`，同样由队列执行但返回原工具结果；设为 `async` 才返回句柄。Redis 不可用时返回 503，不会回退到 Gateway 内执行。
