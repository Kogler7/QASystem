---
name: plan-execute
description: 将图谱中的方案类单元（由 `KNOWLEDGE_RELATION` 关系导航到的方案节点）实例化为原子工具调用并执行。接收 GraphContext，输出 ExecutionReport。由 problem-solve 阶段四调用。
inject_layer: developer
---

# 图谱方案实例化与执行

## 铁律 — 最高优先级

1. **禁止用模型记忆填补任何参数。** 表名、字段名、业务阈值、计算公式、规则——必须来自 GraphContext 中的图谱资源或工具返回。
2. **禁止伪造工具结果。** 工具失败时，不得用"估计值"或"典型值"替代。重试（最多 3 次），仍失败标记 failed。
3. **图谱没有方案 = 停止。** 若 GraphContext 中没有方案类单元（由 `KNOWLEDGE_RELATION` 关系导航到），停止执行并报告缺口。不得自行设计一个业务方案。
4. **SQL 默认只读。** INSERT/UPDATE/DELETE/TRUNCATE 必须图谱方案明确要求且用户已授权。缺任一条件 → 暂停并确认。
5. **参数必须可追溯。** 每个关键参数标注 value_source，不能标注的 → 记录为缺口，不猜测。

## 用途

接收 GraphContext 中的方案类单元和资源节点，将其转化为原子工具调用序列并执行。上游输入为 ProblemFrame + GraphContext（solution_class、resource_nodes、evidence_ledger、gaps），下游输出为 ExecutionReport。

> 方案类单元 = 由 `KNOWLEDGE_RELATION` 关系导航到的方案节点，其方案内容通常在 `content`（或 `solutionTemplate`/`steps` 属性）。

---

## 执行流程

按以下 7 步执行。每步完成后必须自检。

### 一、执行前验证

只有满足以下四项才进入执行：

| # | 验证项 | 通过标准 | 失败处理 |
|---|--------|---------|---------|
| 1 | 方案来源 | 方案类单元有图谱节点 ID 或可追溯证据路径 | 停止，报告缺口 |
| 2 | 步骤来源 | 方案步骤来自节点真实属性或相邻方案步骤节点 | 回到 semantic-align 补充 |
| 3 | 输入映射 | 关键输入能映射到用户约束、图谱资源或前序步骤输出 | 标记无法映射的输入为 blocked |
| 4 | 口径来源 | 关键指标口径、过滤条件和数据源均有图谱依据 | 记录缺口，不猜测口径 |

若不存在图谱方案 → **停止。禁止自行设计业务方案冒充图谱方案。**

---

### 二、方案规范化

把图谱中的方案表示规范化为标准结构：

```json
{
  "solution_source": {"id": "", "name": "", "evidence_path": []},
  "steps": [{
    "id": "step-1",
    "objective": "",
    "depends_on": [],
    "inputs": {},
    "expected_output": "",
    "resource_refs": [],
    "source_property": "solutionTemplate"
  }]
}
```

- 只做格式转换、参数绑定和依赖解析，不改变方案的业务含义
- 若原方案步骤含糊（如"查询相关数据"）→ 回到图谱探索相关节点明确步骤
- `depends_on` 必须精确反映步骤间的数据依赖关系
- `source_property` 记录步骤来自节点的哪个属性

**【自检】**
- [ ] 业务含义是否原样保留？没有增删改步骤？
- [ ] source_property 是否已记录？

---

### 三、按方案步骤获取资源

> semantic-align 已完成方案类定位，但未收集具体资源节点（resource_nodes_deferred: true）。本阶段严格按方案步骤的描述获取资源。

**核心原则：方案步骤决定工具，不预设工具类型。**

- 步骤需要理解某个业务实体/术语 → 使用 `link_entities`（只链术语节点，用于术语对齐）
- 步骤说"从图谱中查询 X 的邻居" → 使用 `search_graph` / `query_entity_properties`
- 步骤说"查询数据库 Y 表 / 指标数据" → 使用 `execute_sql`；SQL 模板、字段与口径取自图谱方案或图谱资源（表结构查询工具已停用，不调用）
- 步骤不指定工具 → 根据步骤目标选择最合适的工具，记录选择理由

**图谱查询的唯一入口：语义对齐阶段链接的术语节点（aligned_entities）**

- 阶段二已通过 `link_entities`（术语对齐）把术语记录到 `aligned_entities`，执行时用它们理解方案步骤中的业务实体
- 图谱探索从 `aligned_entities` 的术语节点出发
- 数据获取按方案步骤描述执行（步骤会指定查询对象）
- 不允许用未经确认的节点直接作为图谱搜索起点

**参数来源优先级（从高到低）**：
1. 用户明确给出的目标和约束
2. 方案类单元与业务规则节点
3. 实体链接确认的 KnowledgeUnit（图谱探索唯一入口）
4. 按方案步骤获取的资源（图谱节点、数据表、字段、SQL 模板等）
5. 已验证的前序步骤输出

**value_source 格式**：
```json
{
  "param_name": "table_name",
  "value": "sales_data",
  "source_type": "resource_node | user_constraint | previous_output | solution_class",
  "source_id": "节点ID 或 user_input 或 step-1",
  "source_property": "tableName"
}
```

- SQL 模板中的占位符必须全部绑定后才能执行
- 参数值缺失时：按方案步骤指定的方式获取；仍缺失 → 标记为缺口

---

### 四、原子工具映射

根据步骤目标选择现有 MCP 工具：

| 场景 | 工具 | 选择条件 |
|------|------|---------|
| 图谱信息不足 | 返回 semantic-align，使用 `search_graph` / `query_entity_properties` | 当前缺少步骤必需的信息 |
| 执行只读查询 | `execute_sql` | SQL 完整且参数已绑定（SQL 与字段口径来自图谱） |
| 生成图表 | `draw_*` 或先调用 `list_chart_types` | 数据已获取 |
| 知识抽取 | `kg_extract_v2` | 仅当图谱方案明确要求 |

工具选择依据必须是当前方案步骤的目标，不是额外创造的业务流程。

**【自检】**
- [ ] 所选工具能力是否匹配步骤 objective？
- [ ] 是否已排除用更高风险的工具替代？
- [ ] 工具所需输入是否已在参数绑定阶段准备完毕？

---

### 五、执行调度

1. 根据 `depends_on` 建立 DAG；无依赖且资源互不冲突的步骤可并行。
2. 每步执行后验证返回 envelope（`code == 0`）、输出结构和业务约束，**并记录该步骤对应的输出/结果**。
3. 工具失败时最多调整参数重试 2 次（共 3 次尝试），不得用伪造结果继续。
4. SQL 默认只读。写操作必须：(a) 图谱方案明确要求，(b) 用户已授权。
5. 前序输出不满足下一步要求时，回退到参数绑定或图谱探索。
6. 记录实际执行与图谱原方案之间的差异及原因。

> ⚠️ **必须严格按方案步骤逐步执行，每一步都给出对应的输出/结果**，不得合并多步、也不得把整体描述为"已执行"。ExecutionReport 中每一步都要有实际 tool_calls 和 output。

**并行安全判断**：

| 条件 | 可并行？ |
|------|---------|
| depends_on 为空且操作不同资源 | 是 |
| depends_on 为空但操作同一表 | 是（只读）/ 否（写入）|
| 步骤 B 依赖步骤 A 的输出 | 否 |
| 步骤 B 使用步骤 A 可能修改的资源 | 否 |

**【自检】**
- [ ] 本步实际执行了几个工具调用？（必须 > 0）

---

### 六、结果验证

逐项检查：

- **完整性**：是否完成图谱方案规定的输出
- **口径一致**：数据字段和指标口径是否与图谱一致
- **数据质量**：查询结果是否为空、截断或异常（空结果如实报告，不等于错误）
- **图表正确**：图表类型、数据映射是否与方案要求和用户目标一致
- **可追溯**：每个业务结论能否追溯到图谱证据和运行结果

---

### 七、输出 ExecutionReport

```json
{
  "solution_source": {"id": "", "name": "", "evidence_path": []},
  "status": "completed | partial | blocked",
  "steps": [{
    "id": "", "status": "completed | failed | skipped",
    "tool_calls": [{"tool_name": "", "params": {}, "result_summary": "", "error": null}],
    "output": null,
    "evidence_refs": [],
    "deviation": null
  }],
  "artifacts": [],
  "verified_claims": [],
  "assumptions": [],
  "gaps": [],
  "errors": []
}
```

**status 判定**：
- `completed`：所有步骤执行成功，输出符合预期
- `partial`：部分完成，已完成部分仍有价值
- `blocked`：关键步骤因缺口无法执行，已完成部分无独立价值

最终答案优先呈现结论，但须能通过 solution_source、evidence_refs 和工具结果完成审计。

---

## 正确示例

```
方案类单元步骤: step-1（读取查询所需 SQL 模板与口径）→ step-2（执行查询）

执行:
1. 验证 → 方案类单元有节点 ID ✓，步骤来自方案内容 ✓
2. 规范化 → 识别 depends_on: step-2 依赖 step-1
3. 执行 step-1: query_entity_properties（方案/指标节点）→ 取得 SQL 模板与字段口径
4. 参数绑定: 用用户约束填充模板占位符，记录 value_source
5. 执行 step-2: execute_sql → 返回 150 行
6. 验证: 结果字段与图谱口径一致 ✓，行数合理 ✓
7. 输出 ExecutionReport(status="completed", ...)
```

---

## 异常处理

| 情况 | 处理 |
|------|------|
| 字段值为编码需语义映射 | 从 KnowledgeUnit 出发查图谱获取字典映射或字段注释 |
| 步骤缺少关键参数 | 按方案步骤指定的方式获取；无法获取则标注并跳过 |
| 工具调用失败 | 分析原因，调整参数重试（最多 2 次）|
| 步骤输出不满足要求 | 换工具或补充信息后重试 |
| 需要补充图谱信息 | 回退到 semantic-align 继续探索 |
| 连续失败 | 说明障碍，询问用户或给出替代建议 |

## 停止条件

- **执行前验证失败** → 停止，报告缺口，回到 semantic-align
- **参数绑定时关键值无图谱来源** → 记录缺口；若导致步骤无法执行 → 标记为 blocked
- **同一工具连续失败 3 次** → 停止重试该步骤，标记 failed，继续其他独立步骤
- **SQL 返回空结果** → 不是错误，如实报告
- **写入操作未获授权** → 停止，展示操作内容和影响范围，等待确认
- **步骤输出与预期严重不符** → 回退到参数绑定或图谱探索
- **3 个以上步骤 blocked 或 failed** → 评估继续价值，报告给编排层
