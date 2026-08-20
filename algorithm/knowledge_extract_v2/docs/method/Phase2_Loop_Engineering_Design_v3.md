# Phase 2：Knowledge Extraction Harness + Loop Engineering 详细设计方案

> 文档版本：v3.0  
> 前置条件：Phase 1 Knowledge Extraction Harness 已完成并通过验收；输入仍为上游已经解析/切片后的 `text/chunks`。  
> 阶段定位：在 Phase 1 的单次抽取能力之上，引入**显式状态、质量反馈、定向任务、Verified Delta、停止规则和离线优化闭环**。  
> 核心原则：Loop 不等于“多跑几次 Prompt”，而是受本体、证据、质量指标和预算约束的工程循环。  

---

## 0. Phase 2 目标

Phase 1（基于上游已解析 `text/chunks`）可以回答：

> “一次抽取怎样尽可能做正确，并指出哪里可能有问题？”

Phase 2 进一步回答：

> “发现缺口后，系统下一步应该做什么？  
> 修复结果是否真的更好？  
> 哪些变化可以提交？  
> 什么时候应该停止？”

因此 Phase 2 的核心不是增加 LLM 调用次数，而是：

```text
State
  ↓
Observe
  ↓
Assess
  ↓
Plan
  ↓
Execute
  ↓
Verify
  ↓
Commit Delta
  ↓
Measure Progress
  ↓
Stop / Continue
```


## 0.1 输入边界保持不变

Phase 2 不扩大输入职责。

```text
上游文档解析 / OCR / 多模态 / 切片
          ↓
    parsed text/chunks
          ↓
     Phase 1 Harness
          ↓
  Initial Result + Gaps
          ↓
    Phase 2 Loop
```

Loop 只围绕：

```text
已有 chunks
已有 Evidence
已有 Candidate / Intermediate Model
已有 Coverage Gap / Warning
```

进行局部重抽取、关系补全、证据恢复和映射修复。

禁止 Worker：

```text
重新读取原始 PDF
重新 OCR
重新进行版面分析
自行改变原始 chunk 边界
```

如果某个 gap 的根因属于上游解析错误，则终止为：

```text
UPSTREAM_PARSE_GAP / NEEDS_REVIEW
```

由上游文档处理链路解决，而不是让 Extraction Loop 越界修复。

---

# 1. Phase 2 对需求文档的进一步落实

需求文档要求：

```text
完整性
一致性
动态阈值
特性覆盖
复合元素结构
中间对象模型
Graph / Triples
>=85% 系统验收
```

Phase 1 已能测量这些问题。

Phase 2 利用这些测量结果进行：

```text
Coverage Gap → Targeted Extraction
Low Confidence → Targeted Disambiguation
Evidence Gap → Evidence Recovery
Missing Relation → Relation Completion
Composite Gap → Member / Composition Repair
Schema Error → Schema-constrained Repair
```

因此：

> Phase 2 只处理 Phase 1 已能结构化表达的问题。

禁止用一个通用：

```text
“请重新检查并补充所有遗漏”
```

替代工程化任务。

---

# 2. Phase 2 总体架构

```text
                        START
                          │
                          ▼
        Phase 1 Harness Initial Run
        （输入：parsed text/chunks）
                          │
                          ▼
                    Initialize State
                          │
                          ▼
┌──────────────────────────────────────────────────────────────┐
│                      QUALITY LOOP                            │
│                                                              │
│  Observe Current State                                       │
│           │                                                  │
│           ▼                                                  │
│  Quality Assessor                                            │
│           │                                                  │
│           ▼                                                  │
│  Gap / Warning / Low-confidence Queue                        │
│           │                                                  │
│           ▼                                                  │
│  Planner                                                     │
│           │                                                  │
│           ▼                                                  │
│  Bounded Task Batch                                          │
│           │                                                  │
│           ▼                                                  │
│  Worker                                                      │
│           │                                                  │
│           ▼                                                  │
│  Extraction Delta                                            │
│           │                                                  │
│           ▼                                                  │
│  Delta Verifier                                              │
│       ┌───┴────┐                                             │
│       ▼        ▼                                             │
│    Accept    Reject                                           │
│       │        │                                             │
│       └───┬────┘                                             │
│           ▼                                                  │
│  Commit Verified Delta                                       │
│           │                                                  │
│           ▼                                                  │
│  Re-audit / Re-score                                         │
│           │                                                  │
│           ▼                                                  │
│  Progress + Stop Controller                                  │
│       ┌───┴────────────┐                                     │
│       ▼                ▼                                     │
│     STOP            CONTINUE ───────────────────────┐         │
│       │                                              │         │
└───────┼──────────────────────────────────────────────┘         │
        ▼                                                        │
 Terminal Result                                                 │
```

---

# 3. 不允许的“伪 Loop Engineering”

以下都不算本方案中的 Loop Engineering：

```python
for i in range(5):
    result = llm("再检查一遍")
```

或：

```text
Extract
→ Judge LLM
→ Rewrite All
→ Judge LLM
```

原因：

- 没有显式 state；
- 没有 gap 类型；
- 没有局部 task；
- 没有 delta；
- 已正确内容可能被改坏；
- 无法解释为什么继续；
- 无法可靠判断 progress；
- 容易 token 无限增长。

---

# 4. LoopState

Loop 的 memory 必须是结构化状态，而不是聊天历史。

```python
@dataclass
class ExtractionLoopState:
    run_id: str

    round_no: int
    terminal_state: str | None

    context: ExtractionContext
    plan: ExtractionPlan

    intermediate_model: IntermediateObjectModel

    accepted_entities: dict[str, EntityObject]
    accepted_relations: dict[str, RelationObject]

    candidate_pool: CandidatePool

    coverage_gaps: dict[str, CoverageGap]
    validation_warnings: dict[str, ValidationWarning]
    ontology_warnings: dict[str, SchemaWarning]

    low_confidence_objects: set[str]

    quality: QualityReport

    rejected_delta_ids: list[str]

    llm_calls: int
    token_budget_used: int
    elapsed_seconds: float

    no_progress_rounds: int
    oscillation_keys: dict[str, int]

    decision_history: list[DecisionEvent]
    trace: list[TraceEvent]
```

---

# 5. Loop State 不保存什么

不把以下内容当成权威状态：

```text
模型自己写的总结
模型“记得”的上一轮判断
自然语言 conversation history
```

conversation 可以辅助 LLM，但 authoritative state 必须来自：

```text
IntermediateObjectModel
CandidatePool
CoverageGap
ValidationWarning
QualityReport
DecisionHistory
```

---

# 6. Quality Assessor

每轮开始先重新评估：

```python
assessment = assessor.assess(state)
```

输出：

```python
@dataclass
class LoopAssessment:
    schema_errors: list[str]
    evidence_gaps: list[str]
    coverage_gaps: list[str]
    relation_gaps: list[str]
    composite_gaps: list[str]
    low_confidence_ids: list[str]

    quality_vector: dict[str, float]

    high_priority_gap_count: int
```

---

# 7. Quality Vector

建议运行时保留多维指标，而不是压成一个“总分”。

```text
schema_validity
evidence_coverage
type_mapping_confidence
feature_coverage
relation_completeness
structural_coverage
composite_completeness
document_specific_coverage
```

例如公文：

```text
DocumentUnit coverage
OperationalActivity evidence coverage
OperationalExchange endpoint completeness
Constraint target completeness
```

例如标准：

```text
StructuralElement coverage
Constraint trigger coverage
Characteristic coverage
Action coverage
Reference coverage
```

---

# 8. Planner

Planner 不是自由聊天 Agent。

Planner 输入：

```text
LoopAssessment
ExtractionPlan
Remaining Budget
Decision History
```

输出严格的 `ExtractionTask`。

```python
@dataclass
class ExtractionTask:
    task_id: str
    task_type: str

    gap_ids: list[str]

    span_ids: list[str]
    object_ids: list[str]

    allowed_types: list[str]
    allowed_relations: list[str]

    goal: str
    success_criteria: dict

    priority: int
    max_llm_calls: int
```

---

# 9. Task Types

首批建议：

```text
MISSING_ENTITY
MISSING_FEATURE
MISSING_RELATION
MISSING_COMPOSITE_MEMBER
MISSING_COMPOSITION_RELATION
LOW_CONFIDENCE_MAPPING
EVIDENCE_RECOVERY
RELATION_ENDPOINT_REPAIR
CARDINALITY_REPAIR
IDENTITY_RESOLUTION
CONSTRAINT_COMPLETION
ACTION_COMPLETION
REFERENCE_COMPLETION
OFFICIAL_ACTIVITY_COMPLETION
OFFICIAL_EXCHANGE_COMPLETION
```

---

# 10. Gap → Task 映射必须确定化

例如：

```text
CoverageGap:
missing_constraint_upper_bound
```

只能路由到：

```text
ConstraintCompletionWorker
```

而不是交给通用 Agent 决定所有事情。

例如：

```text
OperationalExchange targetAgent missing
```

路由：

```text
OfficialExchangeCompletionWorker
```

例如：

```text
CompositeRule has 2 member mentions
but only 1 hasSubRule
```

路由：

```text
CompositeMemberRepairWorker
```

---

# 11. Worker 设计

第一版不需要多 Agent。

Worker 是固定职责执行器：

```python
class Worker(Protocol):
    def execute(
        self,
        task: ExtractionTask,
        state: ExtractionLoopState,
    ) -> ExtractionDelta:
        ...
```

Worker 可以内部调用：

```text
rule parser
runtime ontology
LLM
local candidate search
existing graph read-only search
embedding matcher
```

但：

> Worker 不能直接修改 accepted graph。

---

# 12. 首批 Worker

```text
EntityCompletionWorker
FeatureCompletionWorker
RelationCompletionWorker
ConstraintCompletionWorker
CompositeRepairWorker
EvidenceRecoveryWorker
IdentityResolutionWorker
StandardReferenceWorker
OfficialActivityWorker
OfficialExchangeWorker
OfficialConstraintWorker
```

---

# 13. ExtractionDelta

Loop 的最重要设计：

> **每轮只产生 Delta，不重新生成整张 Graph。**

```python
@dataclass
class ExtractionDelta:
    delta_id: str
    task_id: str

    add_entities: list[EntityCandidate]
    update_entities: list[EntityPatch]
    remove_entity_ids: list[str]

    add_relations: list[RelationCandidate]
    remove_relation_ids: list[str]

    resolved_gap_ids: list[str]

    evidence: list[Evidence]

    rationale_code: str
```

---

# 14. 为什么必须 Delta

错误方式：

```text
Round 1: Graph A
Round 2: Graph B
Round 3: Graph C
```

可能发生：

```text
补好了一个漏抽
但删掉三个本来正确的实体
```

正确方式：

```text
Accepted Knowledge K_n
          +
Verified Delta Δ
          ↓
Accepted Knowledge K_(n+1)
```

只有 verifier 接受的变化才能 commit。

---

# 15. Delta Verifier

```python
verification = delta_verifier.verify(
    delta=delta,
    state=state,
    task=task,
)
```

输出：

```python
@dataclass
class DeltaVerification:
    accepted: bool

    accepted_operations: list[str]
    rejected_operations: list[str]

    new_errors: list[str]
    resolved_errors: list[str]

    quality_before: dict
    quality_after_preview: dict

    reason_codes: list[str]
```

---

# 16. Delta Verification Stack

每个 Delta 必须通过：

```text
V1 Schema
  ↓
V2 Target Scope
  ↓
V3 Identity / UID
  ↓
V4 Relation Signature / Cardinality
  ↓
V5 Evidence
  ↓
V6 Semantic Mapping
  ↓
V7 Regression Guard
```

`Regression Guard` 检查：

```text
是否引入新的 schema error
是否降低已确认 evidence coverage
是否破坏 deterministic structure
是否删除 high-confidence accepted knowledge
```

---

# 17. 部分接受 Delta

不要求一个 Delta 全部通过或全部拒绝。

例如 Worker 输出：

```text
+ Constraint A      ✓
+ Relation R1       ✓
+ Relation R2       ✗ endpoint mismatch
```

可以：

```text
commit A + R1
reject R2
```

减少因局部错误导致整批重做。

---

# 18. Standard Document Loop

标准文档首批 Loop 路径：

```text
Coverage Audit
    │
    ├─ missing StructuralElement
    │      → deterministic repair
    │
    ├─ missing Constraint
    │      → ConstraintCompletionWorker
    │
    ├─ incomplete min/max
    │      → ConstraintCompletionWorker
    │
    ├─ missing Property
    │      → FeatureCompletionWorker
    │
    ├─ missing ActionClass
    │      → ActionCompletionWorker
    │
    ├─ missing internal clause reference
    │      → RelationCompletionWorker
    │
    └─ missing ExternalResource
           → StandardReferenceWorker
```

---

# 19. 公文 Loop

```text
Coverage Audit
    │
    ├─ activity trigger but no OperationalActivity
    │      → OfficialActivityWorker
    │
    ├─ activity exists but no PERFORMED_BY
    │      → RelationCompletionWorker
    │
    ├─ activity exists but no ACTS_ON
    │      → RelationCompletionWorker
    │
    ├─ report/approval/feedback phrase
    │  but no OperationalExchange
    │      → OfficialExchangeWorker
    │
    ├─ Exchange missing sourceAgent/targetAgent
    │      → OfficialExchangeCompletionWorker
    │
    ├─ mandatory phrase but no OperationalConstraint
    │      → OfficialConstraintWorker
    │
    └─ entity has no EVIDENCED_BY
           → EvidenceRecoveryWorker
```

---

# 20. 复合元素 Loop

以需求中的 CompositeRule 为例。

初始结果：

```text
SimpleRule R1
SimpleRule R2
CompositeRule CR1

hasSubRule(CR1, R1)

文本明确还有 R2
```

Coverage Auditor：

```text
MISSING_COMPOSITE_MEMBER_RELATION
```

Task：

```text
只验证 CR1 与 R2 是否存在 membership relation
```

Delta：

```text
+ hasSubRule(CR1, R2)
```

Verifier：

```text
relation in ontology?
endpoint types legal?
evidence supports?
duplicate?
```

通过才 commit。

---

# 21. 类映射低置信度 Loop

需求参考区间：

```text
>=0.85 accepted
0.70-0.85 low confidence
<0.70 rejected
```

Phase 2 对 0.70-0.85 可创建：

```text
LOW_CONFIDENCE_MAPPING
```

任务不要求模型自由重抽，而是提供：

```text
Candidate features
Candidate evidence
Top-k allowed ontology classes
FeatureSpec difference
Existing relations
```

要求进行受限判定。

最终仍由本地 MappingScorer / Validator 决定。

---

# 22. 不对 <0.70 Candidate 无限抢救

低于拒绝阈值的 candidate：

```text
默认 reject
```

只有以下情况可再开一次任务：

```text
coverage auditor 认为该 span 存在 high-priority target knowledge
```

避免：

> Loop 为一个错误候选无限寻找理由。

---

# 23. Evidence Recovery Loop

如果 candidate 语义合理但证据失配：

```text
Entity candidate
Evidence quote not found
```

任务：

```text
仅允许在原 span + 邻近结构范围中重新定位证据
```

禁止：

```text
扩大到整篇文档然后“找一个相似句”
```

如果仍无法定位：

```text
reject / downgrade
```

而不是凭 LLM 置信度保留。

---

# 24. Loop Controller

```python
class KnowledgeExtractionLoopController:

    def should_stop(
        self,
        state: ExtractionLoopState,
        assessment: LoopAssessment,
    ) -> TerminalDecision:
        ...

    def update_progress(...):
        ...

    def detect_oscillation(...):
        ...
```

---

# 25. Terminal States

```python
class TerminalState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    NO_PROGRESS = "no_progress"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INVALID_ONTOLOGY = "invalid_ontology"
    FAILED = "failed"
```

---

# 26. COMPLETE 不能由 LLM 声明

运行时 DONE 可以基于：

```python
complete = (
    schema_error_count == 0
    and dangling_relation_count == 0
    and high_severity_evidence_gap_count == 0
    and high_priority_coverage_gap_count == 0
    and deterministic_structure_errors == 0
)
```

再结合：

```text
quality proxy threshold
```

注意：

> Runtime 没有 Gold，因此不能用“85%真实抽取准确率”作为单篇文档的直接停止条件。

85% 是离线 benchmark 验收指标。

---

# 27. Hard Budget Stop

至少配置：

```text
max_rounds
max_llm_calls
max_tokens
max_runtime_seconds
max_tasks_per_round
max_tasks_per_span
```

例如配置结构：

```python
@dataclass
class LoopBudget:
    max_rounds: int = 4
    max_tasks_per_round: int = 8
    max_llm_calls: int = 20
    max_tasks_per_span: int = 2
```

具体默认值通过 benchmark 决定。

---

# 28. No Progress

定义 `quality_vector` 后计算变化：

```python
progress = compare_quality(
    before,
    after
)
```

如果：

```text
新增 accepted knowledge = 0
resolved high-priority gaps = 0
quality_vector 无实质改善
```

则：

```python
no_progress_rounds += 1
```

达到配置次数：

```text
NO_PROGRESS
```

---

# 29. Oscillation

记录关键 decision：

```text
merge A+B
split A+B
merge A+B
```

或者：

```text
accept relation
remove relation
accept relation
```

达到阈值：

```text
NEEDS_REVIEW
```

不能继续烧 token。

---

# 30. Decision History

```python
@dataclass
class DecisionEvent:
    decision_id: str

    round_no: int
    task_id: str

    decision_type: str

    target_keys: list[str]

    before_state_digest: str
    after_state_digest: str

    reason_codes: list[str]
```

用于：

```text
审计
回放
oscillation detection
benchmark error analysis
```

---

# 31. Planner 优先级

建议固定优先级：

```text
P0 schema / graph validity
P1 evidence correctness
P2 deterministic structure completeness
P3 high-confidence coverage gap
P4 relation / feature completion
P5 low-confidence disambiguation
P6 optional enrichment
```

先保证：

```text
正确
```

再追求：

```text
更丰富
```

---

# 32. Loop 不得突破 Target Scope

Phase 1 目标：

```text
只抽 Rule
```

Phase 2 不得因为修复 relation：

```text
顺便抽取大量非目标业务实体
```

允许创建关系端点的条件：

```text
endpoint type 是 relation 完整性所必需
+
调用配置允许 support endpoint
```

建议区分：

```text
target object
support object
```

但最终 output scope 规则必须明确。

---

# 33. Loop 不得突破 Ontology

Planner、Worker、Verifier 均只能使用：

```text
ExtractionPlan.allowed_types
ExtractionPlan.relation_specs
FeatureSpec
CompositeSpec
```

如果发现当前任务必须使用本体不存在的结构：

```text
SCHEMA_CAPABILITY_GAP
→ NEEDS_REVIEW
```

而不是新增 schema。

---

# 34. Phase 2 与现有图谱的关系

若后续需要查询已有全图：

```text
Loop 可以 Read
Loop 不直接 Write
```

Graph Search 可用于：

```text
entity resolution
existing identity match
alias
duplicate detection
conflict awareness
```

但最终写入仍由独立服务或人工确认触发。

---

# 35. Existing Graph Candidate

```python
@dataclass
class ExistingGraphMatch:
    candidate_id: str

    graph_uid: str
    match_score: float

    identity_match: bool
    property_overlap: dict
    conflict_fields: dict
```

Loop 可提出：

```text
merge suggestion
new node suggestion
conflict suggestion
```

但不直接写 Neo4j。

---

# 36. Runtime Loop 与 Offline Optimization Loop 必须区分

Phase 2 建议分为两个子阶段。

---

## Phase 2A：Runtime Extraction Loop

目标：

> 把一篇文档的初始抽取结果定向修到更完整、更一致。

这是 Phase 2 的必做部分。

---

## Phase 2B：Offline Algorithm Optimization Loop

目标：

> 根据固定 benchmark 上的错误模式，持续优化 Harness 的 strategy、规则和 prompt。

这不是对生产代码进行无约束自动自修改。

---

# 37. Offline Optimization Loop

```text
Frozen Benchmark
      ↓
Run Baseline Harness
      ↓
Measure
      ↓
Error Taxonomy
      ↓
Generate Improvement Candidate
      ↓
Implement / Configure Candidate
      ↓
Regression Run
      ↓
Compare
   ┌──┴──┐
   ▼     ▼
Accept  Reject
   │
   └────────────→ Next Experiment
```

---

# 38. Optimization Target

可优化：

```text
Prompt template
span strategy
rule pattern
mapping weight
acceptance threshold
relation candidate generation
coverage rule
worker routing
context window
```

默认不允许自动修改：

```text
ontology schema
business semantics
graph write policy
security policy
acceptance metric definition
```

---

# 39. Benchmark 必须分层

根据需求文档和当前本体，建议至少建立：

```text
B1 Term
B2 Ontology/Class Description
B3 Simple Rule
B4 Composite Rule
B5 Standard Structure
B6 Standard Object/Property/Constraint
B7 Standard Action/Reference
B8 Official Metadata
B9 Official Activity
B10 Official Exchange
B11 Official Constraint
B12 Tables
B13 Cross-paragraph Relation
B14 Ambiguous Mapping
```

---

# 40. Error Taxonomy

```text
MISS_ENTITY
MISS_PROPERTY
MISS_RELATION
WRONG_TYPE
WRONG_PROPERTY
WRONG_RELATION
WRONG_DIRECTION
DANGLING_RELATION
DUPLICATE_ENTITY
IDENTITY_DRIFT
UNSUPPORTED_VALUE
MISSING_EVIDENCE
WRONG_EVIDENCE
COMPOSITE_INCOMPLETE
STRUCTURE_INCOMPLETE
CARDINALITY_VIOLATION
ONTOLOGY_CAPABILITY_GAP
```

所有优化必须能说明：

```text
本次修改主要减少哪类错误？
```

---

# 41. Offline Acceptance

一个 Improvement Candidate 只有在：

```text
主指标提升
+
关键子指标不显著退化
+
schema validity 不退化
+
evidence validity 不退化
+
成本在预算内
```

时才接受。

禁止：

```text
Recall +3%
但 hallucination 大幅上升
```

仍判定为“优化成功”。

---

# 42. 85% 研发指标在 Phase 2 的位置

需求要求：

```text
统一抽取准确率 >=85%
```

Phase 2 的 Offline Evaluation 必须成为正式验收入口。

但指标公式需要先冻结。

建议同时设：

### Hard Gates

```text
Schema validity = 100%
No dangling relations = 100%
Deterministic structural correctness = 100%
```

### Main Extraction Metric

项目双方冻结的统一指标：

```text
>= 85%
```

### Supporting Metrics

```text
Entity F1
Property Accuracy
Relation F1
Composite Accuracy
Evidence Coverage
Coverage Recall Proxy
```

---

# 43. Runtime Loop 不能伪造 Accuracy

单篇没有 Gold 时：

不能显示：

```text
“本篇准确率 93%”
```

除非有真实标注。

Runtime 应显示：

```text
Mapping Confidence
Schema Validity
Evidence Coverage
Coverage Proxy
Unresolved Gaps
```

避免把 proxy 当真实 accuracy。

---

# 44. Loop Result Contract

```python
@dataclass
class LoopExtractionResult:
    run_id: str

    terminal_state: TerminalState
    rounds: int

    intermediate_model: IntermediateObjectModel

    graph: dict
    triples: list[dict]

    unresolved_gaps: list[CoverageGap]

    quality: QualityReport

    loop_stats: LoopStats
    decision_history: list[DecisionEvent]
    trace: list[TraceEvent]
```

---

# 45. LoopStats

```python
@dataclass
class LoopStats:
    initial_gap_count: int
    final_gap_count: int

    resolved_gap_count: int

    accepted_delta_count: int
    rejected_delta_count: int

    llm_calls: int
    tokens_used: int

    elapsed_seconds: float

    no_progress_rounds: int
```

---

# 46. Phase 2 目录结构

在 Phase 1 基础上增加：

```text
knowledge_extract/
│
├── loop/
│   ├── controller.py
│   ├── state.py
│   ├── assessment.py
│   ├── planner.py
│   ├── task.py
│   ├── delta.py
│   ├── verifier.py
│   ├── progress.py
│   ├── terminal.py
│   └── oscillation.py
│
├── workers/
│   ├── base.py
│   ├── entity_completion.py
│   ├── feature_completion.py
│   ├── relation_completion.py
│   ├── constraint_completion.py
│   ├── evidence_recovery.py
│   ├── identity_resolution.py
│   ├── composite_repair.py
│   ├── standard_reference.py
│   ├── official_activity.py
│   ├── official_exchange.py
│   └── official_constraint.py
│
└── optimization/
    ├── benchmark.py
    ├── evaluator.py
    ├── error_taxonomy.py
    ├── experiment.py
    ├── comparator.py
    └── regression.py
```

---

# 47. 主流程伪代码

```python
def run_with_loop(request: ExtractionRequest) -> LoopExtractionResult:

    initial = harness.run(request)

    state = ExtractionLoopState.from_initial_result(initial)

    while True:

        assessment = quality_assessor.assess(state)

        terminal = controller.should_stop(
            state=state,
            assessment=assessment,
        )

        if terminal is not None:
            return finalize(state, terminal)

        tasks = planner.plan(
            state=state,
            assessment=assessment,
        )

        verified_deltas = []

        for task in tasks:

            worker = worker_registry.resolve(task.task_type)

            delta = worker.execute(
                task=task,
                state=state,
            )

            verification = delta_verifier.verify(
                delta=delta,
                state=state,
                task=task,
            )

            accepted_delta = verification.accepted_delta()

            if accepted_delta:
                verified_deltas.append(accepted_delta)

        previous_quality = state.quality

        state.commit(
            verified_deltas
        )

        state.revalidate(
            harness=harness
        )

        controller.update_progress(
            state=state,
            previous_quality=previous_quality,
            current_quality=state.quality,
        )
```

---

# 48. Loop API

```python
engine = LoopKnowledgeExtractionEngine(
    harness=harness,
    controller=controller,
    planner=planner,
    workers=workers,
)

result = engine.run(
    chunks=chunks,
    ontology=ontology,
    target_types=["OperationalActivity"],
    loop_config=LoopConfig(
        enabled=True,
        max_rounds=4,
    ),
)
```

兼容：

```python
harness.run(...)
```

Phase 1 仍可独立运行。

---

# 49. Loop Configuration

```python
@dataclass
class LoopConfig:
    enabled: bool = True

    max_rounds: int = 4
    max_tasks_per_round: int = 8
    max_tasks_per_span: int = 2

    max_llm_calls: int = 20
    max_runtime_seconds: int = 300

    no_progress_limit: int = 2

    enable_low_confidence_repair: bool = True
    enable_evidence_recovery: bool = True
    enable_relation_completion: bool = True
    enable_composite_repair: bool = True
```

所有默认值通过 benchmark 调整。

---

# 50. Phase 2 验收：功能

## AC-L1：Gap-driven

每一个 Loop task 必须能回溯到：

```text
gap / warning / low-confidence object
```

不能凭空启动任务。

## AC-L2：Locality

Task 必须有：

```text
span_ids
object_ids
allowed_types
allowed_relations
```

## AC-L3：Verified Delta Only

Worker 无权直接修改最终 graph。

## AC-L4：No Full Regeneration

默认禁止每轮重新生成整个 graph。

## AC-L5：Stop

必须支持：

```text
COMPLETE
PARTIAL
NEEDS_REVIEW
NO_PROGRESS
BUDGET_EXHAUSTED
FAILED
```

## AC-L6：Budget

达到预算后稳定停止。

## AC-L7：Oscillation

构造 merge/split oscillation testcase，系统必须停止并进入 review。

---

# 51. Phase 2 验收：质量

与 Phase 1 initial run 比较：

```text
high-priority unresolved gaps 下降
entity recall 不下降
relation recall 提升
evidence validity 不下降
schema validity 保持 100%
hallucination / unsupported value 不增加
```

在固定 benchmark 上：

```text
Loop result 的统一抽取主指标
>
Phase 1 initial result
```

且最终达到正式要求：

```text
>=85%
```

---

# 52. Phase 2 验收：成本

Loop 不能只看质量。

报告：

```text
LLM calls / document
tokens / document
seconds / document
accepted deltas / calls
resolved gaps / calls
```

定义：

```text
marginal quality gain per call
```

用于决定是否继续 Loop。

---

# 53. 推荐实施顺序

## Step 1：LoopState

不要先写 Planner。

先确保 Phase 1 Result 能稳定恢复成完整状态。

---

## Step 2：Gap → Task

先使用 deterministic router。

```text
gap_type -> worker
```

不要先让 LLM Planner 自由规划。

---

## Step 3：ExtractionDelta

完成：

```text
add/update/remove
preview
verify
commit
rollback
```

---

## Step 4：Delta Verifier

先把安全边界做稳。

---

## Step 5：首批 Workers

优先：

```text
RelationCompletion
ConstraintCompletion
EvidenceRecovery
CompositeRepair
```

这些任务目标最明确。

---

## Step 6：Controller

实现：

```text
hard stop
quality stop
no progress
budget
```

---

## Step 7：Standard Loop

在标准 benchmark 上验收。

---

## Step 8：OfficialDocument Loop

重点验收：

```text
activity
exchange
constraint
EVIDENCED_BY
```

---

## Step 9：Low Confidence Mapping

最后再增加 semantic disambiguation。

---

## Step 10：Offline Optimization Loop

建立长期回归体系。

---

# 54. Phase 2 里程碑

## M1 — Loop Infrastructure

完成：

```text
State / Task / Delta / Verifier / Controller
```

验收：

```text
能手工注入 Gap 并完成一次局部 repair
```

## M2 — Coverage Repair Loop

验收：

```text
Phase 1 coverage gap 可自动转成 task
```

## M3 — Standard Loop

验收：

```text
Constraint / Action / Reference / Relation gap 明显下降
```

## M4 — Official Loop

验收：

```text
Activity / Exchange / Constraint / Evidence gap 明显下降
```

## M5 — Composite Loop

验收需求文档 SimpleRule / CompositeRule 场景。

## M6 — Optimization Benchmark

验收：

```text
实验可复现
可比较
可 rollback
```

---

# 55. 与 Phase 1 的职责边界

Phase 1：

```text
如何抽
如何验证
如何评分
如何发现 gap
如何组装 graph
```

Phase 2：

```text
发现 gap 后下一步做什么
调用哪个能力
接受哪个 delta
什么时候继续
什么时候停止
```

所以：

> Phase 2 不重新实现 Extraction。

---

# 56. 最终两阶段系统

```text
             Parsed text/chunks + User Request
                           │
                           ▼
               Ontology + Target Scope
                           │
                           ▼
┌────────────────────────────────────────────────────────────┐
│              Phase 1 Extraction Harness                    │
│                                                            │
│ Analyze → Compile → Extract → Evidence → Resolve           │
│        → Relations → Composite → Audit → Validate          │
│                                                            │
│ Output: Initial Intermediate Model + Graph + Gaps           │
└──────────────────────────┬─────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────────┐
│              Phase 2 Loop Engineering                      │
│                                                            │
│ Observe → Assess → Plan → Worker → Delta → Verify           │
│       → Commit → Re-audit → Progress → Stop/Continue       │
└──────────────────────────┬─────────────────────────────────┘
                           │
                           ▼
                 Verified Final Knowledge
                           │
                    ┌──────┴──────┐
                    ▼             ▼
             Intermediate O1   Graph/Triples O2
```

---

# 57. Phase 2 一句话定义

> **Phase 2 Loop Engineering 是一个建立在 Phase 1 可验证抽取能力之上的质量反馈闭环：它根据结构化 Gap 和低置信度状态生成局部任务，由专用 Worker 产生 Candidate Delta，再通过本体、证据和回归验证后提交，并由明确的质量、进展和预算规则决定是否继续。**

---

# 58. 最终工程原则

```text
不是：
LLM → 再问 LLM → 再问 LLM

而是：
Ontology
  +
Explicit State
  +
Measured Gap
  +
Bounded Task
  +
Specialized Worker
  +
Verified Delta
  +
Progress Metric
  +
Stopping Rule
```

最终目标是：

> **先让 Knowledge Extraction Harness 能可靠地知道“哪里错、哪里漏”；再让 Loop Engineering 只针对这些已知缺口做最小、可验证、可回滚的改动。**
