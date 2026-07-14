# 项目结构

## 运行入口

`backend/app/main.py` 是业务 HTTP/MCP ASGI 入口。`agent/agentcode/api.py` 是独立的后台 Agent ASGI 入口；二者在 Compose 中使用同一镜像、不同进程。

## 目录职责

```text
backend/
├── app/main.py
├── app/routers/
├── app/services/
├── app/schemas.py
└── tests/

mcp_services/
├── runtime.py
├── server.py
└── domains/
    ├── knowledge/
    ├── graph/           # 知识空间定位与原子图谱检索
    ├── data/
    └── visualization/

algorithm/
├── knowledge_precipitation.py
├── preproc/
├── knowledge_extract/
├── ans/QA/             # 图谱定位、实体链接与单层搜索实现（历史目录名）
├── NL_to_cypher/
├── confidence_calculate/
├── data_access/
└── visualization/

Dockerfile
docker-compose.yml
package-deploy.ps1
.env.example
```

## 边界规则

- HTTP 与 MCP 复用算法业务函数，不复制业务实现。
- 所有 MCP 工具注册到 `mcp_services.runtime.mcp`。
- 算法模块不得自行创建 MCP 或 FastAPI Server。
- `.env.example` 是唯一配置模板，运行时只加载仓库根 `.env`。
- LLM 配置按 `KG_EXTRACT_LLM_*`、`GRAPH_LLM_*` 和 `AGENT_LLM_*` 隔离。
- Agent 自行理解意图并调度原子工具，不提供独立意图识别或方案规划 MCP。
- `problem-solve`、`semantic-align`、`plan-execute` Skills 提供求解框架；ProblemClass、SolutionClass、业务规则和资源必须来自知识图谱。
- 新业务配置统一使用环境变量，不在导入时改写环境变量。
- `agentcode` 只作为 Agent 和 MCP 客户端，不承载服务端业务实现。

AgentCode 的详细结构见 [`agent/README.md`](../agent/README.md) 和[架构设计](../agent/docs/架构设计.md)。
