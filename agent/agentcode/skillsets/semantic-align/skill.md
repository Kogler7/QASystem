---
noteId: "4594962085b011f1a953d7289dee1b45"
tags: []
name: "semantic-align"
description: "将用户表达对齐到知识图谱，并从图谱中检索问题类、方案类、规则与执行资源；不依赖意图识别或方案规划 MCP。"
inject_layer: "developer"
version: "2.0.0"

---

# 语义对齐与图谱方案检索

<Purpose>
将自然语言请求转换为可追溯的 GraphContext 结构体。Agent 负责导航判断，图谱负责提供全部业务语义和方案知识。输出是 plan-execute 的输入。
</Purpose>

<Use_When>
- 用户请求需要从知识图谱中查找业务实体、指标或方案
- 需要确认用户口语化表达对应的标准术语
- 需要探索图谱中的 ProblemClass 和 SolutionClass 来制定执行路径
- 作为 problem-solve 流程的阶段二被调用
</Use_When>

<Do_Not_Use_When>
- 用户请求不涉及知识图谱（如纯计算、闲聊）
- 已经在当前会话中完成了对齐且有缓存的 GraphContext 可直接复用
- 用户明确指定了所有需要的实体 ID、方案 ID 和资源路径
</Do_Not_Use_When>

<Why_This_Exists>
用户使用自然语言描述需求，而知识图谱使用结构化节点和关系存储业务知识。本 skill 在这两者之间建立映射：
1. 将口语化关键词链接到图谱实体
2. 通过逐层探索从实体导航到问题类和方案类
3. 收集方案执行所需的所有资源节点
4. 记录完整的证据链和知识缺口
</Why_This_Exists>

<Integration>
本 skill 是三层流水线的第二层：

```
problem-solve（编排层）
  → semantic-align（本 skill）→ 产出 GraphContext
    → plan-execute（执行层）→ 消费 GraphContext
```

**上游输入**：`ProblemFrame`（来自 problem-solve 阶段一），包含 goal、target_objects、constraints、search_terms、ambiguities

**下游输出**：`GraphContext`（交付给 problem-solve 阶段三做证据门检查，然后传递给 plan-execute）

**可被独立调用**：当已经有明确的 ProblemFrame 且只需要图谱检索时，可单独调用本 skill。但通常由 problem-solve 编排调用。
</Integration>

<Execution_Policy>
- 五个环节顺序推进，但每步内部允许回退：知识空间定位 → 实体链接 → 图探索 → 方案提取 → 输出 GraphContext
- 图谱返回空结果不等于"不存在"，只能解释为"本次未检索到"
- 遇到多个候选无法区分且会改变结论时，向用户消歧；否则保留候选及选择理由
- 避免无意义循环：维护 visited_entity_ids
- 每次选择某条边继续探索时记录理由
- 探索预算：默认最多 3 跳从实体到 SolutionClass；3 跳后仍未找到则记录缺口
</Execution_Policy>

## 可用原子工具

| 工具 | 用途 |
| --- | --- |
| `qasystem-tools__locate_knowledge_space` | 根据问题和应用约束定位知识子空间 |
| `qasystem-tools__get_all_subspaces` | 定位失败或需要消歧时查看候选子空间 |
| `qasystem-tools__link_entities` | 将一个或多个关键词链接到候选图谱实体 |
| `qasystem-tools__query_entity_properties` | 获取节点完整属性 |
| `qasystem-tools__search_graph` | 确定性查询一跳关系；可按 `relationship_types`、`direction`、`limit` 过滤 |

所有工具结果都采用 `{code, message, data, error}`。只有 `code == 0` 时才能把 `data` 作为证据；失败信息必须保留并用于调整下一步。

<Steps>

### 一、知识空间定位

1. 用原始问题调用 `locate_knowledge_space`，把用户明确给出的领域、场景、时间、组织等放入 `app_requirement`。
2. 检查 `可回答`、`目标子空间`、`选中的刻度`、`置信度` 和 `选择原因`。
3. 若不可回答或置信度不足，调用 `get_all_subspaces` 比较候选；必要时向用户询问一个能区分子空间的问题。
4. 不得因为定位失败就假装选择了默认空间。

**失败处理**：
- 第 1 次定位失败 → 调用 `get_all_subspaces`，比较候选子空间与问题的匹配度
- 候选子空间中有明显匹配项 → 选择最匹配的，记录选择理由
- 候选子空间中无明显匹配项 → 向用户展示候选列表，请求确认
- 无候选子空间 → 记录为知识缺口，在 gaps 中说明"无法定位知识空间"

### 二、实体链接

1. 从 `ProblemFrame.search_terms` 选择少量、信息密度高的名词或指标短语；不要把整段问题拆成大量无意义词。
2. 调用 `link_entities(keywords=[...], question=原始问题)`。
3. 综合 `score`、`is_relevant`、节点属性和问题上下文选择候选，不只看最高分。具体判断规则：
   - score ≥ 0.8 且 is_relevant → 直接采纳
   - 0.5 ≤ score < 0.8 → 查看节点属性确认是否相关
   - score < 0.5 → 标记为低置信度，尝试换关键词重试
4. 对关键候选调用 `query_entity_properties`，确认名称、标签、定义、口径和标识。
5. 多个候选无法区分且会改变结论时，向用户消歧；否则保留候选及选择理由。

**消歧决策矩阵**：
| 场景 | 行为 |
|------|------|
| 多个候选，选哪个会改变方案选择 | 向用户消歧 |
| 多个候选，选哪个不影响方案 | 保留最高分，记录其他候选 |
| 全部候选 score < 0.5 | 向用户确认关键词含义 |
| 单个候选 score ≥ 0.8 | 直接采纳 |

### 三、图驱动探索

从已确认的实体出发，逐层调用 `search_graph(depth=1)`。每次根据返回的 `triples` 决定下一跳，不预设唯一关系名称；已知目标关系时可传 `relationship_types` 缩小范围，但首次探索通常保留完整邻接关系。

**探索优先级**：

1. 定义、同义词、指标口径和所属知识空间；
2. ProblemClass 或表达问题类别的节点；
3. SolutionClass 或表达解决方案/流程/模板的节点；
4. 方案步骤、输入输出、规则、数据资源和组件资源；
5. 支撑最终结论的其他事实节点。

**遍历规则**：

- 维护 `visited_entity_ids`，禁止无意义循环；
- 记录每次选择某条边的理由；
- 优先探索与当前目标和 SolutionClass 资源要求相关的节点；
- 默认逐层探索，只有当前证据不足时继续扩展；
- 对承担关键结论或方案定义的节点调用 `query_entity_properties`；
- 不把工具的空结果解释为"不存在"，只能解释为"本次未检索到"。

**探索深度管理**：

| 跳数 | 节点类型 | 未找到时的行为 |
|------|---------|---------------|
| 1 | 定义、同义词、知识空间归属 | 继续第 2 跳 |
| 2 | ProblemClass | 尝试不同关系类型；仍无则记录缺口 |
| 3 | SolutionClass | 记录"图谱当前没有该问题的解决方案"|
| 4+ | 仅在已有 SolutionClass 时探索其资源节点 | 方案资源不足则记录缺口 |

### 四、方案与资源提取

找到方案节点后，读取其真实属性，兼容但不限于：`steps`、`solutionTemplate`、`procedure`、`input`、`output`、`constraints`、`resourceRequirements`。

不得根据属性名猜测缺失值。若图谱只给出部分方案，应原样保留已知部分，并把缺失字段加入 `gaps`。

继续沿方案节点探索，收集：

- 数据库、表、字段、指标或 SQL 模板；
- 业务规则、计算口径和过滤条件；
- 文件、API、模型、图表模板等执行资源；
- 步骤之间的前置、顺序或依赖关系。

**属性缺失处理**：
- `steps` 或 `solutionTemplate` 缺失 → 检查相邻节点是否有步骤定义；仍无则记录为阻塞性缺口
- `input` 缺失 → 从 ProblemFrame.constraints 和 resource_nodes 推断可用的输入；无法推断则记录缺口
- `constraints` 缺失 → 不阻塞，但标记为"方案约束未知"

### 五、输出 GraphContext

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

**输出前自检**：
- [ ] knowledge_space.confidence > 0 或有明确的定位失败记录？
- [ ] aligned_entities 中每个实体都有 selection_reason？
- [ ] evidence_ledger 中每条 claim 都有 source_entity_ids 或 graph_path？
- [ ] gaps 中是否区分了阻塞性缺口（影响执行）和信息性缺口（不影响执行）？

`GraphContext` 中不存在的事实不得在后续执行阶段被当成已知条件。

</Steps>

<Anti_Patterns>
以下是必须避免的常见错误模式：

1. **定位失败后假装成功**
   - 错误：locate_knowledge_space 返回置信度 0.3 且"可回答"=false，Agent 忽略并声称"已定位到 XX 知识空间"
   - 正确：调用 get_all_subspaces 或向用户确认

2. **空结果断言不存在**
   - 错误：search_graph 返回空 → "该实体没有关联的方案节点"
   - 正确："本次未检索到关联的方案节点"，可尝试不同关系类型或更大探索深度

3. **关键词过度拆分**
   - 错误：把"华东区上周销售额"拆成 ["华东", "区", "上周", "销售", "额"]
   - 正确：保留有意义的短语 ["华东区", "销售额"]，时间约束放入 app_requirement

4. **只按最高分选择实体**
   - 错误：link_entities 返回 3 个候选，只看 score 最高的
   - 正确：综合 score、is_relevant、节点属性和问题上下文判断；有时 score 第二但标签更匹配

5. **无限探索不设预算**
   - 错误：已探索 5 跳仍未找到 SolutionClass，继续扩展
   - 正确：3 跳后仍未找到 → 记录缺口，交付不完整的 GraphContext 并标记 gaps

6. **猜测缺失属性**
   - 错误：方案节点无 steps 属性 → 根据节点名称推断步骤
   - 正确：检查相邻节点，仍缺失则记录缺口
</Anti_Patterns>

<Examples>
<Good>
ProblemFrame.search_terms: ["华东区", "销售额"]
Agent 调用 link_entities(keywords=["华东区", "销售额"], question="上周华东区销售额是多少")
返回: [{name: "华东大区", id: 123, score: 0.95, is_relevant: true}, {name: "销售额", id: 456, score: 0.88, is_relevant: true}]
Agent: 两个候选分数高且与问题上下文一致 → 采纳，调用 query_entity_properties 确认属性，继续搜索
</Good>

<Good>
Agent 调用 search_graph 从实体 123 出发，返回 15 条 triples。
其中 (实体123)-[:HAS_METRIC]->(指标456) 和 (指标456)-[:SOLVED_BY]->(方案789) 与目标直接相关。
Agent: 选择这两条边继续探索，记录其他 13 条为"与当前目标无关，跳过"。维护 visited_entity_ids = [123, 456]。
</Good>

<Good>
Agent 调用 locate_knowledge_space 返回置信度 0.3，"可回答"=false。
Agent 调用 get_all_subspaces，获得 ["销售统计", "库存分析", "客户管理"] 三个候选。
Agent: 用户问题涉及"销售额"→ 选择"销售统计"子空间，记录选择理由和低置信度风险。
</Good>

<Bad>
Agent 调用 locate_knowledge_space 返回置信度 0.3 且 "可回答" = false。
Agent 忽略该结果，继续说"已定位到销售知识空间"。
Why bad: 定位失败时不得假装成功。应调用 get_all_subspaces 或向用户确认。
</Bad>

<Bad>
Agent 在 search_graph 返回空结果后记录"该实体没有关联的方案节点"。
Why bad: 工具空结果只能解释为"本次未检索到"，不能断言"不存在"。
</Bad>

<Bad>
Agent 发现方案节点的 steps 属性为空，根据节点名称"销售统计方案"自行补出 3 个步骤。
Why bad: 不得根据属性名猜测缺失值。应检查相邻节点或记录为 gaps。
</Bad>
</Examples>

<Escalation_And_Stop_Conditions>
- **知识空间定位失败且无合适候选**: 向用户确认所属领域，不要随机选择默认空间
- **所有实体链接 score < 0.5**: 向用户确认关键词含义，或请求提供更精确的表达
- **3 跳探索后仍未找到 SolutionClass**: 记录为知识缺口，报告"图谱当前没有该问题的解决方案"
- **关键属性缺失**（如方案节点没有 steps 或 procedure 字段）: 记录缺口，尝试从相邻节点获取；仍缺失时报告
- **同一个工具连续失败 3 次**: 停止重试，记录 failed_tool_calls，使用已有的部分信息继续
- **知识空间置信度 < 0.5 且无可区分的候选**: 向用户展示候选并请求选择
</Escalation_And_Stop_Conditions>

<Final_Checklist>
- [ ] 知识空间已成功定位（或定位失败原因已记录在 gaps 中）
- [ ] 关键搜索词已链接到至少一个图谱实体（或全部低置信度已记录）
- [ ] 已尝试探索 ProblemClass 和 SolutionClass（到达预算上限后已记录缺口）
- [ ] evidence_ledger 记录了关键事实的来源路径
- [ ] 方案节点的真实属性被原样保留，未根据属性名猜测缺失值
- [ ] gaps 中区分了阻塞性缺口和信息性缺口
- [ ] failed_tool_calls 记录了所有失败的工具调用及其错误
- [ ] visited_entity_ids 已维护，未出现无意义循环
- [ ] GraphContext 结构完整，可交付给证据门检查和 plan-execute
</Final_Checklist>
