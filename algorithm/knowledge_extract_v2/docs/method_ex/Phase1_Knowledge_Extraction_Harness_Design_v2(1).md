# Phase 1：Ontology-Driven Knowledge Extraction Harness 详细设计方案

> 文档版本：v2.0  
> 阶段定位：**单次执行（Single-Run）、无自主 Loop 的 Knowledge Extraction Harness**  
> 目标：在现有 `ontology + text/chunks -> raw + graph` 契约基础上，将“单 Prompt + LLM + Schema 校验”升级为**本体驱动、特性驱动、证据可追溯、覆盖可评估、场景可扩展**的知识抽取执行框架。  
> 第二阶段再在本方案稳定接口之上引入 Loop Engineering。  

---

## 0. 依据材料与设计边界

本方案直接依据以下材料设计：

1. `专题需求-基于大模型和本体的知识图谱构建需求.docx`
2. `标准本体模型.md`
3. `公文智能问答场景-元模型.md`
4. 当前项目已有的 `extract_v2()`、`OntologySchema`、`graph_utils`、LLM scheduler 等实现。

需求文档明确要求：

- 在给定领域本体前提下，从非结构化文本中识别**本体指定范围**的知识元素及关系；
- 支持指定单类、多个类及其子类；
- 支持本体、实体、属性、术语、简单规则、复合规则、事件、流程、故障树等不同知识元素；
- 抽取逻辑应建立在类的**属性特性、关系特性和上下文特性**之上；
- 支持复合元素的组成关系；
- 输出先形成**中间对象模型**，再映射为 Graph / Triples；
- 进行本体一致性、完整性和质量评估；
- 输入可能来自 Word、可编辑 PDF、图片 PDF、xlsx、csv，并可能包含图片和表格；
- 最终研发指标要求知识图谱自动构建正确实现，统一抽取准确率不低于 85%。

Phase 1 的职责是：

> **把“一次知识抽取运行”工程化、结构化、可验证。**

Phase 1 明确不做：

- 不做开放式 Agent；
- 不做基于 Coverage Gap 的自主多轮循环；
- 不做无限 retry；
- 不做算法自动改写；
- 不做 Prompt 自动进化；
- 不做多 Agent 辩论；
- 不直接写 Neo4j；
- 不以 LLM 自我评价代替确定性验证。

---

# 1. 需求到架构的总体映射

需求文档使用 ICOM 描述知识结构化统一抽取。本 Harness 直接按照 ICOM 落地。

| ICOM/需求 | 需求含义 | Phase 1 设计组件 |
|---|---|---|
| I1 | 一个或多个非结构化自然语言文档 | `InputAdapter` + `DocumentAnalyzer` |
| I2 | 指定本体类型及范围 | `OntologyCompiler` + `TargetScopeResolver` |
| C1 | 仅允许本体已定义类型及子类 | `SchemaValidator` |
| C2 | 单类 / 多类 / 子类范围抽取 | `TargetScopeResolver` |
| C3 | 复合规则、流程、故障树等复合元素 | `CompositeElementHandler` |
| C4 | 特性识别与匹配，覆盖本质/关系/上下文特性 | `FeatureSpec` + `FeatureExtractor` + `MappingScorer` |
| C5 | 完整性、一致性、质量阈值 | `CoverageAuditor` + `QualityEvaluator` |
| O1 | 中间对象模型 JSON | `CandidatePool` + `IntermediateObjectModel` |
| O2 | Graph / Triples | `GraphAssembler` |
| M1 | 特征识别、候选实例、特性值抽取 | `DeterministicExtractor` + `LLMCandidateExtractor` |
| M2 | 本体映射、相似度、动态阈值、校验 | `MappingScorer` + Validation Stack |
| M3 | 中间模型映射到知识图谱 | `GraphAssembler` |
| 数据格式 | Word/PDF/扫描件/xlsx/csv/图片/表格 | 上游 `InputAdapter` 接口 |
| ≥85% | 统一抽取准确率验收 | `EvaluationSuite`，指标口径需冻结 |

---

# 2. 当前实现的问题重新定义

当前 `extract_v2()` 已经具备：

```text
Ontology parsing
Target type checking
Document profile detection
Prompt construction
LLM generation
JSON structured-output fallback
JSON retry
Ontology normalization
Ontology schema validation
Graph conversion
```

这些能力应保留，但它们目前仍然组合成：

```text
Ontology + Text
      ↓
一个综合 Prompt
      ↓
LLM 同时发现 Entity + Property + Relation
      ↓
Schema Normalize
      ↓
Schema Validate
      ↓
Graph
```

核心缺陷不是“Prompt 不够严格”，而是：

### 2.1 发现能力和校验能力不对称

现有 validator 可以回答：

```text
“模型输出的东西是否合法？”
```

但无法可靠回答：

```text
“原文中应该抽的东西是不是漏掉了？”
```

### 2.2 Prompt 承担了过多确定性规则

例如：

- 条款层级；
- 标识符生成；
- 标准编号；
- 数值/单位；
- relation endpoint 类型；
- 本体属性值域；

这些应优先由程序完成。

### 2.3 Entity 和 Relation 一次生成

关系必须同步生成端点，增加：

- 悬空 relation；
- 端点别名不一致；
- 同实体重复；
- relation recall 下降。

### 2.4 没有真正的中间对象模型

当前 `raw` 更接近 LLM 最终答案，而需求要求 O1 是**基于本体的中间对象模型**。因此必须增加 Candidate / Evidence / Mapping State。

### 2.5 缺乏特性级匹配

需求明确提出：

> 抽取 = 特征识别 + 匹配过程。

因此 Harness 必须把本体类的：

```text
属性特性
关系特性
上下文特性
```

编译成可执行的 FeatureSpec，而不是只把 schema 塞进 Prompt。

---

# 3. 最重要的设计修正：禁止专项 Prompt 与运行时本体漂移

当前专项 Prompt 中包含较多写死的 schema 名称。

但实际提供的标准本体中，例如：

```text
行动类：ActionClass
特性关系：hasCharacteristic
约束关系：imposesConstraint / constrainsCharacteristic
约束单位属性：unit
```

而已有抽取 Prompt 曾出现类似：

```text
Action
hasProperty
hasConstraint
measurementUnit
describedIn
```

如果运行时本体未声明这些标识，即使领域语义合理，也属于 schema drift。

因此 Phase 1 规定：

> **任何实体类型、属性名、关系名、方向和枚举值，都必须来自运行时 `OntologySchema` 或显式的 Ontology Extension；Document Strategy 不能偷偷增加 schema。**

专项规则可以写：

```text
“识别数值区间”
“识别试验动作”
“识别执行主体”
```

但最终映射名称必须通过：

```python
schema.resolve_class(...)
schema.resolve_property(...)
schema.resolve_relation(...)
```

获得。

如果本体没有对应 schema：

```text
不生成该字段
+
产生 SCHEMA_CAPABILITY_GAP
```

而不是发明新的属性或关系。

---

# 4. Phase 1 总体架构

```text
                         INPUT
                           │
                           ▼
┌───────────────────────────────────────────────────────────────┐
│ 1. Input Adapter Contract                                     │
│    Word / PDF / Scan / XLSX / CSV → normalized blocks        │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ 2. Document Analyzer                                          │
│    document / section / clause / paragraph / table / row      │
│    + source location                                          │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ 3. Ontology Compiler                                          │
│    classes + inheritance + properties + relations             │
│    + cardinality + feature metadata → ExtractionPlan          │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ 4. Target Scope Resolver                                      │
│    one type / multiple types / include subclasses             │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ 5. Document Strategy                                          │
│    Standard / OfficialDocument / Generic                      │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ 6. Deterministic Extraction                                   │
│    structure / ids / literals / known patterns                │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ 7. LLM Entity & Feature Candidate Extraction                  │
│    LLM proposes candidates, never final graph                 │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ 8. Evidence Verification                                      │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ 9. Mapping Scorer + Entity Resolution                         │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ 10. Relation Extraction                                      │
│     deterministic + candidate-pair semantic classification   │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ 11. Composite Element Assembly                               │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ 12. Coverage Auditor + Validation Stack                      │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ 13. Intermediate Object Model                                │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ 14. Graph Assembler                                           │
│     nodes / relationships / triples                           │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
                            RESULT
```

这仍然是**单次 Run**。

`CoverageAuditor` 只产生 gap，不自动启动新一轮抽取。

---

# 5. 输入层：Input Adapter Contract

需求数据包括：

```text
Word
可编辑 PDF
图片型 PDF
xlsx
csv
图片
表格
```

Phase 1 不要求 Knowledge Extraction Harness 自己实现所有 OCR/Office Parser，但必须定义统一输入契约。

```python
@dataclass
class SourceBlock:
    block_id: str
    source_file_id: str

    block_type: str
    text: str

    page_no: int | None
    sheet_name: str | None
    row_index: int | None

    bbox: tuple[float, float, float, float] | None
    metadata: dict[str, Any]
```

`block_type`：

```text
title
paragraph
table
table_row
table_cell
image_caption
list_item
header
footer
```

Harness 的输入边界：

```python
harness.run(
    ontology=ontology,
    blocks=source_blocks,
    target_types=...
)
```

保留兼容接口：

```python
harness.run(text=plain_text, ...)
```

但内部统一转成 `SourceBlock`。

---

# 6. Document Analyzer

## 6.1 统一文档结构

```python
@dataclass
class DocumentSpan:
    span_id: str
    source_block_ids: list[str]

    span_type: str

    text: str
    normalized_text: str

    number: str | None
    title: str | None

    parent_id: str | None
    children_ids: list[str]

    location: SourceLocation
```

目的：

- 原文定位；
- 结构关系生成；
- Evidence；
- 上下文窗口；
- 防止 chunk 切断关系；
- Phase 2 局部任务定位。

---

# 7. Ontology Compiler

## 7.1 输入

运行时本体是唯一 schema。

Compiler 解析：

```text
Class
Inheritance
DataProperty
ObjectProperty / Relation
Domain
Range
Cardinality
Identity
Enum / datatype
Ontology annotations
```

## 7.2 输出 ExtractionPlan

```python
@dataclass
class ExtractionPlan:
    ontology_id: str

    allowed_types: set[str]
    class_specs: dict[str, ClassSpec]

    relation_specs: dict[str, RelationSpec]
    identity_specs: dict[str, IdentitySpec]

    target_scope: TargetScope

    feature_specs: dict[str, list[FeatureSpec]]
    composite_specs: dict[str, CompositeSpec]

    document_strategy: str

    schema_warnings: list[SchemaWarning]
```

---

# 8. 特性模型：FeatureSpec

为了落实需求中的：

> “抽取 = 特征识别 + 匹配过程”

每个类编译出 FeatureSpec。

```python
@dataclass
class FeatureSpec:
    feature_id: str

    owner_type: str
    kind: str

    identifier: str

    datatype: str | None

    required: bool | None
    multi_value: bool | None
    complete_coverage: bool | None
    context_inferable: bool | None

    lexical_patterns: list[str]
    structural_patterns: list[str]
    relation_signature: RelationSpec | None

    source: str
```

`kind`：

```text
intrinsic
relation
context
```

如果本体没有“必选/可选、是否多值、是否可推断”等元数据：

> 不得自行猜测。

设置：

```python
required = None
multi_value = None
context_inferable = None
```

并在 `OntologyQualityReport` 中提示本体元信息不完整。

---

# 9. Target Scope Resolver

必须满足：

```text
仅抽取一个类型
抽取多个类型
自动包含其本体子类
全文覆盖
```

统一结构：

```python
@dataclass
class TargetScope:
    requested_types: list[str]
    expanded_types: set[str]

    include_subclasses: bool
    unknown_content_policy: str
```

例如：

```python
TargetScope(
    requested_types=["Rule"],
    expanded_types={
        "Rule",
        "SimpleRule",
        "CompositeRule"
    },
    include_subclasses=True,
)
```

### 未分类内容策略

需求允许：

```text
忽略
或暂存未分类 Element
```

Phase 1 配置：

```text
unknown_content_policy = ignore | retain_unclassified
```

默认：

```text
ignore
```

只有本体确实声明通用 `Element` 且调用方允许时，才使用 `retain_unclassified`。

禁止：

> 把无法映射的内容强行归到一个已定义业务类。

---

# 10. Document Strategy Registry

```python
STRATEGY_REGISTRY = {
    "standard": StandardExtractionStrategy(),
    "official_document": OfficialDocumentExtractionStrategy(),
    "generic": GenericOntologyExtractionStrategy(),
}
```

Strategy 只定义：

```text
文档如何分段
哪些结构可确定性识别
哪些特性有典型表现
如何做 coverage audit
```

Strategy 不定义运行时 ontology 没有的 schema 元素。

---

# 11. Strategy A：标准/规范文档

提供的标准本体覆盖：

```text
Standard
StandardizationObject
Stakeholder
Organization
Individual
DomainCategory
ContentElement
StructuralElement
InformationUnit
InformationForm
Object
Property
Constraint
ActionClass
ExternalResource
StandardizationProcess
```

以及标准元数据、层次、条款、对象、特性、约束和 34 类关系。

因此 Phase 1 将 Standard Strategy 设计为首批重点策略。

---

## 11.1 Standard Deterministic Extraction

优先本地完成：

### 文档元数据

若原文明示：

```text
standardNumber
documentName
issuedDate
effectiveDate
status
languageVersion
```

可由 pattern/parser 抽取并经过 datatype 校验。

### StructuralElement

解析：

```text
章
条
子条
段
列项
```

并生成：

```text
hasStructuralElement
hasSubClause
```

前提是这些 relation 在运行时本体存在。

### InformationUnit

按结构生成稳定 span / unit id。

### 分类代码

ICS / CCS 等结构化代码优先用 pattern。

### ExternalResource Literal

标准号、法规号等可先生成引用候选，再交由语义阶段判断引用关系类型。

---

## 11.2 Standard Semantic Extraction

LLM 重点处理：

```text
StandardizationObject
Object
Property
Constraint
ActionClass
ExternalResource
Stakeholder
复杂业务关系
```

而不是重新生成章节层级。

---

## 11.3 标准场景关系策略

本体声明的关系如：

```text
hasStructuralElement
hasClause
hasSubClause
defines
usesTerm
referencesClause
involvesObject
specifiesCharacteristic
hasCharacteristic
imposesConstraint
constrainsObject
constrainsCharacteristic
describesAction
referencesExternalResource
```

关系抽取采用：

```text
已确认实体
+
运行时 RelationSpec
+
局部 Evidence
        ↓
Candidate Pair Generation
        ↓
Rule / LLM Classification
```

而非开放式生成 relation name。

---

## 11.4 Standard Coverage Rules

从本体反推覆盖检查：

### Structural Coverage

所有已解析结构单元均应：

```text
存在对应 StructuralElement
+
关系端点可达
```

### Constraint Coverage

检测：

```text
不低于
不超过
至少
至多
范围
不得
应
应符合
```

若 ontology 允许 `Constraint`，但对应 span 无约束 candidate，则产生 gap。

### Action Coverage

检测：

```text
检验
试验
测量
检查
执行
```

若 ontology 允许 `ActionClass`，但缺失 Action candidate，则产生 gap。

### Reference Coverage

检测标准号、章条引用、法规/专利/文献引用，并检查对应关系候选。

### Characteristic Coverage

当条款明显描述对象特性时，检查：

```text
Object
Property
hasCharacteristic / specifiesCharacteristic
```

的结构是否完整。

---

# 12. Strategy B：公文

提供的公文本体以：

```text
OfficialDocument
DocumentUnit
OperationalAgent
OperationalObject
OperationalActivity
OperationalInformation
OperationalExchange
OperationalConstraint
```

为核心。

其中：

- `DocumentUnit` 明确承担原文内容和原文位置，是天然 Evidence Anchor；
- `OperationalActivity` 具有动作、执行主体、作用对象等运行语义；
- `OperationalExchange` 表达报送、审批、反馈、协同、监督、通知、备案；
- `OperationalConstraint` 表达时间、权限、安全、流程、质量、合规约束。

这非常适合 Evidence-grounded Harness。

---

## 12.1 OfficialDocument Deterministic Extraction

公文头部元数据优先结构化抽取：

```text
收文号
文件标题
来文单位
来文字号
联系电话
密级
缓急
收文日期
收文部门
主办部门
主送
抄送
拟办意见
领导批示
```

这些字段只在 runtime ontology 中存在时输出。

---

## 12.2 DocumentUnit 必须先建立

DocumentUnit 属性包括：

```text
unitId
unitType
unitTitle
unitOrder
originalText
location
```

因此公文抽取必须：

```text
先结构化 DocumentUnit
      ↓
再抽 operational knowledge
```

所有业务知识 Candidate 都通过 `span_id -> DocumentUnit` 保留 Evidence。

---

## 12.3 Operational Activity

重点识别：

```text
建设
管理
评估
报送
监督
实施
试点
完善
推进
加强
组织
```

形成：

```text
OperationalActivity
ACTS_ON
PERFORMED_BY
PRECEDES
USES_INFORMATION
PRODUCES
```

仅在本体签名允许时建立关系。

---

## 12.4 Operational Exchange

提取：

```text
sourceAgent
targetAgent
EXCHANGES_INFORMATION
SUPPORTS_ACTIVITY
```

由于本体中 sourceAgent / targetAgent 是 1→1：

> 在最终 accepted exchange 中，两个端点是强校验项。

若缺少端点：

```text
不应静默提交为完整 Exchange
```

而应标记：

```text
INCOMPLETE_RELATION_FEATURE
```

---

## 12.5 Operational Constraint

识别：

```text
时间约束
权限约束
安全约束
流程约束
质量约束
合规约束
```

以及强制程度：

```text
必须
应当
原则上
鼓励
禁止
```

并建立：

```text
CONSTRAINS_ACTIVITY
CONSTRAINS_AGENT
CONSTRAINS_OBJECT
```

---

## 12.6 公文 Evidence Coverage

本体为多个运行类声明：

```text
EVIDENCED_BY -> DocumentUnit
```

因此可以直接形成强覆盖指标：

```text
OperationalAgent evidence coverage
OperationalObject evidence coverage
OperationalActivity evidence coverage
OperationalInformation evidence coverage
OperationalExchange evidence coverage
OperationalConstraint evidence coverage
```

目标：

```text
accepted operational instance 的 EVIDENCED_BY coverage = 100%
```

---

# 13. 当前本体质量检查也纳入 Harness

Ontology Compiler 不仅“解析本体”，还应进行 Schema QA。

根据当前提供模型，至少要支持以下检查类型：

```text
关系端点引用了未声明类型
类重复定义
属性名称重复但 datatype 不一致
domain/range 缺失
identity 缺失
cardinality 缺失
enum 描述不规范
annotation 与属性名语义冲突
```

例如当前公文模型中存在：

```text
OfficialDocument -> 公文关系
```

但当前文件未见对应 `公文关系` 类定义。

Harness 不应猜测该类，而应产生：

```text
UNRESOLVED_RELATION_ENDPOINT_TYPE
```

并允许：

```text
strict schema compile -> 阻止该 relation
permissive schema compile -> relation unavailable + warning
```

另外当前 `OperationalObject` 的 `domain` / `description` 注释语义存在交叉描述现象。

对此：

> 保留 ontology 原文，不自动“纠正”，仅产生 ontology QA warning。

---

# 14. Candidate Model：真正的中间对象基础

## 14.1 EntityCandidate

```python
@dataclass
class EntityCandidate:
    candidate_id: str

    proposed_type: str
    canonical_type: str | None

    name: str
    properties: dict[str, Any]

    evidence: list["Evidence"]

    source: str
    extraction_pass: str

    mapping_score: float | None
    mapping_status: str

    warnings: list[str]
```

---

## 14.2 RelationCandidate

```python
@dataclass
class RelationCandidate:
    candidate_id: str

    from_candidate_id: str
    to_candidate_id: str

    proposed_relation: str
    canonical_relation: str | None

    evidence: list["Evidence"]

    mapping_score: float | None
    mapping_status: str
```

---

## 14.3 Evidence

```python
@dataclass
class Evidence:
    span_id: str
    source_block_ids: list[str]

    quote: str

    start_char: int | None
    end_char: int | None

    evidence_type: str
```

`evidence_type`：

```text
explicit
structural
contextual
```

---

# 15. LLM Extraction：由 Graph Generation 改为 Candidate Proposal

## 15.1 第一次 LLM Pass：Entity + Feature Candidate

输入只包含：

```text
当前 span
必要局部上下文
允许类
每类 FeatureSpec
当前 deterministic entities
```

输出：

```json
{
  "entities": [
    {
      "type": "runtime ontology identifier",
      "name": "...",
      "properties": {},
      "evidence": {
        "span_id": "...",
        "quote": "..."
      }
    }
  ]
}
```

禁止在此阶段开放式生成 relations。

---

## 15.2 第二次 LLM Pass：Relation Candidate

输入：

```text
原文 span
已 accepted / resolved entity catalog
允许 relation signatures
```

输出：

```json
{
  "relations": [
    {
      "from_id": "entity-...",
      "rel_type": "...",
      "to_id": "entity-...",
      "evidence": {
        "span_id": "...",
        "quote": "..."
      }
    }
  ]
}
```

relation endpoint 使用稳定 candidate/entity id，不再依赖自由字符串 name 对齐。

---

# 16. Evidence Verifier

Evidence Verification 分四层。

## L1 Source Existence

```text
span_id 是否存在
source_block 是否存在
```

## L2 Literal Match

```text
evidence quote 是否能在 span 中定位
```

## L3 Property Support

数值、日期、编号、枚举等 literal property 检查：

```text
值是否来自 evidence
是否可按 datatype normalize
```

## L4 Contextual Support

只有 ontology 的 FeatureSpec 明确允许上下文推断时，才接受 contextual evidence。

否则：

```text
LLM 语义上合理
≠
可提交事实
```

---

# 17. Mapping Scorer

需求提出：

```text
类相似度 = 属性匹配 + 关系匹配 + 上下文相关
```

并给出参考阈值：

```text
>= 0.85     确定类归属
0.70-0.85   低置信度
< 0.70      拒绝映射
```

需求没有规定三部分的具体权重。

因此 Phase 1 设计：

```python
score = (
    w_attr * attribute_score
    + w_rel * relation_score
    + w_ctx * context_score
)
```

其中：

```text
w_attr + w_rel + w_ctx = 1
```

权重由：

```text
document strategy
ontology type
evaluation calibration
```

配置，不能在方案中假定固定比例。

默认阈值可以采用需求给出的参考值：

```python
accepted_threshold = 0.85
low_confidence_threshold = 0.70
```

但正式上线前必须通过 benchmark 校准。

---

# 18. 属性匹配评分

建议：

```text
required feature presence
optional feature coverage
datatype consistency
enum consistency
identity consistency
evidence support
```

如果 ontology 没声明 required：

> 不纳入 required penalty。

避免因为本体元信息缺失而错误惩罚 candidate。

---

# 19. 关系匹配评分

包括：

```text
relation identifier legality
source class legality
target class legality
cardinality
endpoint existence
local evidence
```

如果 ontology 有多重性：

```text
1
0..1
1..*
0..*
```

则直接作为结构 validator 使用，而不是仅作为 prompt 提示。

---

# 20. Entity Resolution

顺序：

```text
Identity Exact Match
      ↓
Normalized Identity Match
      ↓
Document-local Alias Rules
      ↓
Ontology Feature Match
      ↓
Optional Semantic Match
```

只有通过证据和类型约束后，才允许 merge。

不允许仅凭 LLM：

```text
“我觉得这两个实体是同一个”
```

直接合并。

---

# 21. CompositeElementHandler

需求明确包含：

```text
CompositeRule
Process
FaultTree
```

等复合元素。

Phase 1 设计通用 CompositeSpec：

```python
@dataclass
class CompositeSpec:
    composite_type: str
    member_type: str

    membership_relation: str
    combination_property: str | None

    allowed_combinations: set[str]
```

例如本体如果声明：

```text
CompositeRule hasSubRule Rule
CombinationType = AND | OR | SEQUENCE
```

则 Harness 必须：

1. 创建 simple member；
2. 创建 composite instance；
3. 建立 hasSubRule；
4. 保存 combination type；
5. 校验 member 数量和 relation endpoint。

不得把整个复合规则只保存成一段字符串。

---

# 22. Coverage Auditor

Phase 1 的 Coverage Auditor 不负责自动修复，只负责结构化发现 gap。

```python
@dataclass
class CoverageGap:
    gap_id: str

    span_id: str
    target_type: str | None

    gap_type: str
    severity: str

    evidence: str
    reason: str

    expected_features: list[str]
    expected_relations: list[str]
```

---

# 23. Coverage 类型

## 23.1 Type Coverage

指定 target type 后：

```text
文本中的明显候选模式
vs
最终 accepted instances
```

## 23.2 Feature Coverage

对一个 accepted instance：

```text
ontology 可观测特性
vs
实际已提取特性
```

注意：

> “缺失”只有在 ontology 明确标注 required / complete coverage 时才是硬错误，否则仅作为提示。

## 23.3 Relation Coverage

根据 ontology relation signatures 和文档结构，判断明显 relation candidate 是否缺失。

## 23.4 Evidence Coverage

accepted candidate 是否都有证据。

## 23.5 Composite Coverage

复合元素是否：

```text
有 member
有组成 relation
有 combination type（若 schema 要求）
```

---

# 24. Validation Stack

```text
L0 Ontology Compile Validation
         ↓
L1 JSON / Data Structure Validation
         ↓
L2 Type / Property / Datatype Validation
         ↓
L3 Relation Signature / Cardinality Validation
         ↓
L4 Identity / UID Validation
         ↓
L5 Evidence Validation
         ↓
L6 Coverage Validation
         ↓
L7 Semantic Mapping Validation
```

优先级：

```text
Code
>
Ontology
>
Rule
>
LLM semantic judge
```

---

# 25. 中间对象模型（O1）

需求明确要求先生成“中间对象模型”。

Phase 1 不再把 LLM raw JSON 直接视为 O1。

正式 O1：

```json
{
  "run_id": "ke-...",
  "ontology": {
    "ontology_id": "...",
    "target_types": ["..."],
    "expanded_types": ["..."]
  },
  "objects": [
    {
      "id": "obj-001",
      "type": "OperationalActivity",
      "identity": "...",
      "properties": {},
      "features": {
        "intrinsic": {},
        "relation": {},
        "context": {}
      },
      "evidence": [],
      "mapping": {
        "score": 0.91,
        "status": "accepted"
      }
    }
  ],
  "relations": [],
  "coverage_gaps": [],
  "warnings": []
}
```

这是真正的需求 O1。

---

# 26. Graph / Triples（O2）

O2 只由 accepted Intermediate Objects 映射。

```json
{
  "nodes": [
    {
      "uid": "...",
      "label": "OperationalActivity",
      "properties": {}
    }
  ],
  "relationships": [
    {
      "from_uid": "...",
      "to_uid": "...",
      "type": "ACTS_ON"
    }
  ]
}
```

可同时提供 triples：

```json
{
  "head": "...",
  "relation": "ACTS_ON",
  "tail": "..."
}
```

---

# 27. UID 原则

必须检查当前 `_entities_to_graph()`。

现有模式：

```python
node_uid = entity_uid(type, name, properties)
relation_uid = entity_uid(type, name, None)
```

如果 properties 参与 UID，会导致 endpoint 不一致。

Phase 1 强制：

> Graph node UID 只能由稳定类型 + canonical identity 生成。

```python
uid = stable_hash(
    canonical_type,
    canonical_identity
)
```

普通属性不得影响 UID。

---

# 28. Result Contract

```python
@dataclass
class ExtractionResult:
    run_id: str

    profile: ExtractionProfile

    intermediate_model: IntermediateObjectModel

    graph: dict[str, Any]
    triples: list[dict[str, Any]]

    coverage_gaps: list[CoverageGap]
    validation_warnings: list[ValidationWarning]
    ontology_warnings: list[SchemaWarning]

    quality: QualityReport
    trace: list[TraceEvent]
```

为了兼容现有调用：

```python
return {
    "raw": intermediate_model.compat_raw(),
    "graph": graph,
    "warnings": ...,
    "profile": ...,
    "attempts": ...,
    "coverage_gaps": ...,
    "quality": ...,
    "trace": ...
}
```

---

# 29. Trace

```python
@dataclass
class TraceEvent:
    event_id: str
    run_id: str

    stage: str
    status: str

    span_ids: list[str]
    candidate_ids: list[str]

    input_digest: str
    output_summary: dict

    elapsed_ms: int
    model_call_id: str | None
```

必须能回答：

```text
这个知识是谁抽的？
来自哪一段？
哪一步 normalize？
为什么归到这个类？
mapping score 是多少？
哪个 relation verifier 接受？
```

---

# 30. LLM Retry 的新边界

Phase 1 仍允许底层 retry，但只限：

```text
transport error
timeout
unsupported response_format
JSON syntax
schema formatting
```

不把：

```text
coverage gap
语义漏抽
低 recall
```

当作自动循环。

这些问题 Phase 1 只记录。

Phase 2 再通过 Loop Engineering 定向处理。

---

# 31. 当前 `_read_stream()` 建议修正

如果 backend 只返回：

```text
reasoning_content
```

而没有 final content：

不应将 reasoning token 当作抽取 JSON。

建议：

```text
NO_FINAL_CONTENT
→ bounded retry / fail
```

避免把模型内部推理文本误当正式结构化输出。

---

# 32. 目录设计

```text
extractor/algorithm/knowledge_extract/
│
├── api.py
├── compat.py
│
├── harness/
│   ├── harness.py
│   ├── context.py
│   ├── result.py
│   └── trace.py
│
├── input/
│   ├── models.py
│   └── adapter_contract.py
│
├── document/
│   ├── analyzer.py
│   ├── models.py
│   └── strategies/
│       ├── base.py
│       ├── standard.py
│       ├── official_document.py
│       └── generic.py
│
├── ontology/
│   ├── schema.py
│   ├── compiler.py
│   ├── plan.py
│   ├── features.py
│   └── qa.py
│
├── candidates/
│   ├── models.py
│   └── pool.py
│
├── extraction/
│   ├── deterministic.py
│   ├── entity_candidate.py
│   ├── relation_candidate.py
│   └── composite.py
│
├── verification/
│   ├── schema.py
│   ├── relation.py
│   ├── cardinality.py
│   ├── evidence.py
│   └── coverage.py
│
├── mapping/
│   ├── scorer.py
│   └── resolver.py
│
├── graph/
│   ├── intermediate.py
│   └── assembler.py
│
└── evaluation/
    ├── metrics.py
    ├── dataset.py
    └── regression.py
```

---

# 33. `extract_v2()` 迁移策略

不建议一次删除旧接口。

```python
def extract_v2(...):
    return get_default_harness().run_compat(...)
```

逐步迁移：

| 当前代码 | 新位置 |
|---|---|
| `detect_document_type` | `document/strategies/registry.py` |
| `build_extraction_prompt` | 拆为各 candidate extractor |
| `GENERIC_INSTRUCTION` | 精简为通用 Candidate 约束 |
| `DOCUMENT_INSTRUCTIONS` | 拆为 Strategy + Feature rules + Prompt hints |
| `OntologySchema.parse` | 保留并增强 |
| `normalize_extraction` | Validation Stack |
| `validate_extraction` | Validation Stack |
| `_entities_to_graph` | `graph/assembler.py` |
| `json_retries` | LLM transport layer |
| scheduler | 保留 |
| OpenAI client compatibility | 保留 |

---

# 34. 质量评估

需求给出类映射参考阈值：

```text
>= 0.85
0.70 - 0.85
< 0.70
```

同时最终研发指标提出：

```text
统一抽取准确率 >= 85%
```

两者不能混为一谈：

- `0.85 mapping threshold`：单 candidate 类归属参考阈值；
- `85% extraction accuracy`：系统级验收指标。

---

# 35. “统一抽取准确率”需要冻结口径

需求给出阈值，但没有给出严格公式。

正式验收前必须由项目双方冻结，例如：

```text
方案 A：
micro-F1(entity + property + relation)

方案 B：
weighted accuracy =
  w1 * entity_f1
+ w2 * property_accuracy
+ w3 * relation_f1
+ w4 * composite_accuracy
```

Phase 1 不应擅自把“85%”解释为某一个指标。

推荐同时报告：

```text
Entity Precision / Recall / F1
Property Exact / Normalized Accuracy
Relation Precision / Recall / F1
Composite Structure Accuracy
Evidence Coverage
Schema Validity
Coverage Proxy
```

并指定一个“统一抽取主指标”用于正式验收。

---

# 36. Phase 1 Acceptance Criteria

## AC-1：Schema Authority

```text
最终 graph 中未知 class = 0
最终 graph 中未知 property = 0
最终 graph 中未知 relation = 0
relation endpoint type mismatch = 0
```

## AC-2：Target Scope

测试：

```text
单类
父类 + 子类
多个类型
全文类型
```

所有结果必须严格落在 expanded target scope。

## AC-3：Intermediate Object Model

所有 accepted graph node / relation 必须能追溯到 O1。

## AC-4：Evidence

对需要证据的业务类：

```text
accepted instance evidence coverage = 100%
```

公文 operational classes 的 `EVIDENCED_BY` 完整度应达到 100%。

## AC-5：Standard Structure

标准文档结构：

```text
section / clause hierarchy precision = 100%
```

对规则可确定性识别的结构，不能依赖 LLM。

## AC-6：Composite Elements

测试 simple + composite rule：

```text
member extraction
membership relation
combination type
```

全部正确。

## AC-7：No Dangling Relations

```text
dangling relation = 0
```

## AC-8：Coverage Report

至少能够检测：

```text
明显规范性约束漏抽
数值边界不完整
Action/Activity 漏抽
引用漏抽
结构漏抽
复合成员缺失
Evidence 缺失
```

## AC-9：Ontology QA

对不完整 ontology：

```text
能报告
不编造
不崩溃
```

## AC-10：性能回归

相同模型和输入下：

```text
Phase 1 主指标不得低于旧 extract_v2 baseline
schema error 明显下降
coverage recall proxy 明显提高
```

## AC-11：研发主指标

固定 benchmark 上：

```text
统一抽取主指标 >= 85%
```

具体计算公式在验收前冻结。

---

# 37. Phase 1 推荐开发顺序

## Step 1：建立基准集

先冻结旧 `extract_v2()` baseline。

包含：

```text
标准
公文
术语
ontology class extraction
简单规则
复合规则
```

---

## Step 2：Ontology Compiler + TargetScope

先实现：

```text
class hierarchy
properties
relations
target expansion
schema QA
```

验收后再做 LLM 变更。

---

## Step 3：DocumentSpan + Evidence

所有抽取结果先拥有 source span。

---

## Step 4：Entity / Relation 拆分

从 one-shot：

```text
entities + relations
```

变成：

```text
entity candidate
→ resolve
→ relation candidate
```

---

## Step 5：Standard Strategy

实现：

```text
structure
metadata
constraint/action/reference coverage
```

---

## Step 6：OfficialDocument Strategy

实现：

```text
DocumentUnit
OperationalAgent
OperationalObject
OperationalActivity
OperationalInformation
OperationalExchange
OperationalConstraint
EVIDENCED_BY
```

---

## Step 7：Composite Element

优先满足需求文档中的 CompositeRule 示例。

---

## Step 8：Mapping Scorer

实现可配置的：

```text
attribute + relation + context
```

评分。

---

## Step 9：Coverage Auditor

只发现 gap，不自动修复。

---

## Step 10：GraphAssembler + Compat

保留现有 API。

---

# 38. Phase 1 完成定义

Phase 1 完成时，系统应从：

```text
一个 Prompt 生成一个 Graph
```

升级为：

```text
Ontology
  ↓
Executable ExtractionPlan

Document
  ↓
Structured Evidence Spans

Plan + Document
  ↓
Deterministic Knowledge
+
LLM Candidates
  ↓
Evidence Verification
  ↓
Feature Mapping
  ↓
Entity Resolution
  ↓
Relation Extraction
  ↓
Composite Assembly
  ↓
Coverage Audit
  ↓
Intermediate Object Model
  ↓
Graph / Triples
```

最关键的能力不是“LLM 多聪明”，而是：

> **每一条知识都知道自己属于哪个本体类型、由哪些本体特性支持、来自哪段原文、为什么被接受、有没有明显遗漏。**

---

# 39. 为 Phase 2 预留的稳定接口

Phase 1 必须确保以下能力均可独立调用：

```python
document_analyzer.analyze(...)
ontology_compiler.compile(...)
deterministic_extractor.extract(...)
entity_extractor.extract(...)
evidence_verifier.verify(...)
mapping_scorer.score(...)
entity_resolver.resolve(...)
relation_extractor.extract(...)
composite_handler.assemble(...)
coverage_auditor.audit(...)
validator.validate(...)
graph_assembler.assemble(...)
```

Phase 2 不应复制这些逻辑。

它只通过这些接口进行：

```text
Observe
Plan
Execute
Verify
Commit
Stop
```

---

# 40. Phase 1 一句话定义

> **Phase 1 Knowledge Extraction Harness 是一个以运行时本体为唯一 Schema Authority，以 FeatureSpec 为抽取依据，以 Candidate 为中间对象，以 Evidence 为事实基础，以 Coverage 为完整性检查，并将结果稳定映射为中间对象模型和 Graph/Triples 的单次知识抽取执行框架。**
