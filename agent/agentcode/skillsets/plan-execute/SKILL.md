---
noteId: "6125c6c085b011f1a953d7289dee1b45"
tags: []
name: "plan-execute"
description: "将图谱中的 SolutionClass 实例化为原子工具调用并执行；允许自主调度，但禁止脱离图谱创造业务方案或业务事实。"
inject_layer: "developer"
version: "2.0.0"

---

# 图谱方案实例化与执行

<Purpose>
接收 GraphContext 中的 SolutionClass 和资源节点，将其转化为原子工具调用序列并执行。Agent 自主调度（顺序/并行/重试），但方案的业务含义必须原样保留，不得增删改。
</Purpose>

<Use_When>
- 已通过 semantic-align 获取了 SolutionClass 和资源节点
- 证据门检查已通过
- 需要将图谱中的方案步骤映射为具体的 MCP 工具调用
- 作为 problem-solve 流程的阶段四被调用
</Use_When>

<Do_Not_Use_When>
- 还没有 SolutionClass — 先回到 semantic-align 继续探索
- 证据门未通过 — 不应进入执行阶段
- 方案步骤涉及写操作且用户未授权 — 先确认
</Do_Not_Use_When>

<Why_This_Exists>
图谱中的 SolutionClass 是抽象的业务方案，需要被实例化为具体的工具调用才能产出结果。本 skill 确保：
1. 图谱方案的业务语义在实例化过程中不被篡改
2. 每个参数都能追溯到用户约束或图谱资源
3. 工具调用失败时有明确的回退和重试策略
4. 执行结果可审计
</Why_This_Exists>

<Integration>
本 skill 是三层流水线的第三层（执行层）：

```
problem-solve（编排层）
  → semantic-align（检索层）→ GraphContext
    → 证据门检查（problem-solve 阶段三）
      → plan-execute（本 skill，执行层）→ ExecutionReport
```

**上游输入**：
- `ProblemFrame`：用户目标与约束
- `GraphContext.solution_class`：图谱提供的业务方案
- `GraphContext.resource_nodes`：数据源、字段、模板、规则或组件
- `GraphContext.evidence_ledger`：事实与来源
- `GraphContext.gaps`：尚未解决的知识缺口
- `GraphContext.business_rules`：业务规则与约束

**下游输出**：`ExecutionReport`（交付给 problem-solve 阶段五做最终呈现）

**回退到上游**：当执行中发现图谱信息不足时（如缺少表结构、字段不明确），应回退到 semantic-align 补充图谱信息，而非猜测。
</Integration>

<Execution_Policy>
- 执行前必须通过四项验证（SolutionClass 来源、步骤来源、输入映射、口径来源）
- 参数绑定的每个关键值必须可追溯（value_source）
- SQL 默认只读；写操作必须由图谱方案明确要求且获得用户授权
- 工具失败时最多重试 2 次（共 3 次尝试），不得用伪造结果继续
- 前序输出不满足下一步要求时回退，不静默跳过
- 记录实际执行与图谱原方案之间的差异及原因
</Execution_Policy>

<Steps>

### 一、执行前验证

只有满足以下四项条件才进入执行：

| # | 验证项 | 通过标准 | 失败处理 |
|---|--------|---------|---------|
| 1 | 方案来源 | SolutionClass 有图谱节点 ID 或可追溯证据路径 | 停止，报告缺口 |
| 2 | 步骤来源 | 方案步骤来自节点真实属性或相邻方案步骤节点 | 回到 semantic-align 补充 |
| 3 | 输入映射 | 关键输入能映射到用户约束、图谱资源或前序步骤输出 | 标记无法映射的输入为 blocked |
| 4 | 口径来源 | 关键指标口径、过滤条件和数据源均有图谱依据 | 记录缺口，不猜测口径 |

若不存在图谱方案，停止执行并报告知识缺口。不得自行设计一个业务方案冒充 SolutionClass。

### 二、方案规范化

把图谱中的各种方案表示规范化为标准结构：

```json
{
  "solution_source": {"id": "", "name": "", "evidence_path": []},
  "steps": [
    {
      "id": "step-1",
      "objective": "",
      "depends_on": [],
      "inputs": {},
      "expected_output": "",
      "resource_refs": [],
      "source_property": "solutionTemplate"
    }
  ]
}
```

**规范化规则**：
- 只做格式转换、参数绑定和依赖解析，不改变方案的业务含义
- 若原方案步骤含糊（如"查询相关数据"），先回到图谱探索相关节点明确步骤；仍无法确定时记录缺口
- `depends_on` 必须精确反映步骤间的数据依赖关系
- `source_property` 记录步骤来自节点的哪个属性（便于审计追溯）

### 三、参数绑定

参数来源优先级（从高到低）：

1. 用户明确给出的目标和约束；
2. SolutionClass 与业务规则节点；
3. resource_nodes 中的标识、表名、字段、SQL 模板或配置；
4. 已验证的前序步骤输出。

**绑定规则**：
- 每个关键参数绑定后必须记录 `value_source`（来源类型 + 来源 ID）
- 禁止从模型记忆补出表名、字段名、业务阈值、计算公式或规则
- 参数值缺失时：先尝试从 resource_nodes 查找 → 其次检查前序输出 → 仍缺失则标记为缺口
- SQL 模板中的占位符必须全部绑定后才能执行

**value_source 格式**：
```
{
  "param_name": "table_name",
  "value": "sales_data",
  "source_type": "resource_node | user_constraint | previous_output | solution_class",
  "source_id": "节点ID 或 "user_input" 或 "step-1",
  "source_property": "tableName"
}
```

### 四、原子工具映射

根据步骤目标选择现有原子 MCP：

| 场景 | 工具 | 选择条件 |
|------|------|---------|
| 图谱信息不足 | 返回 `semantic-align`，使用 `qasystem-tools__search_graph` / `qasystem-tools__query_entity_properties` | resource_nodes 缺少必要的表结构或字段信息 |
| 查看数据库结构 | `qasystem-tools__get_database_info`、`list_tables`、`describe_table` | 需要确认表存在、获取字段列表 |
| 执行只读查询 | `qasystem-tools__execute_sql` | 已有完整 SQL 且所有参数已绑定 |
| 生成图表 | 选择 `qasystem-tools__draw_*`，或先调用 `list_chart_types` | 数据已获取、图表类型与数据维度匹配 |
| 知识抽取与存储 | `kg_extract_v2`、`kg_store_extraction` | 仅当图谱方案明确要求且用户已授权 |

工具选择是 Agent 的自主行为，但选择依据必须是当前方案步骤的目标，而不是额外创造的业务流程。

**工具选择自检**：
- [ ] 所选工具的能力是否匹配步骤的 objective？
- [ ] 是否已排除用更高风险的工具替代（如用写操作替代只读查询）？
- [ ] 工具所需的输入是否在参数绑定阶段已准备完毕？

### 五、执行调度

1. **依赖分析**：根据 `depends_on` 建立 DAG；无依赖且资源互不冲突的步骤可并行执行。
2. **执行**：每步执行后验证返回 envelope（`code == 0`）、输出结构和业务约束。
3. **失败处理**：工具失败时最多调整参数重试两次（共 3 次尝试）；不得用伪造结果继续。
4. **写操作安全**：SQL 默认只读。写操作必须同时满足：(a) 图谱方案明确要求，(b) 用户已授权。缺少任一条件则暂停执行并确认。
5. **回退机制**：前序输出不满足下一步要求时，回退到参数绑定或图谱探索，不静默跳过。
6. **偏差记录**：记录实际执行与图谱原方案之间的差异及原因。

**并行安全判断**：
| 条件 | 可并行？ |
|------|---------|
| depends_on 为空且操作不同资源 | 是 |
| depends_on 为空但操作同一表 | 是（只读），否（写入）|
| 步骤 B 依赖步骤 A 的输出 | 否，必须顺序 |
| 步骤 B 使用步骤 A 可能修改的资源 | 否，必须顺序 |

### 六、结果验证

逐项检查：

- **完整性**：是否完成 SolutionClass 规定的输出；
- **口径一致**：数据字段和指标口径是否与图谱一致；
- **数据质量**：查询结果是否为空、截断或异常（空结果不等于错误，但需如实报告）；
- **图表正确**：图表类型、数据映射是否与方案要求和用户目标一致；
- **可追溯**：每个业务结论能否追溯到图谱证据和运行结果。

计算结果可以来自 SQL 等工具；对结果的业务解释仍必须由图谱中的定义、规则和方案支持。

### 七、输出 ExecutionReport

```json
{
  "solution_source": {"id": "", "name": "", "evidence_path": []},
  "status": "completed|partial|blocked",
  "steps": [
    {
      "id": "",
      "status": "completed|failed|skipped",
      "tool_calls": [
        {"tool_name": "", "params": {}, "result_summary": "", "error": null}
      ],
      "output": null,
      "evidence_refs": [],
      "deviation": null
    }
  ],
  "artifacts": [],
  "verified_claims": [],
  "assumptions": [],
  "gaps": [],
  "errors": []
}
```

**status 判定**：
- `completed`：所有步骤执行成功，输出符合预期
- `partial`：部分步骤完成，部分 failed/skipped/blocked，但已完成部分仍有价值
- `blocked`：关键步骤因缺口或授权问题无法执行，且已完成部分无独立价值

最终答案应优先呈现结论，但必须能通过 `solution_source`、`evidence_refs` 和工具结果完成审计。

</Steps>

<Anti_Patterns>
以下是必须避免的常见错误模式：

1. **自行设计业务方案**
   - 错误：图谱没有 SolutionClass → Agent 自行设计 SQL 方案并声称来自图谱
   - 正确：停止执行，报告"知识图谱尚未沉淀该方案"，不得自行设计一个业务方案冒充 SolutionClass

2. **用模型记忆填补参数**
   - 错误：resource_nodes 中没有表的字段信息 → Agent 从训练数据中补出字段名和类型
   - 正确：调用 describe_table 获取真实结构，或记录为知识缺口

3. **伪造工具结果**
   - 错误：SQL 执行失败 → Agent 用"估计"的数值替代真实查询结果
   - 正确：重试（最多 2 次），仍失败则标记步骤为 failed 并记录错误信息

4. **跳过写操作授权**
   - 错误：方案涉及 INSERT/UPDATE/DELETE，Agent 未确认授权就直接执行
   - 正确：暂停执行，向用户展示将要执行的操作及影响范围，等待授权

5. **静默跳过失败步骤**
   - 错误：步骤 2 执行失败，Agent 跳过它继续执行步骤 3，但步骤 3 依赖步骤 2 的输出
   - 正确：步骤 2 失败时，标记依赖它的步骤为 skipped，记录原因

6. **未验证就交付**
   - 错误：SQL 返回 1000 行数据，Agent 未做任何验证直接全部呈现
   - 正确：检查行数、字段完整性、口径一致性；截断或异常时如实说明
</Anti_Patterns>

<Examples>
<Good>
SolutionClass 有 step-1（查表结构）和 step-2（执行查询），step-2 依赖 step-1。
Agent:
  1. 验证：SolutionClass 有节点 ID ✓，步骤来自 solutionTemplate 属性 ✓
  2. 规范化：识别 depends_on 关系
  3. 执行 step-1: describe_table → 获取字段列表（12 个字段）
  4. 参数绑定：从 resource_nodes 取 SQL 模板，用字段列表和用户约束填充参数，记录 value_source
  5. 执行 step-2: execute_sql → 返回 150 行数据
  6. 验证：字段列表与图谱指标口径一致 ✓，行数合理 ✓
</Good>

<Good>
SolutionClass 的步骤中有描述含糊的 "生成图表" 步骤，未指定图表类型。
Agent: 先回到 semantic-align → search_graph 找到与方案关联的 ChartConfig 节点 → 确定图表类型为"柱状图"、X 轴为"月份"、Y 轴为"销售额" → 继续执行 draw_bar_chart。
记录 deviation: "图表类型和轴映射来自 ChartConfig 节点补充检索，原方案步骤未明确指定"
</Good>

<Good>
Agent 执行 SQL 查询，第一次因表名错误失败。
Agent: 检查 resource_nodes → 发现正确的表名 → 修正 SQL → 第二次执行成功。
记录: "第 1 次尝试使用表名 'sales' 失败（表不存在），从 resource_nodes 确认正确表名为 'sales_data' 后重试成功"
</Good>

<Bad>
Agent 发现方案中需要用到 sales_data 表，但 resource_nodes 中没有这个表的元数据。
Agent 从模型记忆中补充了表结构并直接执行了 SQL 查询。
Why bad: 表名和字段名必须来自图谱。应报告知识缺口，或继续从图谱检索表元数据。
</Bad>

<Bad>
Agent 执行 SQL 时出错，将错误信息替换为一份"估计"的查询结果返回给用户。
Why bad: 工具失败时不得用伪造结果继续。应重试或报告失败。
</Bad>

<Bad>
方案中的步骤 2 写入数据到数据库。Agent 没有询问用户授权就直接执行了。
Why bad: 写操作必须获得用户明确授权。应先展示操作内容和影响范围，等待确认。
</Bad>
</Examples>

<Escalation_And_Stop_Conditions>
- **执行前验证失败**: 缺少 SolutionClass 或关键资源 → 停止执行，报告缺口，回到 semantic-align
- **参数绑定时关键值无图谱来源**: 记录缺口，不猜测值；如果缺口导致步骤无法执行 → 标记为 blocked
- **同一工具调用连续失败 3 次**: 停止重试该步骤，记录错误，标记为 failed，继续执行其他独立步骤
- **SQL 返回空结果**: 不是错误——如实报告"查询结果为空"，不要编造数据
- **写入操作未获授权**: 停止执行，向用户展示操作内容、影响范围，等待确认后再继续
- **步骤输出与预期严重不符**: 回退到参数绑定或图谱探索，不静默跳过
- **3 个以上步骤 blocked 或 failed**: 评估是否还有继续的价值——可能方案本身有问题，报告给编排层
</Escalation_And_Stop_Conditions>

<Final_Checklist>
- [ ] 执行前四项验证全部通过
- [ ] 方案步骤已规范化，业务含义未被修改，source_property 已记录
- [ ] 每个关键参数有 value_source，可追溯到用户约束、图谱资源或前序输出
- [ ] 工具选择基于方案步骤目标，而非额外创造的业务逻辑
- [ ] 并行步骤已识别并实际并行执行（无依赖且资源不冲突）
- [ ] 写入操作已获得用户授权
- [ ] 每步结果已验证（envelope、输出结构、业务约束）
- [ ] 实际执行与图谱方案之间的差异已记录在 deviation 中
- [ ] ExecutionReport 包含完整的 solution_source、evidence_refs、errors 和 value_sources
- [ ] 没有伪造工具结果、没有用模型记忆填补参数、没有自行设计方案步骤
</Final_Checklist>
