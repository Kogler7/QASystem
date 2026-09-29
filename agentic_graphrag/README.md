# Agentic GraphRAG

一套全新的 Agentic GraphRAG 系统骨架，目标是用 **Skill + MCP** 实现可编排、可追溯、可评测的知识图谱增强问答。

这套系统先独立于现有 `backend/`、`algorithm/`、`agentcode/` 运行，后续可以按需接入旧系统已有的 Neo4j、NL2Cypher、知识抽取、意图识别、SQL 查询和可视化能力。

## 设计目标

- **Skill 定义工作流**：把 Agentic GraphRAG 的策略、步骤、停止条件和输出格式写成 Skill。
- **MCP 提供原子能力**：实体链接、图谱扩展、证据检索、SQL 查询、证据校验等都作为 MCP 工具暴露。
- **Agent 维护状态**：统一维护问题、知识空间、实体、路径、证据、工具轨迹、置信度和最终答案。
- **证据优先回答**：最终答案必须由 `EvidencePack` 支撑，不能只输出自然语言。
- **可独立服务化**：提供 FastAPI 入口，后续可以替换或并行现有问答入口。

## 目录结构

```text
agentic_graphrag/
├── README.md
├── pyproject.toml
├── .env.example
├── configs/
│   └── mcp_servers.example.json
├── docs/
│   └── architecture.md
├── skills/
│   └── agentic-graphrag/
│       └── SKILL.md
├── mcp_servers/
│   └── graph_retrieval/
│       └── server.py
├── src/
│   └── agentic_graphrag/
│       ├── __init__.py
│       ├── app.py
│       ├── orchestrator.py
│       ├── schemas.py
│       ├── settings.py
│       └── tool_client.py
└── tests/
    └── test_orchestrator.py
```

## 快速开始

安装为可编辑包：

```bash
cd agentic_graphrag
pip install -e .
```

启动 API：

```bash
uvicorn agentic_graphrag.app:app --reload --host 0.0.0.0 --port 8010
```

测试问答：

```bash
curl -X POST http://localhost:8010/api/agentic-graphrag/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"公开采购与非公开采购金额构成如何？"}'
```

当前默认使用 `LocalMockToolClient`，用于验证状态流和输出契约。核心 orchestrator 只依赖 Python 标准库；启动 API 时再安装 FastAPI 相关依赖。

接入真实 MCP：

```bash
set AGENTIC_GRAPHRAG_TOOL_CLIENT=mcp
set AGENTIC_GRAPHRAG_MCP_CONFIG=./configs/mcp_servers.example.json
```

MCP 模式会通过现有 `agentcode` MCP wrapper 调用工具，并把工具返回统一转换为 `LinkedEntity`、`GraphPath` 和 `EvidenceItem`。

本地开发也可以参考 [mcp_servers.stdio.example.json](configs/mcp_servers.stdio.example.json)，直接用 stdio 拉起本地 MCP server。

## 核心数据流

```text
Question
  -> locate knowledge space
  -> recognize problem type
  -> link entities
  -> retrieve graph paths / chunks / SQL evidence
  -> build EvidencePack
  -> verify evidence
  -> compose answer
  -> return FinalGraphAnswer
```

## 下一步

1. 将统一 MCP 层 `mcp_services/domains/graph/` 的图谱检索工具接入 `McpToolClient`。
2. 将 `algorithm/intent_recognition_model/mcp/*`（draw / intent / plan / sql_search）接入意图、计划和 SQL 查询路径。
3. 将知识抽取与沉淀能力（MCP `kg_extract_v2`，见 `mcp_services/domains/knowledge/extraction.py`）接入图谱构建/更新路径。
4. 给前端增加证据路径展示：实体、关系、SQL、原文片段、置信度。
