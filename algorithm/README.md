---
noteId: "338e21e0c30d11f1aea7cbe984ab9484"
tags: []

---

# `algorithm/` 说明

`algorithm/` 是系统的**业务与算法实现层**：文档处理、知识抽取、向量化、图谱检索、空间定位、
实体链接、SQL 取数与图表生成都在这里实现。MCP 工具与 HTTP 路由只做参数适配与编排，真正的逻辑在本目录。

```text
入库线：preproc → knowledge_pipeline → knowledge_extract.{latest,v2,v3,v3_1} → embedding_runtime → 外部写入
检索线：ans/QA（空间定位 / 实体链接）→ data_access（取数）/ visualization（图表）
支撑 ：term_vector（向量索引）、NL_to_cypher（切片与向量工具）、external_services（第三方接口）
```

---

## 1. 目录速查

| 目录 / 文件 | 作用 |
| --- | --- |
| `preproc/` | 各类文档解析为 Markdown（PDF/图片/Word/Excel/HTML/OFD） |
| `knowledge_pipeline/` | 语义切片、演示视图与向量存储编排 |
| `knowledge_extract/` | 第一版知识抽取（本体驱动）；现同时承载 V1 契约/共享件、版本路由 shim 与无本体摘要 |
| `knowledge_extract/version.py` | 读取 `KG_EXTRACT_ALGO_VERSION`（`latest` / `v2` / `v3` / `v3.1` / `jev`，默认 `latest`），并做合法性校验；`jev` 只进入实验副本 |
| `knowledge_extract/latest/` | `latest` 实体包：当前主版本代码副本，不是 v3 的软链/映射 |
| `knowledge_extract/v2/` | 新版抽取与本体编译，导出统一签名 `extract_aggregate` |
| `knowledge_extract/v3/` | 第三代抽取（独立重写），导出 `extract_aggregate` 并把 flat facts 转成 V2 聚合契约 |
| `knowledge_extract/v3_1/` | 实体与属性抽取：同一 LLM 分阶段生成、证据核验及定向修复；关系固定为空，未接入 Clef |
| `embedding_runtime.py` | 文本向量化运行时（本地模型 / 远程服务自动选择） |
| `knowledge_precipitation.py` | 图谱沉淀总编排（解析→切片→抽取→向量化→入库） |
| `term_vector/` | Term 语义向量索引的构建与检索 |
| `NL_to_cypher/` | 语义切片算法与向量调用工具 |
| `data_access/sql.py` | 只读 SQL 执行器 |
| `visualization/charts.py` | 十类图表生成 |
| `ans/QA/` | 知识空间定位、实体链接、图谱检索提示词与剪枝 |
| `confidence_calculate/` | 置信度计算（第三方能力开通前未接入主链路） |
| `intent_recognition_model/` | 历史意图识别目录，未接入当前统一 MCP |

---

## 2. `preproc/` —— 文档解析

`preproc_src/main_processor.py` 是总入口，按扩展名分发到 `preproc_src/processors/` 下各处理器：

| 处理器 | 处理的文档类型 |
| --- | --- |
| `ocr_processor.py` | 图片 / 需要 OCR 的页面 |
| `pdf_processor.py` | PDF |
| `docx_processor.py` | Word |
| `excel_processor.py` | Excel / CSV（含编码嗅探） |
| `html_processor.py` | HTML |
| `ofd_converter.py` | OFD 转换 |

输出统一为 Markdown，交由 `knowledge_pipeline` 继续处理。

---

## 3. `knowledge_pipeline/` —— 切片与存储

| 文件 | 作用 |
| --- | --- |
| `chunking.py` | **Max-Min 语义切片**：先按结构切段，再用相邻段向量相似度的“谷值”确定语义断点 |
| `presentation.py` | 切片结果的展示/整理 |
| `storage.py` | 向量与文件写入（经 `external_services`）：`store_vectors()`、`upload_file()` |

---

## 4. `knowledge_extract/`、`knowledge_extract/v2/` 与 `knowledge_extract/v3/` —— 知识抽取

### 4.1 `knowledge_extract/`（第一版，仅保留文本摘要）

> **抽取与图合并已全部由 V2/V3 承担（版本选择见 4.3）。** V1 的 `extractor.py` 里虽然还有一个叫 `extract_v2` 的函数，
> 那是第一版内部的第二个抽取器，**与 V2 的包无关**，目前没有任何生产调用方。
> V1 只保留新版抽取没有的能力：**无本体时的 `summarize_text` / `summarize_chunks`**，
> 两处调用分别在 MCP 的 `kg_extract_v2`（`mcp_services/domains/knowledge/extraction.py`）
> 与沉淀流程的无本体分支（`backend/app/services/knowledge_precipitation.py`）。

| 文件 | 作用 |
| --- | --- |
| `extractor.py` | 文本摘要（`summarize_text`）；历史抽取器已停用 |
| `ontology_schema.py` | 本体（PlantUML）解析 |
| `graph_utils.py` | 图谱结构工具（合并、去重、指纹等） |
| `config.py` / `batch.py` / `scheduler.py` | 配置、批处理与限流调度 |
| `uml/` | 本体定义（业务指标、数据模型、术语等） |

### 4.2 `knowledge_extract/v2/`（当前主用，默认版本）

| 文件 | 作用 |
| --- | --- |
| `harness_extractor.py` | 抽取主流程入口（包级 `extract` 由此导出） |
| `orchestrator.py` | 有界的 Plan/Verify/Repair 定向修复循环 |
| `composite.py` / `split_extractor.py` | 复合元素处理 / Entity-Relation 两轮拆分抽取 |
| `standard_skeleton.py` / `standard_alignment.py` / `standard_requirements.py` / `standard_semantics.py` | 标准类文档：结构骨架、对齐与条款语义抽取 |
| `official_structure.py` / `official_document_scope.py` / `official_attachments.py` | 公文类文档：结构抽取、来源边界与附件保留 |
| `chunk_context.py` / `chunk_metadata.py` | 切片上下文契约与确定性元数据补全 |
| `candidate_generation.py` / `candidate_models.py` / `candidate_selector.py` | 候选生成与本体感知的候选取舍 |
| `evidence.py` / `semantic_evidence.py` / `table_evidence.py` / `coverage.py` / `mapping_scorer.py` | 证据绑定与核验、覆盖度与映射打分 |
| `ontology_compiler.py` / `ontology_schema.py` | 本体编译与结构定义 |
| `document_entities.py` / `structure_entities.py` / `property_enrichment.py` / `hierarchy.py` | 确定性实体抽取与属性/层级补全 |
| `graph_assembler.py` / `graph_utils.py` / `entity_resolver.py` / `relation_resolver.py` | 图谱组装、实体/关系解析 |
| `llm_client.py` / `prompt_router.py` / `jev_client.py` | 抽取用 LLM 客户端、提示词构建与裁判客户端 |
| `result_contract.py` / `run_state.py` / `verification.py` | 对外结果契约、运行状态与最终校验 |
| `structured_text.py` / `structured_output.py` / `table_semantics.py` | 结构化文本/表格处理与输出校验 |
| `batch.py` / `scheduler.py` / `config.py` | 批处理、调度与配置 |
| （已迁出） | 原 `benchmark/` / `golden_task/` / `scripts/` / `archive/` 已迁到 `test_data/eval-v2` / `eval/tools/` / `tests/fixtures`，评测统一入口为 `eval/run.py`（旧版 legacy 脚本已清理），版本目录只保留算法代码 |
| `tests/` / `archive/` | 测试与归档代码 |

> 抽取的产物是**知识单元与关系**，最终由 `embedding_runtime` 向量化、经外部接口写入图谱。

### 4.3 `knowledge_extract/` 版本路由与 `latest` 默认

`knowledge_extract/` 现在不仅是 V1 兼容层，还集中了版本路由 shim：

| 文件 | 作用 |
| --- | --- |
| `knowledge_extract/version.py` | 读取 `KG_EXTRACT_ALGO_VERSION`（`latest` / `v2` / `v3` / `v3.1` / `jev`，默认 `latest`），并做合法性校验；`jev` 只进入实验副本 |
| `knowledge_extract/latest/` | `latest` 实体包：当前主版本代码副本，不是 v3 的软链/映射 |
| `knowledge_extract/v2/` | 新版抽取与本体编译，导出统一签名 `extract_aggregate` |
| `knowledge_extract/v3/` | 第三代抽取（独立重写），导出 `extract_aggregate` 并把 flat facts 转成 V2 聚合契约 |
| `knowledge_extract/v3_1/` | 实体与属性抽取：同一 LLM 分阶段生成、证据核验及定向修复；关系固定为空，未接入 Clef |

Backend `graph.extract` 与 MCP `kg_extract_v2` 通过
`backend/app/services/knowledge_extract_adapter.py` 调用上述 shim。
调用方只需指定 `KG_EXTRACT_ALGO_VERSION`：

- `latest`（默认）：运行独立的 `latest` 包，结果投影回 V1 逐 chunk 契约；
- `v2` / `v3` / `v3.1`：固定跑对应版本，便于算法版本对比评测；
- `jev`：运行 `knowledge_extract/jev_extract`，默认关闭，不改变 `latest`。

V2/V3/V3.1 都返回文档聚合结果，其中 V3.1 使用同一 LLM 分阶段抽取和证据核验，仅输出实体与属性；adapter 再按每 chunk 的证据锚点把结果投影回 V1 的逐 chunk 契约，因此上层代码无需随版本切换而改动。

---

## 5. 向量与存储

| 文件 | 作用 |
| --- | --- |
| `embedding_runtime.py` | 统一向量化入口：按环境自动选择本地模型或远程 Embedding 服务 |
| `NL_to_cypher/vector.py` | `batch_text_to_vectors()`：批量文本向量化（被向量召回复用） |
| `term_vector/build.py` | 构建 **Term 向量索引**：读取图谱 Term 节点及其逻辑属性，产出索引文件 |
| `term_vector/index.py` | 索引加载与检索（`search()`：余弦 top-K） |

Term 向量索引用于实体链接的**语义召回**：把用户问题拆成子问题后做整句向量检索，弥补纯字面匹配的不足。

---

## 6. `ans/QA/` —— 知识空间定位与图谱检索

| 文件 | 作用 |
| --- | --- |
| `config.py` | 读取根 `.env` 的图谱检索配置（`GRAPH_LLM_*`） |
| `utils.py` | `run_llm()`：OpenAI 兼容调用，含重试与超时 |
| `knowledge_map_client.py` | 兼容层，实际转发 `external_services.application` |
| `prompt_list.py` | 实体消歧、图谱关系/实体剪枝等提示词 |
| `entity_linker.py` | 实体识别、词面/向量召回、LLM 消歧、节点完整属性查询 |
| `graph_prune.py` | 基于 LLM 的关系/实体两轮剪枝（可选，用于缩小探索面） |
| `tools/knowledge_space_locator.py` | **知识空间定位器** |

### 6.1 知识空间定位器（`tools/knowledge_space_locator.py`）

- **只依据“用户问题 + 根空间名称”语义选出根空间**（不选刻度、不深钻子空间、不取知识单元）；
- 返回该根空间**绑定的维度与刻度**（直接展示）、**一跳子空间列表**（供后续按子空间取单元）；
- 维度/刻度说明全部取自图谱节点描述，不维护静态副本；
- 选不出时按“问题分词与候选名称的字面重叠”兜底，重叠为 0 不臆断。

### 6.2 实体链接（`entity_linker.py`）

- **词面召回**：按关键词 `name CONTAINS`；名称完全相同的候选标记 `exact_match` 并优先保留；
- **向量召回**：先把用户问题经 LLM 拆成自洽子问题，再用整句子问题做向量检索（需配置 Term 向量索引）；
- **LLM 消歧**：所有候选合并后，用**原始用户问题**整体判断相关性（≥0.7 采纳）；
- **回退**：LLM 判不出时先回退词面命中，再（无词面时）回退向量分数，并标记 `degraded`；
- 另提供 `get_node_full_info()`：返回节点自身字段 + 逻辑属性（不含原始 `properties_json`）。

---

## 7. `data_access/sql.py` —— 只读 SQL 执行

| 关注点 | 说明 |
| --- | --- |
| 执行器 | `SQLExecutor`：基于 SQLAlchemy，默认只读；按方言（MySQL/PostgreSQL/SQLite/MSSQL/Oracle）适配 |
| 结果映射 | `row._mapping[col]` 取列值（兼容 SQLAlchemy 2.0 的 `Row`） |
| JSON 安全 | `_json_safe()`：`Decimal→float`、`datetime→ISO`、`bytes→utf-8` |
| 输入规整 | `_sanitize_sql_text()`：把全角括号/逗号/空格与不可见字符规整为半角，避免解析错乱 |
| 上限 | 结果行数有上限，超限截断并记录 |

---

## 8. `visualization/charts.py` —— 图表生成

- `VisualizationExecutor`：柱状/饼/折线/散点/直方/面积/雷达/箱线/气泡/热力共十类图表的生成方法；
- `_save_fig()`：保存 PNG 到输出目录并返回文件路径与文件名；
- 对外只返回图片路径等摘要信息（**不把 base64 大字符串传给调用方**）。

---

## 9. 其它

| 目录 | 说明 |
| --- | --- |
| `knowledge_precipitation.py` | 图谱沉淀总编排：解析 → 切片 → 抽取 → 向量化 → 写入 |
| `confidence_calculate/` | 置信度计算（组件 + 工具 + 配置），第三方能力开通前未接入主链路 |
| `intent_recognition_model/` | 历史意图识别目录，当前统一 MCP 已不再使用 |

---

## 10. 模块调用关系

```text
preproc ──▶ knowledge_pipeline ──▶ knowledge_extract.{latest,v2,v3,v3_1} ──▶ embedding_runtime
                                        │                             │
                                        ▼                             ▼
                                   (本体/图谱结构)              external_services（写入）

用户问题 ──▶ ans/QA（空间定位 / 实体链接 / 提示词）
                │                │
                │                └─▶ term_vector（向量召回）
                ▼
        data_access（取数） / visualization（图表） ──▶ external_services（图谱查询）
```
