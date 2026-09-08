---
name: semantic-align
description: 语义对齐与图谱上下文获取。实体链接（只链接并记录）、获取干系人，沿关系导航找问题类/方案类，读取方案步骤。由 problem-solve 阶段二调用。
inject_layer: developer
---

# 语义对齐与图谱方案检索

## 标准模型映射（先读这一节）

当前图谱采用**知识地图标准模型**。业务概念（干系人、问题类、方案类、指标、术语...）统一存储为 **`KnowledgeUnit`** 节点，**业务类型通过 `resource_type` 字段引用本体层（`MetaClass`）的 uid**。

**业务类型识别**：
- **干系人**：用 `get_stakeholders` 工具获取（内部按 Stakeholder 本体的固定 uid 查询），**不需要手动查 MetaClass uid**；
- **问题类 / 方案类**：**不靠 resource_type 识别**（它们的 resource_type 不是 Problem/Solution），沿 `KNOWLEDGE_RELATION` 关系导航找到（图里干系人→问题类→方案类全部是 KNOWLEDGE_RELATION）。

**关键导航关系**：
- 干系人 → 问题类：沿 `KNOWLEDGE_RELATION` 关系导航
- 问题类 → 方案类：沿 `KNOWLEDGE_RELATION` 关系导航
- **判断是否问题类/方案类：读取节点属性/内容，用 LLM 判断**（关系全是 KNOWLEDGE_RELATION，不靠关系名区分）

**逻辑属性**：KnowledgeUnit 的逻辑属性是独立的 `KnowledgeProperty` 节点，通过 `HAS_LOGICAL_PROPERTY` 边连接；`query_entity_properties` 返回时已包含。

**图谱搜索入口是 `KnowledgeUnit`**。所有查询通过知识地图接口进行，自动带权限过滤，只返回当前用户有权访问的数据。

## 铁律

1. **本阶段只操作图谱，禁止调用任何 SQL/数据类工具。**
2. **每步必须等待工具实际返回。** 禁止推测工具返回内容。
3. **图谱返回空 = "本次未检索到"，≠ "不存在"。**
4. **定位失败不得假装成功。**
5. **方案节点缺失属性 ≠ 你可以补。** 记录为缺口。
6. **`link_entities` 的 keywords 必须直接用 `ProblemFrame.search_terms` 的原词，禁止自行加词、改词或扩展。**

## 可用工具

| 工具 | 用途 |
|------|------|
| `locate_knowledge_space` | 定位知识子空间 |
| `get_all_subspaces` | 定位失败时查看所有候选子空间 |
| `link_entities` | 关键词搜索候选 KnowledgeUnit（链接实体，仅记录到 aligned_entities，不做后续操作） |
| `get_stakeholders` | 获取所有干系人实例（resource_type 指向 Stakeholder 本体） |
| `query_entity_properties` | 获取节点完整属性（含关联的 KnowledgeProperty 逻辑属性） |
| `search_graph` | 一跳邻居查询。返回全部邻居（不剪枝），由 Agent 读属性判断下一步 |
| `find_problem_classes` | 干系人 → 问题类：沿 `KNOWLEDGE_RELATION` 返回邻接问题类候选（含 name/labels/content） |
| `find_solution_classes` | 问题类 → 方案类：沿 `KNOWLEDGE_RELATION` 返回邻接方案类候选（含 name/labels/content） |
| `problem-resolve-path-display` | 将方案步骤展示给用户 |

## 核心探索规则

每一步的标准操作：

```
search_graph(
  start_entity_id=<当前节点ID>,
  depth=1,
  direction="outgoing",
  question=用户原始问题,
)
```

`search_graph` 返回全部邻居、**不做剪枝**，因此不要传 `prune` 等不存在的参数，也不依赖后端任何剪枝。返回的三元组自带 `labels`，根据标签、属性和内容判断下一步目标，不依赖固定的关系名。

---

## 执行流程

### 一、知识空间定位

调用 `locate_knowledge_space(user_question=用户完整问题, app_requirement=用户约束)`。

- 可回答 + 置信度 ≥ 0.5 → 记录
- 不可回答 或 置信度 < 0.5 → 调用 `get_all_subspaces` 比较候选
- 全部不行 → 记录"定位失败"（信息性缺口），**不阻塞**，继续

---

### 二、实体链接（只链接并记录，不做后续操作）

调用 `link_entities(keywords=ProblemFrame.search_terms, question=原始问题)`。

> ⚠️ **keywords 必须原样使用 `ProblemFrame.search_terms`（用户问题里提取的原词），禁止自行添加、改写或扩展词语。** 例：search_terms 是 `["供应商投标数量"]`，就传 `["供应商投标数量"]`，不得传 `"供应商投标数量统计"`。

| score 区间 | 行为 |
|-----------|------|
| ≥ 0.8 且 is_relevant | 直接采纳 |
| 0.5 ~ 0.8 | `query_entity_properties` 确认后采纳 |
| < 0.5 | 换 `search_terms` 中的**其他原词**重试；仍低 → 向用户确认（禁止自行造词） |

把采纳的候选（`id`、`name`、`resource_type`）存入 **`aligned_entities`**。

> ⚠️ **实体链接只做"链接并记录"，不做任何后续操作**（不查询指标/数据、不探索路径）。链接到的实体信息保存在 `GraphContext.aligned_entities`，**方案执行阶段使用**。

---

### 三、找干系人（问题求解链路的起点）

- 调用 **`get_stakeholders`** 工具，获取**所有干系人实例**（工具内部已按 Stakeholder 本体 uid 通过 `resource_type` 查询）；
- 返回后**用 LLM 按 `name` 判断筛选**，选择和问题最相关的干系人；
- 不需要调用 `/third/auth/current-stakeholders` 接口。

---

### 四、问题求解链路（干系人 → 问题类 → 方案类）

> 标准模型中，沿 `KNOWLEDGE_RELATION` 一跳连接的邻居本身就是对应类型单元（位置即类型）。**优先用专用工具直接取类**，候选里的 content 已足够判断，无需再逐跳读全部邻居手工判类：

1. **找问题类**：调用 `find_problem_classes(stakeholder_id=<干系人ID>)` → 一步返回沿 `KNOWLEDGE_RELATION` 邻接的问题类候选，且每个候选已带**完整属性（含 content 与逻辑属性）**，据此直接选出与用户问题相符的问题类，无需再逐个 `query_entity_properties`；
   - 工具未提供或返回空时，再退回 `search_graph` 沿 `KNOWLEDGE_RELATION` 手工探索，对邻居 `query_entity_properties` 读属性/内容判断哪个是问题类、哪个与用户问题相符；
2. **找方案类**：调用 `find_solution_classes(problem_id=<问题类ID>)` → 一步返回该问题的方案类候选（同样已带完整属性），据此选出对应方案类；
   - 退回路径同上：`search_graph` 沿 `KNOWLEDGE_RELATION` 找邻居，读属性/内容判断哪个是方案类；
3. **读方案步骤**：方案类候选返回中已含 content / solutionTemplate / steps 等完整属性；仅当个别字段缺失时，再 `query_entity_properties(方案类ID)` 补取；

> ⚠️ **本阶段不调用 `problem-resolve-path-display`**——该工具在 semantic-align 和 plan-execute 都完成、可一次性展示最终完整路径时才调用。

> ⚠️ **本阶段只做干系人 → 问题类 → 方案类的导航 + 已记录的实体链接，不查询业务数据。** 找不到问题类/方案类邻居 → 记录缺口，如实报告"图谱未沉淀该问题/方案"，不编造。

**遍历规则**：

- 维护 `visited_entity_ids`，禁止无意义循环；
- 记录每次选择某条边的理由；
- 对承担关键结论或方案定义的节点调用 `query_entity_properties`；
- 不把工具的空结果解释为"不存在"，只能解释为"本次未检索到"。

---

### 五、方案与资源提取

找到方案类（由 `KNOWLEDGE_RELATION` 关系导航到的方案节点）后，读取其**方案步骤描述**——内容通常在 `content` 字段，或兼容 `solutionTemplate`/`steps` 等属性。

> ⚠️ **本阶段只读取方案步骤描述，不收集任何数据资源**（数据库、表、字段、SQL 模板、业务规则等）。数据/资源在**方案执行阶段**（plan-execute）按方案步骤逐步获取。

不得根据属性名猜测缺失值。若图谱只给出部分方案，应原样保留已知部分，并把缺失字段加入 `gaps`。

**属性缺失处理**：
- `steps` 或 `solutionTemplate` 缺失 → 检查相邻节点是否有步骤定义；仍无则记录为阻塞性缺口
- `input` / `output` / `constraints` 缺失 → 记录为缺口，执行阶段按方案步骤获取，不在此推断

---

### 六、输出 GraphContext

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

3. **用旧模型关系名导航**
   - 错误：假设存在 `SYNONYM_OF`/`DENOTES`/`HAS_STAKEHOLDER` 等关系（旧模型才有）
   - 正确：根据返回三元组的 `labels` 和 `resource_type` 判断类型，沿实际存在的关系导航

4. **只按最高分选择实体**
   - 错误：link_entities 返回 3 个候选，只看 score 最高的
   - 正确：综合 score、is_relevant、节点属性和问题上下文判断

5. **无限探索不设预算**
   - 错误：已探索 5 跳仍未找到方案类，继续扩展
   - 正确：3 跳后仍未找到 → 记录缺口，交付不完整的 GraphContext 并标记 gaps

6. **猜测缺失属性**
   - 错误：方案节点无 steps 属性 → 根据节点名称推断步骤
   - 正确：检查相邻节点，仍缺失则记录缺口
</Anti_Patterns>

<Examples>
<Good>
ProblemFrame.search_terms: ["电子采购", "采购金额"]
Agent 调用 link_entities(keywords=["电子采购"], question="电子采购信息汇总表是什么")
返回: [{name: "电子采购信息汇总表", id: "cc50b11e-...", score: 0.9, is_relevant: true, labels: ["KnowledgeUnit"]}]
Agent: 采纳，调用 query_entity_properties 确认 content 和 resource_type，判断它是"业务对象"类单元。
</Good>

<Good>
Agent 调用 search_graph 从单元 A 出发，返回 15 条 triples。
其中一条尾节点是 KnowledgeProperty（逻辑属性），一条是 KnowledgeUnit（沿关系导航到的方案类）。
Agent: 关注方案类邻居，记录"该单元关联到方案类单元"，维护 visited_entity_ids = [A, 方案单元]。
</Good>

<Good>
Agent 调用 locate_knowledge_space 返回置信度 0.3，"可回答"=false。
Agent 调用 get_all_subspaces 比较候选，向用户确认所属领域。
</Good>

<Bad>
Agent 在 search_graph 返回空结果后记录"该实体没有关联的方案节点"。
Why bad: 工具空结果只能解释为"本次未检索到"，不能断言"不存在"。
</Bad>

<Bad>
Agent 假设存在 `SYNONYM_OF`/`DENOTES` 关系来导航（旧模型的固定链路）。
Why bad: 标准模型没有这些关系，应按返回三元组的 labels 和 resource_type 判断类型。
</Bad>

<Bad>
Agent 发现方案节点的 steps 属性为空，根据节点名称"采购方案"自行补出 3 个步骤。
Why bad: 不得根据属性名猜测缺失值。应检查相邻节点或记录为 gaps。
</Bad>
</Examples>

<Escalation_And_Stop_Conditions>
- **知识空间定位失败且无合适候选**: 向用户确认所属领域，不要随机选择默认空间
- **所有实体链接 score < 0.5**: 向用户确认关键词含义，或请求提供更精确的表达
- **3 跳探索后仍未找到方案类**: 记录为知识缺口，报告"图谱当前没有该问题的解决方案"
- **关键属性缺失**（如方案节点没有 steps 或 content 字段）: 记录缺口，尝试从相邻节点获取；仍缺失时报告
- **同一个工具连续失败 3 次**: 停止重试，记录 failed_tool_calls，使用已有的部分信息继续
- **知识空间置信度 < 0.5 且无可区分的候选**: 向用户展示候选并请求选择
</Escalation_And_Stop_Conditions>

<Final_Checklist>
- [ ] 知识空间已成功定位（或定位失败原因已记录在 gaps 中）
- [ ] 关键搜索词已链接到至少一个 KnowledgeUnit（或全部低置信度已记录）
- [ ] 已尝试探索问题类和方案类（到达预算上限后已记录缺口）
- [ ] evidence_ledger 记录了关键事实的来源路径
- [ ] 方案节点的真实属性被原样保留，未根据属性名猜测缺失值
- [ ] gaps 中区分了阻塞性缺口和信息性缺口
- [ ] failed_tool_calls 记录了所有失败的工具调用及其错误
- [ ] visited_entity_ids 已维护，未出现无意义循环
- [ ] GraphContext 结构完整，可交付给证据门检查和 plan-execute
</Final_Checklist>
