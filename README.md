# QASystem Knowledge Services

QASystem 当前提供三个知识服务 HTTP 接口、一个统一 MCP 服务，以及保留的
`agentcode` Agent 运行时。

## 对外接口

| 方法 | 路径 | 能力 |
| --- | --- | --- |
| POST | `/api/knowledge/precipitation` | 文档解析、知识抽取、切片和向量化 |
| POST | `/api/component/nl2cypher/text-to-vectors` | 多文本向量化 |
| POST | `/api/component/kg-update/calculate-confidence` | 图谱关系置信度更新 |

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

镜像只包含运行环境。代码挂载到 `/app`，模型挂载到 `/models`。

```powershell
Copy-Item .env.example .env
.\package-deploy.ps1
docker compose up -d
```

项目部署见 `docs/DEPLOYMENT.md`；Agent 专项部署和排障见 [AgentCode 部署运维指南](agent/docs/部署运维指南.md)。
