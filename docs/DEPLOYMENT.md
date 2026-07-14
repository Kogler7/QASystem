# QASystem 部署说明

仓库根目录是唯一部署配置入口：

- `.env.example`：唯一环境变量模板。
- `Dockerfile`：包含后端、Agent、系统程序和 Python 依赖的完整运行镜像。
- `docker-compose.yml`：唯一 Compose 配置。
- `build-image.ps1`：构建候选镜像、执行容器内验证并原子替换正式标签。
- `package-deploy.ps1`：生成源码快照，并可导出已验证的离线镜像包。

`deploy/` 只存放生成的 `qasystem-runtime/` 和可选镜像包，不再保存第二套 Compose、`.env` 或部署文档。

## 准备配置

```powershell
Copy-Item .env.example .env
```

LLM 配置按调用模块分组：

| 模块 | 环境变量前缀 |
| --- | --- |
| 知识抽取 | `KG_EXTRACT_LLM_*` |
| Agent 运行时 | `AGENT_LLM_*` |

## 构建并验证镜像

```powershell
.\build-image.ps1
```

脚本先构建 `candidate-latest`，验证 Python 依赖、系统程序、文档解析能力以及 Backend/Agent 入口。只有全部通过后才替换 `qasystem-backend:latest`，旧镜像会在不再被引用时删除。

内网环境可先导入批准的基础镜像，再通过 `-PythonImage`、`-NodeImage` 指定本地标签；默认使用 Docker Hub 官方标签。

如需生成源码快照并导出离线镜像：

```powershell
.\package-deploy.ps1 -IncludeImage
```

生成目录：

```text
deploy/
├── models/
├── qasystem-runtime/
│   ├── backend/
│   ├── algorithm/
│   ├── agent/
│   ├── mcp_services/
│   └── 部署清单.json
└── qasystem-backend-latest.tar  # 可选
```

## 构建与启动

```bash
docker compose up -d
curl http://127.0.0.1:8006/health
curl http://127.0.0.1:9180/health
```

Compose 启动 `backend`、`agent` 和 `redis`。统一 MCP 的宿主机地址为 `http://127.0.0.1:8006/mcp`，Agent 容器通过 `http://backend:8000/mcp` 访问。MySQL、Neo4j、Elasticsearch、LLM 和文件服务均作为外部服务配置。

Agent API 默认地址为 `http://127.0.0.1:9180`。详细配置、容量规划和排障见 [`agent/docs/部署运维指南.md`](../agent/docs/部署运维指南.md)。

Backend 与 Agent 代码已经打入镜像，Compose 不再用宿主机目录覆盖 `/app`。这样代码版本和依赖版本始终一致。业务代码或依赖更新后执行：

```powershell
.\build-image.ps1
docker compose up -d --force-recreate backend agent
```
