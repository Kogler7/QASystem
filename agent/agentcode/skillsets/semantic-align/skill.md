---
name: semantic-align
description: 将用户表达对齐到知识图谱，并从图谱中检索问题类、方案类、规则与执行资源；不依赖意图识别或方案规划 MCP。
inject_layer: developer
---

# 语义对齐与图谱方案检索

## 目标

把自然语言请求转换为可追溯的 `GraphContext`。Agent 负责导航判断，图谱负责提供全部业务语义和方案知识。

## 可用原子工具

| 工具 | 用途 |
| --- | --- |
| `qasystem-tools__locate_knowledge_space` | 根据问题和应用约束定位知识子空间 |
| `qasystem-tools__get_all_subspaces` | 定位失败或需要消歧时查看候选子空间 |
| `qasystem-tools__link_entities` | 将一个或多个关键词链接到候选图谱实体 |
| `qasystem-tools__query_entity_properties` | 获取节点完整属性 |
| `qasystem-tools__search_graph` | 确定性查询一跳关系；可按 `relationship_types`、`direction`、`limit` 过滤 |

所有工具结果都采用 `{code, message, data, error}`。只有 `code == 0` 时才能把 `data` 作为证据；失败信息必须保留并用于调整下一步。

## 一、知识空间定位

1. 用原始问题调用 `locate_knowledge_space`，把用户明确给出的领域、场景、时间、组织等放入 `app_requirement`。
2. 检查 `可回答`、`目标子空间`、`选中的刻度`、`置信度` 和 `选择原因`。
3. 若不可回答或置信度不足，调用 `get_all_subspaces` 比较候选；必要时向用户询问一个能区分子空间的问题。
4. 不得因为定位失败就假装选择了默认空间。

## 二、实体链接

1. 从 `ProblemFrame.search_terms` 选择少量、信息密度高的名词或指标短语；不要把整段问题拆成大量无意义词。
2. 调用 `link_entities(keywords=[...], question=原始问题)`。
3. 综合 `score`、`is_relevant`、节点属性和问题上下文选择候选，不只看最高分。
4. 对关键候选调用 `query_entity_properties`，确认名称、标签、定义、口径和标识。
5. 多个候选无法区分且会改变结论时，向用户消歧；否则保留候选及选择理由。

## 三、图驱动探索

从已确认的实体出发，逐层调用 `search_graph(depth=1)`。每次根据返回的 `triples` 决定下一跳，不预设唯一关系名称；已知目标关系时可传 `relationship_types` 缩小范围，但首次探索通常保留完整邻接关系。

探索优先级：

1. 定义、同义词、指标口径和所属知识空间；
2. ProblemClass 或表达问题类别的节点；
3. SolutionClass 或表达解决方案/流程/模板的节点；
4. 方案步骤、输入输出、规则、数据资源和组件资源；
5. 支撑最终结论的其他事实节点。

遍历规则：

- 维护 `visited_entity_ids`，禁止无意义循环；
- 记录每次选择某条边的理由；
- 优先探索与当前目标和 SolutionClass 资源要求相关的节点；
- 默认逐层探索，只有当前证据不足时继续扩展；
- 对承担关键结论或方案定义的节点调用 `query_entity_properties`；
- 不把工具的空结果解释为“不存在”，只能解释为“本次未检索到”。

## 四、方案与资源提取

找到方案节点后，读取其真实属性，兼容但不限于：`steps`、`solutionTemplate`、`procedure`、`input`、`output`、`constraints`、`resourceRequirements`。

不得根据属性名猜测缺失值。若图谱只给出部分方案，应原样保留已知部分，并把缺失字段加入 `gaps`。

继续沿方案节点探索，收集：

- 数据库、表、字段、指标或 SQL 模板；
- 业务规则、计算口径和过滤条件；
- 文件、API、模型、图表模板等执行资源；
- 步骤之间的前置、顺序或依赖关系。

## 输出：GraphContext

```json
{
  "knowledge_space": {
    "name": "",
    "scales": {},
    "confidence": 0.0,
    "evidence": []
  },
  "aligned_entities": [
    {"input": "", "id": "", "name": "", "labels": [], "properties": {}, "selection_reason": ""}
  ],
  "problem_class": {"id": "", "name": "", "properties": {}, "evidence_path": []},
  "solution_class": {
    "id": "",
    "name": "",
    "steps": [],
    "input_spec": {},
    "output_spec": {},
    "resource_requirements": [],
    "properties": {},
    "evidence_path": []
  },
  "resource_nodes": [],
  "business_rules": [],
  "evidence_ledger": [
    {"claim": "", "source_entity_ids": [], "graph_path": [], "tool": ""}
  ],
  "gaps": [],
  "failed_tool_calls": []
}
```

`GraphContext` 中不存在的事实不得在后续执行阶段被当成已知条件。
