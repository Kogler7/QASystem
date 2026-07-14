---
name: plan-execute
description: 将图谱中的 SolutionClass 实例化为原子工具调用并执行；允许自主调度，但禁止脱离图谱创造业务方案或业务事实。
inject_layer: developer
---

# 图谱方案实例化与执行

## 输入

- `ProblemFrame`：用户目标与约束；
- `GraphContext.solution_class`：图谱提供的业务方案；
- `GraphContext.resource_nodes`：数据源、字段、模板、规则或组件；
- `GraphContext.evidence_ledger`：事实与来源；
- `GraphContext.gaps`：尚未解决的知识缺口。

## 一、执行前验证

只有满足以下条件才执行：

1. SolutionClass 有图谱节点 ID 或可追溯证据路径；
2. 方案步骤来自节点真实属性或相邻方案步骤节点；
3. 关键输入能映射到用户约束、图谱资源或前序步骤输出；
4. 关键指标口径、过滤条件和数据源均有图谱依据。

若不存在图谱方案，停止执行并报告知识缺口。不得自行设计一个业务方案冒充 SolutionClass。

## 二、方案规范化

把图谱中的各种方案表示规范化为：

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

只做格式转换、参数绑定和依赖解析，不改变方案的业务含义。若原方案步骤含糊，先回到图谱探索相关节点；仍无法确定时记录缺口。

## 三、参数绑定

参数来源优先级：

1. 用户明确给出的目标和约束；
2. SolutionClass 与业务规则节点；
3. resource_nodes 中的标识、表名、字段、SQL 模板或配置；
4. 已验证的前序步骤输出。

禁止从模型记忆补出表名、字段名、业务阈值、计算公式或规则。绑定过程保留 `value_source`，确保每个关键参数可追溯。

## 四、原子工具映射

根据步骤目标选择现有原子 MCP：

- 图谱信息不足：返回 `semantic-align`，继续使用 `search_graph` / `query_entity_properties`；
- 查看数据库结构：`qasystem-tools__get_database_info`、`list_tables`、`describe_table`；
- 执行只读查询：`qasystem-tools__execute_sql`；
- 生成图表：选择 `qasystem-tools__draw_*`，或先调用 `list_chart_types`；
- 知识抽取与存储：仅当图谱方案明确要求时使用 `kg_extract_v2`、`kg_store_extraction` 等工具。

工具选择是 Agent 的自主行为，但选择依据必须是当前方案步骤，而不是额外创造的业务流程。

## 五、执行调度

1. 根据 `depends_on` 建立依赖关系；无依赖且资源互不冲突的步骤可并行。
2. 每步执行后验证返回 envelope、输出结构和业务约束。
3. 工具失败时最多调整参数重试两次；不得用伪造结果继续。
4. SQL 默认只读。写操作必须由图谱方案明确要求且获得用户授权。
5. 前序输出不满足下一步要求时，回退到参数绑定或图谱探索，而不是静默跳过。
6. 记录实际执行与图谱原方案之间的差异及原因。

## 六、结果验证

逐项检查：

- 是否完成 SolutionClass 规定的输出；
- 数据字段和指标口径是否与图谱一致；
- 查询结果是否为空、截断或异常；
- 图表是否与数据维度和用户目标一致；
- 每个业务结论能否追溯到图谱证据和运行结果。

计算结果可以来自 SQL 等工具；对结果的业务解释仍必须由图谱中的定义、规则和方案支持。

## 输出：ExecutionReport

```json
{
  "solution_source": {},
  "status": "completed|partial|blocked",
  "steps": [
    {
      "id": "",
      "status": "completed|failed|skipped",
      "tool_calls": [],
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

最终答案应优先呈现结论，但必须能通过 `solution_source`、`evidence_refs` 和工具结果完成审计。
