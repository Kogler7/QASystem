# QASystem 项目说明

QASystem 是一个**知识图谱驱动的问答/求解系统**：先把文档解析、切片、抽取、向量化沉淀为知识图谱，
再通过统一的 MCP 工具与 HTTP 接口，对外提供知识空间定位、图谱检索、数据查询与图表生成等能力。

本文件介绍**总体框架与各部分作用**；更细的代码说明见：

- [`algorithm/README.md`](algorithm/README.md) —— 各算法与业务模块详解
- [`mcp_services/README.md`](mcp_services/README.md) —— 统一 MCP Server 与各工具契约
- [`backend/README.md`](backend/README.md) —— HTTP 接口与异步执行框架
- [`external_services/README.md`](external_services/README.md) —— 第三方接口与模型服务适配
- [`docs/`](docs/) —— 架构、协议与部署文档

---

## 1. 总体架构

系统由四层组成，**业务实现集中在 `algorithm/`，对外只通过 MCP 与 HTTP 两种入口暴露**：

```text
┌──────────────────────────────┐        ┌──────────────────────────────┐
│  MCP 入口  mcp_services/      │        │  HTTP 入口  backend/          │
│  统一 FastMCP 工具集（薄适配） │        │  FastAPI 路由 + 异步执行队列   │
└──────────────┬───────────────┘        └──────────────┬───────────────┘
               │                                        │
               └────────────────┬───────────────────────┘
                                ▼
                 ┌───────────────────────────────┐
                 │  算法与业务实现  algorithm/     │
                 │  文档处理 / 抽取 / 向量 / 检索  │
                 └──────────────┬────────────────┘
                                ▼
                 ┌───────────────────────────────┐
                 │ 应用团队接口适配                │
                 │ external_services/            │
                 │ 图谱 Cypher / 向量 / 文件       │
                 └───────────────────────────────┘
```

设计约定：

- **知识来自图谱**：业务概念、关系、口径、方案与资源全部由图谱提供，代码不内嵌领域知识；
- **工具是原子操作**：MCP 工具只做“定位/查询/取数/画图”等可观察动作，不在工具内部隐藏流程决策；
- **重活可入队**：耗时任务经后端执行队列（按资源类分队列）由 Worker 执行；
- **第三方能力注册制**：未开通的能力在注册表中标记，调用时才返回明确错误。

---

## 2. 模块职责一览

| 目录 | 职责 | 详见 |
| --- | --- | --- |
| `algorithm/` | 全部算法与业务实现：文档解析、切片、知识抽取、向量化、图谱检索、空间定位、实体链接、SQL 执行、图表生成 | [algorithm/README.md](algorithm/README.md) |
| `mcp_services/` | 统一 MCP Server：注册各领域工具，统一返回契约，按需把任务交给后端队列 | [mcp_services/README.md](mcp_services/README.md) |
| `backend/` | FastAPI 应用：v1 算法 HTTP 接口、知识沉淀接口、任务状态/取消/SSE，以及异步执行框架（manager/worker/queue/store/hub） | [backend/README.md](backend/README.md) |
| `external_services/` | 对接应用团队的统一接口客户端：图谱 Cypher 查询、向量写入、文件上传，以及用户鉴权上下文透传 | [external_services/README.md](external_services/README.md) |
| `docs/` | 架构说明、接口协议、部署与验收文档 | `docs/文档索引.md` |
| `docker/` | 运行时校验与离线验收脚本/说明 | `docs/部署/离线验收说明.md` |

---

## 3. `algorithm/` 总览（细分见子文档）

按“入库”和“检索/求解”两条线组织：

**入库线**：`preproc/`（文档→Markdown）→ `knowledge_pipeline/`（语义切片、向量存储）
→ `knowledge_extract.{latest,v2,v3,v3_1}`（按本体抽取知识单元与关系，
由 `KG_EXTRACT_ALGO_VERSION` 选择，默认直接运行独立的 `latest` 包）
→ `embedding_runtime.py`（向量化）→ `external_services`（写入图谱/向量库）。

**检索与求解线**：

- `ans/QA/tools/knowledge_space_locator.py`：按问题语义定位**根知识空间**，返回其维度刻度与子空间；
- `ans/QA/entity_linker.py`：术语对齐（词面召回 + 向量召回 + LLM 消歧）；
- `ans/QA/prompt_list.py`、`graph_prune.py`：检索用提示词与可选的图谱剪枝；
- `data_access/sql.py`：只读 SQL 执行器（取数环节）；
- `visualization/charts.py`：十类图表生成；
- `term_vector/`：Term 语义向量索引的构建与检索，供实体链接的向量召回使用。

---

## 4. `backend/` 说明（简述）

| 路径 | 作用 |
| --- | --- |
| `app/main.py` | FastAPI 应用装配：挂载路由、健康检查、MCP 入口 |
| `app/routers/` | 对外路由：算法能力（文档/图谱/向量/置信度）、知识沉淀、执行任务 |
| `app/services/` | 路由对应的业务编排 |
| `app/execution/` | 异步执行框架：任务提交与调度（`manager.py`）、Worker 消费（`worker.py`）、队列与存储（`queue.py`/`store.py`）、事件推送（`hub.py`）、操作映射（`operations.py`）、资源分类（`resources.py`）、监督（`supervisor.py`） |
| `app/schemas.py` | 请求/响应模型 |
| `app/environment.py` | 本地启动的 dotenv 引导；容器环境由 Compose 注入，业务统一从 `docker.configuration` 读取 |

**资源类**（如 `io.graph`、`io.llm`、`io.db`、`cpu.chart`、`gpu.*`）决定任务进入哪个队列；
`operations.py` 把 MCP 工具名映射为 Worker 可执行的操作。

---

## 5. `external_services/` 说明（简述）

| 文件 | 作用 |
| --- | --- |
| `application.py` | `ApplicationServiceClient`：统一 HTTP 调用、用户鉴权头捕获与透传、能力注册表 `ENDPOINTS`、`execute_cypher()` 图谱查询 |
| `models.py` | 相关数据模型 |

所有第三方能力在注册表中声明；**未开通的能力在调用时才报“能力不可用”**，不影响其它能力运行。
图谱查询统一走 `graph.cypher.query`，自动携带用户上下文做权限过滤。

---

## 6. 两条主流程

### 6.1 知识入库

```text
文档 → 解析为 Markdown → 语义切片 → 按本体抽取知识单元/关系 → 向量化 → 写入图谱与向量库
```

### 6.2 问答与求解

```text
用户问题
 → 知识空间定位       定位根空间（返回维度刻度、子空间）
 → 实体链接           术语对齐为图谱实体
 → 干系人 → 问题类 → 方案类   取到方案（候选均带完整属性）
 → 按方案执行         图谱信息不足则沿图谱继续查；需数据则执行 SQL；需图表则画图
 → 交付               结论 + 依据 + 知识缺口
```

**统一原则**：所有业务信息来自知识图谱；缺字段/缺口径时沿图谱继续查找，
不查数据库表结构、不读文件、不用模型记忆补。

---

## 7. 图谱模型与关键约定

- 业务概念统一是 **`KnowledgeUnit`**（术语、问题类、方案类、数据表等都是 KnowledgeUnit）；
- 干系人 → 问题类 → 方案类沿 `KNOWLEDGE_RELATION` 连接，**一跳即对应类型**；
- 逻辑属性是独立的 `KnowledgeProperty` 节点，经 `HAS_LOGICAL_PROPERTY` 连接；
  工具返回逻辑属性时**不包含原始 `properties_json`**；
- 知识空间层级：`KnowledgeSpace -[HAS_CHILD_SPACE]-> KnowledgeSpace`、`KnowledgeSpace -[CONTAINS_UNIT]-> KnowledgeUnit`；
- 图谱中的说明文字（维度/刻度描述等）以**节点属性为准**，代码不维护静态副本。

---

## 8. 对外能力

- **MCP**：单一服务 `qasystem-tools`，HTTP 入口 `/mcp`，工具清单与契约见 [mcp_services/README.md](mcp_services/README.md)；
- **HTTP v1**：文档解析/切片、图谱抽取/合并、文本与图谱向量化、知识沉淀流水线、置信度（保留）、
  任务状态/结果/取消与 SSE；
- **健康检查**：`GET /health`。

## 配置和日常运维

应用配置统一通过 `ops config` 审核；部署默认值在根 `.env.example`，声明、消费者和迁移随版本维护。业务热参数及模型计划保存在 `runtime-config/config.json`，启动参数和密钥保存在部署 `.env`。Compose 维护服务结构，不再维护第二套默认值。

常用入口为 `ops check`、`ops reload`、`ops deploy`、`ops status` 和 `ops history`。`reload` 不重启容器，`deploy` 应用审核后的启动计划；改 `.env` 需要 recreate，不需要重建环境镜像。评测使用本轮命令行参数，不保留默认评测文件。

完整说明见 [配置体系说明](docs/部署/配置体系说明.md)、[配置迁移](docs/部署/配置清理与标准名称迁移.md)、[小模型部署](docs/部署/小模型统一部署.md) 和 [评测入口](eval/README.md)。尚未安装 `ops` 时使用 `python3 ops.py --project-dir . install`；交付包使用 `python3 runtime/ops.py`。
