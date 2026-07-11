# QASystem Knowledge Services

QASystem 当前提供四个知识沉淀 HTTP 接口、一个统一 MCP 服务，以及保留的
`agentcode` Agent 运行时。

## 对外接口

| 方法 | 路径 | 能力 |
| --- | --- | --- |
| POST | `/api/knowledge/precipitation` | 文档解析、知识抽取、切片和向量化 |
| POST | `/api/component/nl2cypher/text-to-vector` | 单文本向量化 |
| POST | `/api/component/nl2cypher/text-to-vectors` | 多文本向量化 |
| POST | `/api/component/kg-update/calculate-confidence` | 图谱关系置信度更新 |

服务健康检查为 `GET /health`。统一 MCP Streamable HTTP 入口为 `/mcp`。

## 项目结构

```text
qasystem/
├── backend/              # FastAPI 入口、四接口适配和契约测试
├── algorithm/            # 文档、抽取、向量、置信度及 MCP 业务实现
├── mcp_services/         # 统一 MCP Server 与领域工具
├── agentcode/            # Agent 与 MCP 客户端运行时
├── docker/               # 运行环境镜像和部署包生成脚本
├── docs/                 # 架构、部署和协议说明
├── models/               # 本地模型，不纳入 Git
└── deploy/               # 本地交付目录，不纳入 Git
```

## 本地运行

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
$env:PYTHONPATH="$PWD;$PWD\agentcode"
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
.\docker\package-deploy.ps1
```

详细说明见 `docs/DEPLOYMENT.md`。
