# `mcp_services/` 说明

`mcp_services/` 是系统的**统一 MCP 服务层**：所有 MCP 工具注册在同一个 `FastMCP` 实例上（服务名 `qasystem-tools`），
由后端挂载到 `/mcp`。本目录是**薄适配层**——只做参数适配、返回契约与执行模式处理，业务实现全部在 `algorithm/`。

> 约定：**禁止在任何业务目录新增独立 MCP Server**；新增能力一律作为本服务的一个工具注册。

---

## 1. 目录结构

| 文件 / 目录 | 作用 |
| --- | --- |
| `runtime.py` | 单例 MCP（内部监听约定，部署HTTP挂载到Backend） |
| `server.py` | 注册各领域模块并启动（默认 Streamable HTTP） |
| `result.py` | 统一返回契约与错误码 |
| `dispatch.py` | 执行模式与队列分发（`maybe_submit`） |
| `domains/graph/locator.py` | 知识空间相关工具 |
| `domains/graph/retrieval.py` | 实体链接、图谱邻居、问题/方案类获取 |
| `domains/data/sql.py` | SQL 执行工具 |
| `domains/knowledge/extraction.py` | 知识抽取与维度刻度/空间初始化工具 |
| `domains/visualization/charts.py` | 图表工具 |
| `domains/execution.py` | 异步任务查询与取消 |

---

## 2. 统一返回契约

所有工具返回同一结构：

```json
{"code": 0, "message": "success", "data": {}, "error": null}
```

- 成功：`code = 0`；
- 失败：`code` 为非零错误码、`data = null`、`error.details` 给出可诊断信息，**禁止静默成功**。

错误码（`result.py` 的 `ToolCode`）：

| 错误码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 3000 | 参数非法 |
| 3100 / 3200 / 3300 / 3400 | 知识处理 / 图谱 / 数据 / 可视化失败 |
| 3900 / 3901 / 3902 | 工具未实现 / 执行队列不可用 / 外部能力未开通 |
| 3999 | 内部错误 |

> 注意：上表是 **MCP 工具层** 的 `ToolCode`，与 HTTP 接口错误码（`docs/错误码协议.md`）是两套独立号段，同号不同义（例如 HTTP `3000` 表示切片失败，MCP `3000` 表示参数非法）。

`normalize_tool_result()` 会把算法层返回的 `{success: false, ...}` 一类结构统一归一为上述契约。

---

## 3. 执行模式与队列（`dispatch.py`）

`maybe_submit(operation, payload, resource_class=...)` 决定一次工具调用怎么执行：

| `MCP_EXECUTION_MODE` | 行为 |
| --- | --- |
| `inline`（默认） | 直接在调用进程中执行 |
| `sync` | 提交到后端执行队列并在 Worker 中执行，调用方等待结果（保持原返回契约） |
| `async` | 提交后立即返回任务句柄（`job_id` 等），结果通过任务工具获取 |

- 只有在后端执行层可用（`EXECUTION_MODE=async`）时才会真正入队，否则退回同步执行；
- **资源类**（如 `io.graph`、`io.llm`、`io.db`、`cpu.chart`）决定任务进入哪个队列；
- Worker 侧通过 `backend/app/execution/operations.py` 的映射执行同一个工具函数，避免重复实现。

---

## 4. 工具清单与契约

### 4.1 知识空间与图谱（`domains/graph/`）

| 工具 | 入参 | 返回 | 说明与约束 |
| --- | --- | --- | --- |
| `locate_knowledge_space` | `user_question`、`app_requirement?` | `可回答`、`知识空间`（根）、`子空间`（一跳 `{id,name}`）、`维度刻度`（`{维度:[刻度]}`）、`置信度` | 只按“问题 + 根空间名称”语义选根空间；不做刻度选择、不下钻子空间、不取知识单元 |
| `get_all_subspaces` | — | 所有知识空间及其绑定刻度 | 定位失败时查看候选 |
| `search_space_units` | `space_id?`、`space_name?`、`limit=20` | `space`、`units`（含 `properties`） | 查询某知识空间**直接包含**的单元（一跳 `CONTAINS_UNIT`），逐个补齐完整属性（含逻辑属性）。**仅在方案明确要求“去某子空间/库找信息”时使用**；名称支持精确/包含模糊 |
| `link_entities` | `keywords`、`question?` | `candidates`（`name/id/labels/score/is_relevant/llm_judgment`） | 术语对齐：词面召回 + 向量召回 + LLM 消歧；**只在流程最初的术语对齐阶段调用一次** |
| `get_stakeholders` | — | `stakeholders`（`{id,name}`） | 取全部干系人实例 |
| `find_problem_classes` | `stakeholder_id`、`limit=20` | `candidates`（含完整属性） | 干系人 → 问题类：沿 `KNOWLEDGE_RELATION` 一跳，候选带完整属性 |
| `find_solution_classes` | `problem_id`、`limit=20` | `candidates`（含完整属性） | 问题类 → 方案类：同上 |
| `query_entity_properties` | `entity_id` | `entity_id`、`name`、`labels`、`properties` | 节点自身字段 + 逻辑属性（不含原始 `properties_json`） |
| `search_graph` | `start_entity_id`、`question?`、`relationship_types?`、`direction`、`limit` | `triples` / `found_entities`（邻居带 `content`、`resource_type`、`properties.logical_properties`） | 一跳邻居查询，**只返回 `KnowledgeUnit` 类型邻居**；**没有 `depth` 参数**（固定单跳，多跳逐层调用） |

> 说明：`search_graph` 的邻居 `content` 会按长度上限截断（`GRAPH_NEIGHBOR_CONTENT_MAX`，默认 1200），
> 需要全文时再调 `query_entity_properties`。

### 4.2 数据（`domains/data/sql.py`）

| 工具 | 入参 | 说明 |
| --- | --- | --- |
| `execute_sql` | `sql`、`params?`、`database_url?`、`read_only=True` | 执行 SQL（默认只读）。返回 `{success, data: 行数组, rowcount, columns, ...}`，行为 `{列名: 值}` |

> 表结构类工具（`list_tables`、`describe_table`、`get_database_info`、`list_supported_dialects`）**已停用**，
> 仅保留注释；项目要求业务信息（含表/字段口径）来自知识图谱。

### 4.3 知识处理（`domains/knowledge/extraction.py`）

| 工具 | 说明 |
| --- | --- |
| `kg_extract_v2` | 从文本抽取知识：传本体时按元模型抽取实体/关系图；不传本体时返回文本摘要 |
| `kg_get_dimension_scales` | 维度刻度查询（依赖图谱写入能力，未开通时返回 `3902`） |
| `kg_init_knowledge_space` | 知识空间初始化（同上） |
| `kg_store_extraction` | 图谱存储（同上） |

### 4.4 可视化（`domains/visualization/charts.py`）

| 工具 | 说明 |
| --- | --- |
| `draw_bar_chart` / `draw_pie_chart` / `draw_line_chart` / `draw_scatter_plot` / `draw_histogram` / `draw_area_chart` / `draw_radar_chart` / `draw_box_plot` / `draw_bubble_chart` / `draw_heatmap` | 生成对应类型图表，返回图片路径等摘要信息 |
| `list_chart_types` | 列出支持的图表类型 |

> 图片已落盘，返回结果中的 `image_base64` 会在包装层剔除，避免超大字符串进入模型上下文。

### 4.5 任务编排（`domains/execution.py`）

| 工具 | 说明 |
| --- | --- |
| `get_job` | 查询任务状态与当前执行摘要 |
| `get_job_result` | 取成功任务的结果 |
| `cancel_job` | 请求取消排队中/运行中的任务 |

---

## 5. 与后端的关系

```text
调用方（Agent / 客户端）
      │  MCP 调用
      ▼
mcp_services 工具（薄适配）
      │ maybe_submit
      ├── inline：直接在本进程执行算法实现
      └── sync/async：提交到 backend 执行队列 → Worker 按 operations 映射执行同一函数
```

- 鉴权：后端捕获用户上下文并以短期引用传给 Worker，**图谱工具始终带权限过滤**；
- 图谱工具默认经队列执行；SQL 直连是当前唯一的数据兼容例外。

---

## 6. 开发约定

- 新增工具：在对应 `domains/*` 下用 `@mcp.tool(name=...)` 注册，业务实现放 `algorithm/`；
- 返回统一走 `tool_success` / `tool_error` / `normalize_tool_result`，不要自定义结构；
- 需要入队的工具使用 `maybe_submit`，并在 `backend/app/execution/operations.py` 补对应映射；
- 工具名与 schema 变更需同步使用方（skills/契约测试）。

## 开发启动与关系型 SQL

标准部署的 MCP HTTP 接口由 Backend 内置，无单独监听环境变量。独立开发可用 `python -m mcp_services.server --transport stdio`，或 `--transport streamable-http --host 127.0.0.1 --port 8890` 显式绑定；这些参数不改变 Backend 的内置接口。

`DATABASE_URL` 是关系型数据库 SQL 连接，不是图数据库或向量数据库地址，部署默认空。工具请求显式 `database_url` 参数保持兼容；未填参数和配置时明确失败。旧 `MCP_DATABASE_URL` 在 ops 中经人工审核迁移，运行时无旧名回退。
