# QASystem 部署说明

仓库根目录是唯一部署配置入口：

- `.env.example`：唯一环境变量模板。
- `Dockerfile`：后端运行环境镜像。
- `docker-compose.yml`：唯一 Compose 配置。
- `package-deploy.ps1`：生成运行时代码目录。

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

## 生成运行时代码

```powershell
.\package-deploy.ps1
```

如需同时导出已构建镜像：

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
│   ├── agentcode/
│   └── mcp_services/
└── qasystem-backend-latest.tar  # 可选
```

## 构建与启动

```bash
docker build -t qasystem-backend:latest .
docker compose up -d
curl http://127.0.0.1:8006/health
curl http://127.0.0.1:9180/health
```

Compose 启动 `backend`、`agent` 和 `redis`。统一 MCP 的宿主机地址为 `http://127.0.0.1:8006/mcp`，Agent 容器通过 `http://backend:8000/mcp` 访问。MySQL、Neo4j、Elasticsearch、LLM 和文件服务均作为外部服务配置。

Agent API 默认地址为 `http://127.0.0.1:9180`。详细配置、容量规划和排障见 [`agentcode/docs/部署运维指南.md`](../agentcode/docs/部署运维指南.md)。

业务代码更新后重新运行 `package-deploy.ps1`，再执行：

```bash
docker compose restart backend agent
```
