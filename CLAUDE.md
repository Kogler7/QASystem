---
noteId: "8acde120c77711f18de48d0829b834d0"
tags: []

---

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

QASystem 是**知识图谱驱动的问答/求解系统**：文档解析→切片→抽取→向量化沉淀为知识图谱，再通过统一的 MCP 工具与 HTTP 接口对外提供知识空间定位、图谱检索、数据查询与图表生成。`AGENTS.md` 中的交付/布局/测试规则是硬性约束，改动前必读。

## 常用命令

### 运行服务

```bash
./run.sh                          # 本地开发：uvicorn 起 Backend，端口 8000，MCP 挂在 /mcp
docker compose up -d              # 容器部署：backend 端口 8006，agent 9180
docker compose --profile decisions up -d   # 加决策模型（clef/nanojev，需 GPU）
curl http://127.0.0.1:8006/health # 健康检查
```

### 测试

```bash
python3 -m pytest -q                                  # 全量（Windows 开发机用 .venv/Scripts/python.exe）
python3 -m pytest tests/backend/test_xxx.py -q        # 单文件
python3 -m pytest tests/backend/test_xxx.py::test_yyy -q   # 单个用例
```

注意：`pytest.ini` 的 `testpaths` 是**显式白名单**（`tests/` 下部分文件 + `algorithm/knowledge_extract/*/tests` 等）。新增测试文件若不在 testpaths 内，默认 `pytest` 不会跑到——要么改 pytest.ini，要么显式传路径。`conftest.py` 已把仓库根、`agent/`、`algorithm/` 加入 `sys.path`，测试内直接 `import algorithm.xxx` / `import agentcode.xxx`。

### 评测与发布门禁

改了解析、切片、抽取或沉淀流水线后，发布前按顺序过门禁（全绿才允许部署）：

```bash
python3 -m pytest -q                     # 1. 单元测试全绿
python3 eval/mini_eval.py                # 2a. 快速验证（4 个 case，连 http://127.0.0.1:8006）
python3 eval/run.py --match-mode llm --fail-on-empty --min-f1 0.5 \
  --max-case-seconds 1800 --max-stage-seconds parse=300,chunk=120,extract=900
                                         # 2b. 全量门禁：退出码 0 才过；2=有空抽取/失败 case，4=F1 或耗时未过，1=配置错误
python3 -m algorithm.knowledge_extract.v2.benchmark_lint   # benchmark 数据质量门禁
```

评测输出统一写 `tmp/eval/runs/<时间戳>/`（report.md 含整体/分类型/单文件三层 P/R/F1）。抽取版本可用 `--extract-version v2/v2.2/v3/v3.1/latest` 在不重启 Backend 的情况下逐次对比。88 是测试服务器，统一用 `qasystem_backend`，不另建评测实例。

## 总体架构

四层结构，**业务实现集中在 `algorithm/`，对外只通过 MCP 与 HTTP 两种入口暴露**：

```text
mcp_services/（统一 FastMCP，薄适配）   backend/（FastAPI 路由 + 异步执行队列）
              └──────────┬───────────────────┘
                         ▼
              algorithm/（文档处理 / 抽取 / 向量 / 检索 —— 全部业务实现）
                         ▼
              external_services/（应用团队接口适配：Cypher / 向量 / 文件）
```

- **`mcp_services/`**：所有 MCP 工具注册在单一 `FastMCP` 实例（`qasystem-tools`），由 Backend 挂载到 `/mcp`。**禁止在任何业务目录新增独立 MCP Server**，新能力一律注册为本服务的一个工具。统一返回契约 `{"code":0,"message":"success","data":{},"error":null}`；`dispatch.py` 的 `maybe_submit()` 按 `MCP_EXECUTION_MODE` 决定 inline 还是入队。
- **`backend/`**：FastAPI 应用（`app/main.py` 装配）。`app/execution/` 是异步执行框架：任务按**资源类**（`io.graph`、`io.llm`、`cpu.chart`、`gpu.*` 等）分队列，Worker 消费执行；`operations.py` 把工具名映射为 Worker 操作。`/agent/{path}` 代理到 Agent 运行时。
- **`agent/agentcode/`**：单智能体运行时（LLM↔Tool 执行循环、MCP 客户端、Skill 加载、预算控制、SSE 事件），容器内 `python -m agentcode.api` 起在 9180。
- **`external_services/`**：`ApplicationServiceClient` 统一调用应用团队服务。所有第三方端点必须在 `application.py` 的 `ENDPOINTS` 注册表声明，**不得散落在算法代码里**；未开通的能力调用时才显式报错。图谱查询统一走 `graph.cypher.query` 并自动透传用户鉴权上下文。

### 两条主流程

- **入库线**：`algorithm/preproc/`（文档→Markdown）→ `knowledge_pipeline/`（语义切片、向量存储）→ `knowledge_extract.{latest,v2,v2_2,v3,v3_1,...}`（按本体抽取，由 `KG_EXTRACT_ALGO_VERSION` 选择，默认 `latest`）→ `embedding_runtime.py` → 写入图谱/向量库。
- **检索/求解线**：`ans/QA/tools/knowledge_space_locator.py`（定位根知识空间）→ `ans/QA/entity_linker.py`（词面召回+向量召回+LLM 消歧）→ 干系人→问题类→方案类 → 按方案执行（图谱续查 / `data_access/sql.py` 只读 SQL / `visualization/charts.py` 画图）。

### 图谱模型约定

- 业务概念统一是 **`KnowledgeUnit`**；干系人→问题类→方案类沿 `KNOWLEDGE_RELATION` 一跳即对应类型。
- 逻辑属性是独立 `KnowledgeProperty` 节点（`HAS_LOGICAL_PROPERTY`）；工具返回逻辑属性时不含原始 `properties_json`。
- 空间层级：`KnowledgeSpace -[HAS_CHILD_SPACE]-> KnowledgeSpace`、`-[CONTAINS_UNIT]-> KnowledgeUnit`。
- **核心原则**：所有业务信息来自知识图谱，代码不内嵌领域知识；缺字段/缺口径沿图谱继续查，不查表结构、不读文件、不用模型记忆补。

## 易踩的坑

- **两套错误码号段不同义**：MCP 工具层 `ToolCode`（`mcp_services/result.py`）与 HTTP 错误码（`docs/错误码协议.md`）同号不同义（如 MCP `3000`=参数非法，HTTP `3000`=切片失败）。
- **`latest` 必须是实体目录**，不得是 `v3` 的软链或运行时映射；`KG_EXTRACT_ALGO_VERSION=latest` 直接运行 `latest` 包。对外版本号 `v3.1`/`v2.2` 对应包名 `v3_1`/`v2_2`。
- **鉴权透传**：队列记录只存 `auth_context_id`，禁止把 Authorization 头放进 payload、日志、SSE 或结果。
- **数据库边界**：SQL 直连（`DATABASE_URL`）是唯一保留的数据库兼容能力；禁止恢复直连 Neo4j/Elasticsearch，未开通的 graph-write/confidence/document-evidence 能力调用时必须显式失败。
- **GPU**：生产 8 卡 A40 通常 `GPU_DEVICE_IDS=4,5,6,7`，改部署配置时保留该变量。

## 仓库规则摘要（详见 AGENTS.md）

- **Git**：合并一律 `git merge --no-ff`，禁止 `--ff-only` 和 `pull --rebase`。
- **布局**：benchmark 数据只放 `test_data/`，评测脚本只放 `eval/`，模块文档只放 `docs/`（模块目录只留 README.md）；`tests/`、`test_data/`、`eval/`、`tmp/` 不进交付镜像。
- **镜像**：唯一环境镜像 `qasystem-env-v1`，Dockerfile 只装环境不含业务源码；源码经 `QASYSTEM_SOURCE_DIR` 只读挂载，模型经 `HOST_MODELS_DIR` 挂到 `/models`。
- **测试维护**：改可观测行为必须在同一 commit 更新覆盖该行为的测试；禁止删测试或静默失败测试取绿。
- **评测 case 沉淀**：评测失败的 case 必须沉淀为 `test_data/eval/` 回归 case 或对应层单测后才算关闭。

## 文档导航

- `docs/文档索引.md` — 全部架构/接口/部署文档入口
- `algorithm/README.md`、`mcp_services/README.md`、`backend/README.md`、`external_services/README.md`、`agent/README.md`、`eval/README.md` — 各模块详解
