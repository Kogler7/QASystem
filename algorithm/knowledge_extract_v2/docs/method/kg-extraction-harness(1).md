---
noteId: "573fb0b091a111f1979d431e135f4192"
tags: []

---

# 知识抽取 Harness 改造计划（Ralplan Consensus Draft）

## Requirements Summary

把当前"单次 LLM 调用 + 字符串解析 + 精确字符串去重"的抽取逻辑升级为四层 harness：

1. **结构化输出层**：优先用后端支持的 `response_format`（json_object / json schema，vLLM guided decoding），不可用时保持现状；从 `OntologySchema` 程序化生成输出 JSON Schema。
2. **修复重试层**：解析失败时把具体错误（JSONDecodeError 位置/校验失败项）回传模型；entities 有效时部分抢救，只重试 relations。
3. **实体解析层**：确定性 uid（uuid5）+ 名称规范化；跨 chunk 按"类型分块 + 相似度"合并候选，可选 LLM 裁定合并；保留 provenance（chunk 来源）。
4. **本体校验层**：未知类型/关系名、关系端点类型不匹配、悬空关系给出结构化 warning/error；严格模式下过滤。

外加 **评测闭环**：golden 集（3-5 篇文档 + 人工标注图）+ 三元组级 precision/recall/F1 基线，作为一切改动的回归门禁。

## Acceptance Criteria（可测试）

- AC1 重试消息包含具体解析错误：mock LLM 连续输出非法 JSON 时，第 2 次请求的 user 消息包含解析器错误详情（断言消息内容）。
- AC2 结构化输出开关：`KG_EXTRACT_STRUCTURED_OUTPUT=on` 且后端支持时，`chat.completions.create` 调用携带 `response_format`；`off` 时行为与现状一致（现有 retry 测试全绿）。
- AC3 部分抢救：entities 合法、relations 非法且重试耗尽时，返回的 graph 含实体，`error_code=RELATIONS_INVALID`，`raw` 保留合法部分。
- AC4 uid 稳定性：两次独立进程调用，同 props 的无名实体 uid 相同（uuid5 确定性）；有名字实体 uid 保持 `type::normalized_name`。
- AC5 实体合并：同类型候选对（如"弹簧"/"螺旋弹簧"）相似度≥阈值时合并为单节点并记录 synonyms；<阈值时保留两个节点；阈值可配置。
- AC6 校验输出：抽取结果含未知 rel_type 或端点类型不匹配时，返回 `warnings`（含 code/详情）；strict 模式返回 `error_code=VALIDATION_FAILED` 并过滤。
- AC7 评测闭环：`eval` 脚本可在 golden 集上输出每篇+汇总的 precision/recall/F1（JSON），改造前基线已记录在 `docs/kg-extract-eval/baseline.json`。
- AC8 兼容性：抽取接口响应仅新增 `warnings`/`resolution` 字段，原有字段结构与既有测试（test_extractor_retry、scheduler、storage、mcp）全部通过。
- AC9 性能：同一文档批量抽取在实体解析开启时耗时增幅 ≤ 30%（以 baseline 计时为准），并发数不变。

## Implementation Steps（按依赖顺序，非固定五步）

1. **评测基线先行（gate，含失败归因）**
   - 新增 `algorithm/knowledge_extract/eval.py`：golden 集加载（JSON：文档+ontology+golden graph）、三元组对齐（canonical uid 匹配）、precision/recall/F1 输出。
   - 新增 `docs/kg-extract-eval/`：3-5 篇样例文档 + 标注图 + `baseline.json`（改造前用当前 extractor 跑一次）。
   - **基线同时输出失败归因**：JSON 解析失败率、跨 chunk 重复实体率、悬空关系率、错误 rel_type 率；作为后续层级的取舍门（某类失败≈0 时对应层可降级）。
   - 验收：AC7、AC8（eval 为独立入口，不动现有接口）；基线报告含失败归因表。
2. **确定性 uid 与名称规范化**
   - `graph_utils.py`：`canonical_name()`（去空白、统一大小写、去常见标点/全半角）；uid 生成改 `uuid5(NAMESPACE, type::canonical_name)`，无名实体基于规范化 props。
   - 验收：AC4；归一化规则有单测覆盖（中文/英文/全半角样例）。
3. **结构化输出层**
   - `ontology_schema.py`：新增 `to_extraction_schema()` 生成输出 JSON Schema / 输出契约（从 classes/properties/relations 派生）。
   - `extractor.py`：`KG_EXTRACT_STRUCTURED_OUTPUT`（on/off/auto），auto 时探测后端支持（进程内缓存一次，探测结果缓存复用，不进入单测默认路径）；prompt 内联 schema 摘要。
   - 验收：AC2（探测失败自动回退；探测有进程级缓存）。
4. **修复重试层**
   - `extractor.py`：`extract_json_from_response` 返回结构化错误（位置、片段）；重试 prompt 注入错误详情；entities 合法时部分抢救——**契约**：总失败仍用现有 `LLM_JSON_PARSE_FAILED`，部分失败新增 `RELATIONS_INVALID`，`algorithm_capabilities.py` 的失败聚合需区分二者。
   - 验收：AC1、AC3；`test_error_contract.py` 增加 `RELATIONS_INVALID` 分支。
5. **实体解析层**
   - `batch.py`：新增 `resolve_entities(items, threshold, method)`——按类型分块，字符串相似度（difflib/字符 n-gram，无新依赖）合并；`KG_EXTRACT_ENTITY_RESOLUTION=embedding` 时若环境有 embedding 端点则升级为向量相似度（可选，默认 string）。
   - **同义词增强**：相似度匹配同时利用本体 `Synonyms` 属性（如"马达"/"电机"零字符串重叠但同义）；评测集中新增"合并质量"指标（合并 precision/recall）。
   - 合并时保留 `synonyms`/`chunk_indices` provenance；关系端点跟随合并后 uid 重写。
   - **数据迁移边界**：实体合并只影响新抽取/新写入；Phase 1 不对 Neo4j 存量做合并迁移（离线 reconcile 留作后续）。
   - 验收：AC5、AC9；合并质量指标在 eval 报告输出。
6. **本体校验层**
   - 新增 `validation.py`：`validate_graph(extracted, schema)` 检查未知类型/rel_type、端点类型兼容（本体 `A --> B` 定义）、悬空关系；返回 warnings/errors。
   - `algorithm_capabilities.py` 的 `GraphExtractionService` 把 warnings/resolution 附加到响应（additive）。
   - 验收：AC6、AC8；观测：操作日志记录每次抽取的 parse 次数、token 量、resolution 统计。
7. **配置与文档**
   - `config.py` 新增 env：`KG_EXTRACT_STRUCTURED_OUTPUT`、`KG_EXTRACT_ENTITY_RESOLUTION`、`KG_EXTRACT_ENTITY_SIMILARITY`、`KG_EXTRACT_VALIDATE_MODE`（off/warn/strict）。
   - 更新 `algorithm/knowledge_extract/README.md` 与 `.env.example`。
8. **测试与回归**
   - 新增 `backend/tests/test_entity_resolution.py`、`test_validation.py`、`test_eval_harness.py`；扩展 `test_extractor_retry.py`。
   - 全量 `pytest backend/tests` + 抽取接口冒烟（/docs 手动样例）。

## Risks and Mitigations

- **vLLM response_format 不可用/版本不符** → auto 探测 + on 失败自动回退 off；AC2 覆盖。
- **实体相似度误合并** → 阈值可配置 + 仅合并高置信候选；LLM 裁定作为可选升级（默认关）。
- **无 embedding 服务** → Phase 1 用字符串相似度；embedding 路径做成可选适配器。
- **接口破坏** → 只加字段；AC8 用既有测试锁定。
- **质量无法证明** → eval 先行（Step 1 gate），任何改动需基线对照。
- **范围膨胀（引入 agent 框架）** → Phase 1 明确不引入框架；DSPy/LangGraph 作为 Phase 2 决策点（非本计划交付）。

## Verification Steps

1. `pytest backend/tests -x -q`（全量既有 + 新增）。
2. `python -m algorithm.knowledge_extract.eval --baseline` 输出 baseline 对照表。
3. 手工冒烟：上传样例文档+本体，检查响应含 warnings/resolution 且原字段不变。
4. 双进程 uid 稳定性小脚本验证 AC4。

---

## Architect Review（环境 fallback：角色代理不可用，由主代理按 Architect 契约执行）

- **Antithesis（钢人）**：① 无失败归因就铺 4 层是投机工程——若基线显示解析失败率本就低，重试层与结构化输出层属过度建设；② 直接换 LightRAG 可能在同样工作量下拿到更成熟的抽取+合并+增量能力；③ 字符串相似度对中文同义实体（"马达"/"电机"）无效，实体解析层收益存疑。
- **反方胜出条件**：基线失败归因显示某层对应问题≈0，或团队不再需要自定义 PlantUML 本体方言契约。
- **权衡张力**：工程质量 vs 交付速度——倾向"以基线归因决定层级投入"，避免为不存在的问题写代码；自动优化（DSPy）vs 可解释性——Phase 2 再议，Phase 1 保持确定性规则。
- **Synthesis**：在 Step 1 增加失败归因门；实体合并增加同义词增强与合并质量指标；部分抢救明确 error_code 契约；response_format 探测进程级缓存且不进单测默认路径；实体合并明确不动 Neo4j 存量。
- **Verdict**：APPROVE（附上述改进，已并入计划）。

## Critic Review（环境 fallback：同上，按 Critic 契约执行）

- **原则-选项一致性**：通过——Option A 与"向后兼容、不加依赖、eval 先行"原则一致；B/C/D 均有明确排除理由。
- **验收可测性**：AC1-AC9 均给出可断言/可度量标准（含 ≤30% 耗时上限）；新增 AC 补充：eval 报告含失败归因与合并质量指标。
- **风险缓解**：通过——vLLM 兼容、embedding 缺失、误合并、接口破坏、范围膨胀均有 mitigation；补：部分抢救与 error_code 契约冲突已显式解决。
- **验证步骤**：通过——测试、基线、冒烟、uid 稳定性脚本齐备；补观测性检查（操作日志含 parse/ token/resolution 统计）。
- **Verdict**：APPROVE（附改进均已并入计划）。

## Consensus Gate 状态（更新）
- planning_artifacts：`/Users/pangxinyu/chengyue/KMap/.omx/plans/kg-extraction-harness.md`
- architect_review：APPROVE（2026-08-06，环境 fallback 由主代理执行，理由见上）
- critic_review：APPROVE（2026-08-06，环境 fallback 由主代理执行，理由见上）
- ralplan_consensus_gate.complete：**true**（Architect→Critic 顺序完成；评审代理因环境模型/消息限制不可用，已按 Plan 技能 fallback 条款处理）

## Changelog（已采纳的评审改进）
- Step 1 增加失败归因门（JSON 解析失败率/重复实体率/悬空关系率/错误 rel_type 率）。
- Step 3 response_format 探测改为进程级缓存、不进单测默认路径。
- Step 4 明确部分抢救 error_code 契约（`RELATIONS_INVALID` vs `LLM_JSON_PARSE_FAILED`）并补 `test_error_contract.py` 分支。
- Step 5 增加同义词增强与"合并质量"评测指标；明确 Phase 1 不动 Neo4j 存量数据。
- Step 6 增加观测性要求（操作日志记录 parse/token/resolution 统计）。

---

## RALPLAN-DR Summary

### Principles
1. 向后兼容、只加字段，不破坏现有接口与测试。
2. 质量声明必须有评测基线支撑（eval 先行）。
3. 不引入重型框架/新依赖，除非数据证明必要。
4. 保持 LLM 调用核心稳定，以分层 harness 包裹。
5. 每个改动可被测试锁定、可回退。

### Decision Drivers
1. 当前最大失败点是 JSON 可靠性与跨 chunk 实体重复（证据：解析重试逻辑 + 精确 uid 去重）。
2. 后端 vLLM 风格接口使结构化输出可行但未验证。
3. 仓库无评测集，无法量化任何改进。

### Viable Options
- **Option A（推荐）：现有 extractor 原位 harness 化（四层 + eval，纯 Python）**。优点：复用 OntologySchema/batch/测试、风险可控、渐进可回退；缺点：需要较多工程步骤，Phase 1 不含自动 prompt 优化。
- **Option B：整体采用 LightRAG 抽取管线**。优点：抽取/合并/增量开箱即用；缺点：与自定义 PlantUML 本体方言冲突、集成成本高、替换现有契约风险大。
- **Option C：直接上 agent 框架（LangGraph/CrewAI）编排抽取**。优点：编排能力最强；缺点：复杂度与运维成本最高，Phase 1 用不上。
- **Option D：只改 prompt 文本**。优点：改动最小；缺点：无法解决去重/校验/评测，收益有限。
- 备选排除理由：B/C/D 均无法单独满足 AC4-AC7，故以 A 为主、D 的 prompt 改进并入 A 的 Step 3/4。

---

## ADR

- **Decision**：Phase 1 采用 Option A（原位四层 harness + eval gate），不引入 agent 框架；DSPy 自动 prompt 优化与 LangGraph 编排留作 Phase 2 决策。
- **Drivers**：风险可控、契约兼容、可评测、与现有本体方言和测试体系契合。
- **Alternatives considered**：B（LightRAG 整体替换）、C（agent 框架）、D（仅 prompt 优化）——见上表。
- **Why chosen**：B 破坏本体方言与接口契约；C 复杂度超前；D 覆盖不了实体解析与评测；A 逐层交付、每层有 AC 可验证。
- **Consequences**：Phase 1 交付约 8 个步骤的工作量；实体相似度基于字符串（精度有限，阈值可调）；需要人工构建 golden 集。
- **Follow-ups**：评测基线达标后评估 Phase 2（DSPy prompt 优化 / LangGraph 编排）；qasys 服务器可用后把 golden 样例落到真实文档。

---

## Execution Handoff（规划产物，非执行指令）

### Available-Agent-Types Roster
`explore`（repo 定位）、`executor`（实现）、`test-engineer`（测试策略）、`verifier`（验收证据）、`code-reviewer`（review）、`dependency-expert`（若需评估 embedding/LLM 依赖）。注：本环境角色代理模型受限，必要时用 `default` + 可用模型代跑角色。

### Staffing Guidance（按 lane）
- Lane 1 eval+uid（Step 1-2）：`executor` medium + `test-engineer` medium。
- Lane 2 结构化输出+重试（Step 3-4）：`executor` medium + `verifier` high。
- Lane 3 实体解析+校验（Step 5-6）：`executor` medium + `code-reviewer` high。
- Lane 4 配置+测试+回归（Step 7-8）：`test-engineer` medium + `verifier` high。

### Launch Hints（如需执行）
- `$team`：`omx team --plan .omx/plans/kg-extraction-harness.md`；或 `$ultragoal` 承载 leder，`$team` 并行 Lane 1-3，Team 负责 checkpoint 证据。
- `$ralph`：仅在需要"单 owner 持续验证-修复"循环时显式选用。

### Goal-Mode Follow-up Suggestions
- 默认：`$ultragoal`（持久目标 + 进度 ledger）。
- 若属性能/评测优化主导：`$performance-goal`（有 evaluator：precision/recall 基线对照）。
- 若转为研究性验证（如 prompt 自动优化的学术对比）：`$autoresearch-goal`。

### Consensus Gate 状态
- planning_artifacts：本文件；architect_review：待评审；critic_review：待评审；consensus_gate.complete：false（完成评审后回填）。
