# Phase 1 Step 1：extract_v2() 基准集（Benchmark Set）

依据 [Phase1 设计文档](../docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md) §37
推荐开发顺序的 **Step 1：建立基准集**。

> Step 1 的目标：先冻结旧 `extract_v2()` baseline，作为后续 Ontology Compiler /
> ChunkContext / Entity-Relation 拆分等所有改动的回归门禁（对照 §36 AC-10、§35 指标口径）。

## 目录结构

```text
benchmark/
├── README.md
├── eval.py                 # 自检 / 指标计算 / 基线冻结 / 报告
├── cases/                  # 7 个基准用例（gold 标注）
│   ├── 01_standard.json
│   ├── 02_official_document.json
│   ├── 03_terminology.json
│   ├── 04_ontology_class_extraction.json
│   ├── 05_simple_rule.json
│   ├── 06_composite_rule.json
│   └── 07_official_document.json
├── ontologies/             # 运行时本体（PlantUML 方言，与 JSON 用例配对）
└── results/                # 基线产物（JSON + Markdown 报告 + raw 结果）
```

## 覆盖类别与真实来源（对应 Step 1 清单）

真实正文全部取自 `docs/抽取结果md/`（与 `docs/中车待抽取pdf/` 中的 PDF 一一对应，
为人工检查过的抽取结果）。正文由 md 的 `DocumentUnit.originalText` 与“证据追溯”
原文片段拼接，保持原文措辞；gold 依据 md 中人工检查的抽取表并按用例范围过滤。

| 类别 | 用例 | 来源（抽取结果 md ↔ 中车待抽取 pdf） | 重点 |
| --- | --- | --- | --- |
| 标准（standard） | `standard-01` | 结构化抽取-GBT+31486-2024 电动汽车用动力蓄电池电性能要求及试验方法.md（人工检查，Turtle）↔ GBT+31486-2024 PDF | 标准元数据、章条结构、要求条款、对象/特性、枚举/符合性/数值/相对值约束、检验行动、外部资源、标准化对象 |
| 公文（official_document） | `official-document-01` | 关于开展OpenClaw（“龙虾”）等开源智能体安全风险排查工作的通知 | DocumentUnit 锚点、运行主体/对象/活动/信息/交互/约束、EVIDENCED_BY |
| 公文（official_document） | `official-document-02` | 关于印发中国中车2026年数智转型工作要点的通知 | 数智转型要点节选：总体思路、“1169”、主要工作目标、穿透式监管、企业架构 |
| 术语（terminology） | `terminology-01` | 关于印发中国中车2026年数智转型工作要点的通知 | 真实术语条目（“1116N”顶层设计、“六统一”原则、主数据治理、人才能力模型）→ Terminology 本体 |
| 本体类抽取（ontology_class_extraction） | `ontology-class-extraction-01` | 关于开展OpenClaw…的通知 | 目标范围纪律：仅 OperationalActivity，范围外类型不得输出（AC-2） |
| 简单规则（simple_rule） | `simple-rule-01` | 关于开展OpenClaw…的通知 | 真实规范性条款 → SimpleRule（ruleText/triggerCondition/action、constrainsObject） |
| 复合规则（composite_rule） | `composite-rule-01` | 关于开展OpenClaw…的通知 | 真实复合条款（未经论证测试 且 未经批准 不得部署）→ CompositeRule AND + hasSubRule（AC-6） |

> 说明：7 个用例中 6 个来自人工检查的 `抽取结果md`；`standard-01` 的抽取结果
> 为 Turtle 格式（`结构化抽取-GBT+31486-2024 电动汽车用动力蓄电池电性能要求及试验方法.md`）。
> gold 按 Phase1 §3 Schema Authority 收敛到 `标准本体模型.md` 的规范标识：
> `unit`（非 measurementUnit）、`hasCharacteristic`（非 hasProperty）、
> `specifiesCharacteristic`（非 specifiesProperty）、`imposesConstraint`（非 hasConstraint）；
> `describedIn`/`propertyDescription` 未在 `标准本体模型.md` 中声明，gold 不使用。
> `allowedValue`/`conformsTo`/`comparedTo`/`constraintTarget` 为标准本体中无对应属性的
> 显式 Ontology Extension（`standard_ontology.puml` 中已标注 [Ontology Extension]）。

## Gold 标注约定

1. **Schema Authority**：`type` / 属性键 / `rel_type` 必须全部来自 `ontologies/*.puml`
   中运行时 `OntologySchema` 可解析的稳定标识；不用展示名或自创字段。
2. **Identity**：实体 `name` 与本体 `@identity` 声明一致（如 Clause 用 `clauseNumber`、
   Terminology 用 `TermName`、DocumentUnit 用 `unitId`）。
3. **证据**：gold 实体/关系可带 `evidence.quote`，`eval.py check` 会校验引文确实存在于
   原文（去掉空白后子串匹配），保证标注可追溯（§14 Evidence）。
4. **聚焦型标注**：每个用例在 `notes` 中说明标注范围。未被纳入 gold 的内容（如标准的
   环境温湿度约束、公文部分交换链路）不要求抽取，避免口径膨胀。
5. **属性规范化**：日期等按本体值域写规范值（如 `2026-01-15`）；数值保持数值类型
   （`minValue: 100.0`），评测按规范化值比较（100 与 100.0 等价）。

## 指标口径（§35，统一主指标尚未冻结）

每个用例输出：

- **Entity Precision / Recall / F1**：按 `(type, canonical(name))` 对齐；
- **Property Exact / Normalized Accuracy**：在匹配到的 gold 实体上比较属性值
  （排除合成 `name` 键）；
- **Relation Precision / Recall / F1**：按
  `(from_type, canonical(from_name), rel_type, to_type, canonical(to_name))` 对齐；
- **Composite Accuracy**：复合用例的 `hasSubRule` 成员关系召回 + `combinationType` 正确率；
- **Schema Validity**：UNKNOWN_* / ENTITY_OUT_OF_SCOPE / 端点类型不匹配等告警统计；
- **Failure Attribution**：JSON 解析失败、LLM 请求失败、重复实体、悬空关系、错误码；
- **Evidence Coverage**：Step 1 为 N/A（extract_v2 无证据输出，Step 3 引入后启用）。

统一主指标为**候选公式**（未冻结，验收前需项目双方确认）：

```text
0.3*entity_f1 + 0.2*property_normalized_accuracy + 0.3*relation_f1
（复合用例再计入 0.2*composite_accuracy）
```

## 使用方式

```bash
# 1) 校验基准集：本体可解析、gold schema 合法、证据可定位
python knowledge_extract_v2/benchmark/eval.py check

# 2) 冻结正式基线（需要 LLM 端点：KG_EXTRACT_LLM_BASE_URL / API_KEY / MODEL）
python knowledge_extract_v2/benchmark/eval.py run --mode live \
  --module knowledge_extract_v2 \
  --out knowledge_extract_v2/benchmark/results/baseline.json

# 3) 离线验证评测链路（不调 LLM；输出会标记 stub，不能作为正式基线）
python knowledge_extract_v2/benchmark/eval.py run --mode stub:gold --out .../baseline.stub-gold.json
python knowledge_extract_v2/benchmark/eval.py run --mode stub:empty --out .../baseline.stub-empty.json
python knowledge_extract_v2/benchmark/eval.py run --mode stub:error --out .../baseline.stub-error.json

# 4) 从已冻结的 baseline.json 重新生成 Markdown 报告
python knowledge_extract_v2/benchmark/eval.py report --run knowledge_extract_v2/benchmark/results/baseline.json
```

说明：

- `eval.py` 不依赖 `openai` 包；只有 `--mode live` 才加载真实 extractor。
- `--module` 可切换 `knowledge_extract_v2` / `knowledge_extract`（当前二者字节一致）。
- 每次 `run` 会同时把每个用例的完整抽取结果写入 `results/raw/<case_id>.json`，
  便于失败复现与报告重生成。

## 当前基线状态

- ✅ 基准集 7 个用例已建立并通过自检（本体解析、gold schema、证据定位）。
- ✅ 评测/基线工具已完成，并用 `stub:gold`（全对 1.0）、`stub:empty`（零召回）、
  `stub:error`（JSON 解析失败归因）验证链路。
- ⏳ **正式 extract_v2() baseline 尚未冻结**：本环境未安装 `openai` 且未配置
  `KG_EXTRACT_LLM_BASE_URL`。配置端点后执行上面对应的 `--mode live` 命令，
  产物写入 `results/baseline.json`，即完成 Step 1 的“冻结旧 baseline”验收。

## 与后续 Step 的关系

- Step 2（Ontology Compiler + TargetScope）：`ontology-class-extraction-01`
  （真实公文文本 + OperationalActivity 单类范围）即为 AC-2 验收样例；
  需要父类+子类展开时可用规则本体（Rule → SimpleRule/CompositeRule）扩展同格式用例；
- Step 3（ChunkContext + Evidence）：gold 已带 `evidence.quote`，
  届时可升级为 chunk 级用例与 Evidence Coverage 指标；
- Step 5/6（Standard / OfficialDocument Strategy）：`standard-01` 与
  `official-document-01` 直接对应两个策略的验收；
- Step 7（Composite Element）：`composite-rule-01` 对应 AC-6。

## Step 2（Ontology Compiler + TargetScope）状态

依据 Phase1 设计 v3 §37 Step 2：先实现 class hierarchy / properties / relations /
target expansion / schema QA，验收后再做 LLM 变更。

- ✅ [`ontology_compiler.py`](../ontology_compiler.py)：`OntologyCompiler` +
  `TargetScopeResolver` + `run_schema_qa`（strict / permissive），输出
  `ExtractionPlan`（class_specs / relation_specs / identity_specs /
  feature_specs / target_scope / schema_warnings），并对 `ontology_schema.py`
  补充未解析关系/继承端点记录与 `descendants()`（向后兼容）。
- ✅ 覆盖 AC-2（单类、多类、父类含子类、全文、范围外类型不输出）与 AC-9
  （不完整本体只报告、不编造、不崩溃，含继承环防护）。
- ✅ 测试：`../tests/test_ontology_compiler.py`（无需 openai / pytest），24 个用例。
- ✅ 验收命令：`python knowledge_extract_v2/benchmark/step2.py`（编译 4 个
  benchmark 本体并输出 QA；`--strict` 在 error 级 QA 时非零退出）。
- ⏳ 未做：LLM 流程变更（按 §37 Step 2 约定留待验收后）；feature_specs 仅做
  确定性推导（§8 缺省元数据保持 None）；composite_specs 留待 Step 7。

## Step 3（ChunkContext + Evidence）状态

依据 Phase1 设计 v3 §37 Step 3：不重新解析文档；统一接收 `text/chunks`；
抽取结果绑定 `chunk_id` 与 evidence span。

- ✅ [`chunk_context.py`](../chunk_context.py)：`ExtractionRequest` /
  `ExtractionChunk`（§5 兼容规则：text-only 包装单 chunk、chunks 优先、
  text+chunks 以 chunks 为主输入）+ `ChunkContextManager`（§6：chunk_id 索引、
  前后邻接、同结构上下文、有界拼接、空白不敏感 span 定位、evidence_text）。
- ✅ [`evidence.py`](../evidence.py)：`Evidence`（§14.3，evidence_type 校验）、
  `bind_evidence`（已有 quote 回填 span；无 quote 时按实体/关系端点名定位）、
  `verify_evidence`（§16 L1 Source Existence + L2 Literal Match；
  L3/L4 留待 Step 8 特性/评分阶段）、`evidence_coverage`（AC-4 代理）。
- ✅ 测试：`../tests/test_chunk_evidence.py`（无需 openai / pytest），25 个用例。
- ✅ 验收命令：`python knowledge_extract_v2/benchmark/step3.py`——将 7 个用例
  文本切成段落 chunk，绑定 gold 的 `evidence.quote` 并验证 L1/L2；gold 必带
  quote 项全部通过、实体 evidence 覆盖率 100%（relations 的 quote 未强制）。
- ⏳ 未做：LLM 流程变更（抽取结果由 LLM 生成时自动携带 evidence，属 Step 4）；
  chunk 级 benchmark 用例（gold 带 chunk_id 的用例可后续补充）。

## Step 4（Entity / Relation 拆分）状态

依据 Phase1 设计 v3 §37 Step 4：从 one-shot `entities + relations` 变为
`entity candidate -> resolve -> relation candidate`（§15 两段式）。

- ✅ [`split_extractor.py`](../split_extractor.py)：`extract_split()` 两段式
  抽取——Pass 1 实体候选（§15.1：允许类 + FeatureSpec + evidence quote，禁关系）；
  `CandidatePool` 解析/去重（§20：类型 + 规范化 identity 一致才 merge）；
  Pass 2 关系候选（§15.2：端点引用稳定 candidate id，基于 accepted 实体目录）；
  关系签名/端点存在性/cardinality 结构校验（§19、AC-7 无悬空）；
  O1 中间对象模型（§25）、O2 graph + triples（§26/§27，UID 只由稳定类型+identity）。
- ✅ [`llm_client.py`](../llm_client.py)：live OpenAI 兼容适配器
  `create_llm_client()`（惰性导入 openai，`llm(prompt) -> str`）。
- ✅ 测试：`../tests/test_split_extractor.py`（无需 openai / pytest），13 个用例
  （prompt 契约、跨 chunk 去重合并、范围外拒绝、悬空/签名拒绝、失败归因、
  UID 稳定性、benchmark gold 冒烟）。
- ✅ 验收命令：`python knowledge_extract_v2/benchmark/step4.py`——gold 驱动
  两段式桩，7 个用例实体/关系与 gold 一致，实体证据覆盖 100%。
- ⏳ 未做：复合元素组装（Step 7）、Mapping Scorer（Step 8）、Coverage Auditor
  （Step 9）；结构化输出/重试沿用现有 extractor 的增强可后续接入。

## Step 5（Standard Strategy）状态

依据 Phase1 设计 v3 §37 Step 5：structure / metadata / constraint / action /
reference coverage（§11.1-§11.4）。

- ✅ [`standard_strategy.py`](../standard_strategy.py)：`StandardExtractionStrategy`
  + `STRATEGY_REGISTRY` / `get_strategy`（§10，未知类型回退 generic）：
  - 确定性抽取（§11.1）：标准元数据（standardNumber / documentName /
    issuedDate / effectiveDate / status / constraintType / languageVersion，
    仅输出本体声明的属性）、结构单元（消费 chunk metadata → Section/Clause +
    hasStructuralElement 顶层 / hasSubClause 父子）、ICS/CCS 分类代码；
  - 覆盖审计（§11.4）：structural / constraint / action / reference /
    characteristic coverage，只发现 gap 不修复（§22/§23.2，全部 info 提示）；
    行动标记排除“试验方法”等名词性用法，避免误报。
- ✅ [`coverage.py`](../coverage.py)：§22 `CoverageGap` 模型（Step 9 复用）。
- ✅ `split_extractor.extract_split` 集成：策略确定性实体先入池并注入实体 pass
  提示词（防 LLM 重复抽取），确定性关系在实体 pass 后入池，末尾输出
  `coverage_gaps`（intermediate_model 同步）。
- ✅ 测试：`../tests/test_standard_strategy.py`（无需 openai / pytest），10 个用例。
- ✅ 验收命令：`python knowledge_extract_v2/benchmark/step5.py`——standard-01
  gold 桩 32/32 实体、34/34 关系一致且无覆盖 gap；空 LLM 场景能检测出
  constraint/action/reference/characteristic gap。
- ⏳ 未做：OfficialDocument Strategy（Step 6）、复合元素（Step 7）。

## Step 6（OfficialDocument Strategy）状态

依据 Phase1 设计 v3 §37 Step 6：DocumentUnit / OperationalAgent /
OperationalObject / OperationalActivity / OperationalInformation /
OperationalExchange / OperationalConstraint / EVIDENCED_BY（§12）。

- ✅ [`official_document_strategy.py`](../official_document_strategy.py)：
  `OfficialDocumentExtractionStrategy` 已注册进 `STRATEGY_REGISTRY`：
  - 确定性抽取（§12.1-§12.2）：公文头部元数据（文件标题/来文单位/来文字号，
    仅输出本体声明属性）；按条目标记（一、/（一）/1.）建立 DocumentUnit
    （DU-xxx + unitType/unitTitle/unitOrder/originalText，首句粒度对齐 gold）
    + OfficialDocument -[HAS_PART]-> DocumentUnit；
  - 覆盖审计（§12.4/§12.6）：operational 实例 EVIDENCED_BY -> DocumentUnit
    覆盖检查；OperationalExchange sourceAgent/targetAgent 强校验
    （缺失 -> INCOMPLETE_RELATION_FEATURE）；结构单元必须存在 DocumentUnit；
    DocumentUnit 必须 HAS_PART 挂接；
  - `prompt_hints()`：活动动词（§12.3）/交互类型/约束类型/强制程度提示注入
    实体 pass。
- ✅ 测试：`../tests/test_official_document_strategy.py`（无需 openai /
  pytest），9 个用例。
- ✅ 验收命令：`python knowledge_extract_v2/benchmark/step6.py`——
  official-document-01 实体 25/25、关系 42/42 与 gold 精确一致且无结构性
  gap；official-document-02 的 gold operational 实体/关系子集全部命中；
  EVIDENCED_BY 审计不对已覆盖实体误报。
- 注：Step 4 验收显式使用 `strategy="generic"`，保持两段式机制验收与策略
  验收职责分离（策略行为由 step5.py / step6.py 覆盖）。
- ⏳ 未做：复合元素组装（Step 7）、Mapping Scorer（Step 8）、通用
  Coverage Auditor（Step 9）。

## Step 7（Composite Element）状态

依据 Phase1 设计 v3 §37 Step 7 / §21：优先满足 CompositeRule 示例（AC-6：
member extraction、membership relation、combination type 全部正确）。

- ✅ [`composite.py`](../composite.py)：`CompositeSpec`（§21）+ 自动发现
  （本体 `hasSubRule`/`hasMember` 关系 + combination 属性及注释中的允许值，
  如 `AND / OR / SEQUENCE`）+ `CompositeElementHandler`：
  - 确定性组装：CompositeRule 按 ruleText 子串或 triggerCondition + action
    支撑判定自动补建 `hasSubRule` 成员关系（不把复合规则只存成字符串）；
  - 校验：成员数 >= 2、combinationType 必须存在且在允许集合内；
  - 校验问题输出为 INSUFFICIENT_COMPOSITE_MEMBERS / MISSING_COMBINATION_TYPE /
    INVALID_COMBINATION_TYPE 告警，并计入 profile.composite_auto_memberships。
- ✅ 编译期集成：`OntologyCompiler.compile` 自动填充 `plan.composite_specs`；
  `extract_split` 在关系 pass 后运行组装（Process/FaultTree 可经同一机制扩展）。
- ✅ 测试：`../tests/test_composite.py`（无需 openai / pytest），7 个用例。
- ✅ 验收命令：`python knowledge_extract_v2/benchmark/step7.py`——
  composite-rule-01 关系 pass 为空时自动补建 2 条 hasSubRule 与 gold 一致
  （combinationType=AND 合法）；全 gold 场景精确一致、无复合告警。
- ⏳ 未做：Mapping Scorer（Step 8）、通用 Coverage Auditor（Step 9）。

## Step 8（Mapping Scorer）状态

依据 Phase1 设计 v3 §37 Step 8 / §17-§19：可配置的 attribute + relation +
context 评分，参考阈值 0.85 / 0.70（正式上线前需 benchmark 校准）。

- ✅ [`mapping_scorer.py`](../mapping_scorer.py)：`MappingScorer`（权重自动
  归一化，阈值可配置并校验先后顺序）：
  - 实体评分：attribute（特性覆盖 + 属性合法性 + identity；§18 required
    penalty 仅在本体声明 required 时启用）、relation（作为源端的关系参与覆盖）、
    context（§16 L4：本体未允许上下文推断时该分量不适用，不猜测）；
  - 关系评分：evidence（local evidence）+ 合法性（rel_type/端点类型）+
    cardinality（§19，多重性作为结构 validator）；
  - 不适用分量自动按剩余权重重新归一化；状态 >=0.85 accepted、
    0.70-0.85 low_confidence、<0.70 rejected（§34）。
- ✅ `extract_split` 集成：默认“注释模式”（annotate，不改抽取结果），
  `apply_mapping_threshold=True` 时按阈值过滤 rejected 候选并输出
  MAPPING_REJECTED / MAPPING_LOW_CONFIDENCE 告警；结果附带 `mapping` 摘要
  （逐候选分数/状态/分解、平均分、低置信/拒绝计数、权重与阈值），
  O1 中间模型的 mapping 字段同步真实分数。
- ✅ 测试：`../tests/test_mapping_scorer.py`（无需 openai / pytest），13 个用例。
- ✅ 验收命令：`python knowledge_extract_v2/benchmark/step8.py`——standard-01 /
  composite-rule-01 注释模式评分后 raw 与 gold 一致，mapping 摘要完整
  （composite-rule-01 的 CR-001 score=0.95 accepted，分数分解可见）。
- ⏳ 未做：通用 Coverage Auditor（Step 9，复用 coverage.py 的 CoverageGap）。

## Step 9（Coverage Auditor）状态

依据 Phase1 设计 v3 §37 Step 9 / §22-§23 / AC-8：只发现 gap，不自动修复。

- ✅ [`coverage.py`](../coverage.py) 新增 `CoverageAuditor`（§23 通用五类检查）：
  - §23.2 Feature Coverage：accepted 实例缺失本体特性（required 未声明时仅
    info 提示，声明时硬错误）；
  - §23.3 Relation Coverage：cardinality min=1 的源端/目标端必需关系缺失；
  - §23.4 Evidence Coverage（evidence_missing）；
  - AC-8 结构漏抽：带结构元数据的 chunk 未绑定任何实体；
  - §23.5 Composite Coverage：成员数 / combinationType 缺失。
- ✅ `extract_split` 集成：通用审计 + 策略专项审计（standard 的 constraint/
  action/reference、official 的 EVIDENCED_BY/Exchange）合并输出
  `coverage_gaps`（generic-* 前缀避免 gap_id 冲突）。
- ✅ 测试：`../tests/test_coverage_auditor.py`（无需 openai / pytest），9 个用例。
- ✅ 验收命令：`python knowledge_extract_v2/benchmark/step9.py`——
  standard-01 全 gold 无误报（structural/constraint/action/composite 全缺），
  关系无 quote 的 evidence_missing 能检出；空 LLM 场景 constraint/action/
  reference/feature/relation 类别齐全；composite-rule-01 自动组装后无
  composite_membership gap。
- ⏳ 剩余 Phase 1 收尾：Step 10 GraphAssembler + Compat（现有 graph 契约已
  在 Step 4 落地，Step 10 主要是回归验证与兼容性收口）。

## Step 10（GraphAssembler + Compat）状态

依据 Phase1 设计 v3 §37 Step 10 / §26-§28：保留现有 API。

- ✅ [`graph_assembler.py`](../graph_assembler.py)：`GraphAssembler`
  - §26 O2：O1 objects/relations -> nodes / relationships / triples；
  - §27 UID：只由稳定类型 + canonical identity 生成（`uid_for`），
    普通属性不影响 UID；
  - §28 `compat_raw`：`intermediate_model.compat_raw()` 与现有 raw 契约
    （entities/relations 字段形态）一致；
  - `validate`：uid 唯一、关系端点存在（AC-7 代理）。
- ✅ `split_extractor` 重构：O1 中间模型先构建，raw / graph / triples 统一
  经 GraphAssembler 生成，输出契约与重构前完全一致（既有测试全部通过）。
- ✅ 测试：`../tests/test_graph_assembler.py`（无需 openai / pytest），6 个用例。
- ✅ 验收命令：`python knowledge_extract_v2/benchmark/step10.py`——7 个用例
  gold 桩：实体/关系与 gold 一致，返回契约保留 legacy 键并附带新键，
  graph 无悬空/重复 uid、uid 可复算、triples 与 relationships 一一对应，
  无 UNKNOWN_* 告警（AC-1/AC-7）。

## Phase 1（Step 1-10）总览

| Step | 交付 | 验收命令 |
| --- | --- | --- |
| 1 | 基准集（7 用例 + eval） | `benchmark/eval.py check` |
| 2 | Ontology Compiler + TargetScope + Schema QA | `benchmark/step2.py --strict` |
| 3 | ChunkContext + Evidence（L1/L2） | `benchmark/step3.py` |
| 4 | Entity/Relation 两段式拆分 | `benchmark/step4.py` |
| 5 | Standard Strategy | `benchmark/step5.py` |
| 6 | OfficialDocument Strategy | `benchmark/step6.py` |
| 7 | Composite Element（§21/AC-6） | `benchmark/step7.py` |
| 8 | Mapping Scorer（§17-§19） | `benchmark/step8.py` |
| 9 | Coverage Auditor（§22-§23/AC-8） | `benchmark/step9.py` |
| 10 | GraphAssembler + Compat（§26-§28） | `benchmark/step10.py` |

全部 10 个 Step 的测试与验收均通过（9 个测试文件，116 个用例；
基准集自检 7 用例）。按 §37 约定，Step 2-10 均未改动 legacy
`extract_v2()` 的 LLM 流程；`extract_split()` 提供新的两段式 Harness 入口，
保留 raw/graph/warnings/attempts/profile/error_code 兼容契约。
