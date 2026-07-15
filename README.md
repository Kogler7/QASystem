# QASystem Knowledge Services

QASystem 当前提供版本化算法 HTTP 接口、一个统一 MCP 服务，以及保留的
`agentcode` Agent 运行时。历史 HTTP 路径作为 deprecated aliases 暂时保留。

## 对外接口

| 方法 | 路径 | 能力 |
| --- | --- | --- |
| POST | `/api/v1/pipelines/knowledge-ingestion` | 文档解析、切片、知识抽取和文本切片向量化聚合流水线 |
| POST | `/api/v1/documents/parse` | 文档解析为 Markdown |
| POST | `/api/v1/documents/chunk` | Markdown 切片 |
| POST | `/api/v1/graphs/extract` | 分片图谱抽取 |
| POST | `/api/v1/graphs/merge` | Graph 确定性去重 |
| POST | `/api/v1/embeddings/texts` | 多文本向量化 |
| POST | `/api/v1/embeddings/graphs` | Graph JSON 向量化 |
| POST | `/api/v1/graphs/confidence` | 图谱关系置信度更新 |

历史接口仅保留 deprecated 路径 aliases，请求和响应字段与 v1 完全一致，`info` 会注明迁移目标。

服务健康检查为 `GET /health`。统一 MCP Streamable HTTP 入口为 `/mcp`。
Agent 通过 MCP 自主选择知识、图谱检索、数据和可视化工具；仓库不再提供独立意图识别和方案规划 MCP。`problem-solve`、`semantic-align`、`plan-execute` Skills 负责约束求解框架，业务事实与方案仍以知识图谱为唯一知识来源。

AgentCode 后台服务默认监听 `9180`，提供异步任务、任务取消、状态查询和 SSE。文档入口见 [AgentCode README](agent/README.md)。

## 项目结构

```text
qasystem/
├── backend/              # FastAPI 路由、业务服务编排和契约测试
├── algorithm/            # 文档、抽取、向量、置信度及 MCP 业务实现
├── mcp_services/         # 统一 MCP Server 与领域工具
├── agent/                # Agent 与 MCP 客户端运行时
├── Dockerfile            # 后端运行环境镜像
├── docker-compose.yml    # 唯一容器编排配置
├── package-deploy.ps1    # 生成 deploy/qasystem-runtime
├── docs/                 # 架构、部署和协议说明
└── deploy/               # 运行时代码与模型，不纳入 Git
```

## 本地运行

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
$env:PYTHONPATH="$PWD;$PWD\agent"
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 本地测试

契约测试使用轻量桩，不需要模型、数据库或 Docker：

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.test.txt
.\.venv\Scripts\python.exe -m pytest -c backend\pytest.ini backend\tests -q
```

## 部署

镜像包含运行环境和业务代码，Compose 不使用宿主机代码覆盖 `/app`；模型单独挂载到 `/models`。

```powershell
Copy-Item .env.example .env
.\build-image.ps1
docker compose up -d
```

文档总入口见[文档索引](docs/文档索引.md)，项目部署见[部署说明](docs/部署说明.md)，接口联调见[算法接口协议](docs/算法接口协议.md)；Agent 专项部署和排障见 [AgentCode 部署运维指南](agent/docs/部署运维指南.md)。
