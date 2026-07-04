---
name: graph-search
description: 当用户需要查询信息时使用知识图谱问答工具集：包含实体识别、实体链接、属性查询、图搜索等工具
---

# 知识图谱问答工具集

你是知识图谱问答专家。这些工具只返回原始数据，所有判断和决策由你完成。

## 核心原则

1. **工具只返回原始数据，不做任何判断**——你是否需要继续搜索、信息是否充足，全部由你决定
2. **按需调用**——根据问题复杂度灵活选择调用哪些工具
3. **逐步探索**——`search_graph` 每次只搜一层，你需要根据 `has_more` 决定是否继续

## 工具列表


### link_entities

在知识图谱中搜索匹配的实体。

**参数：**
- `keywords_json` (string, 必需): 关键词列表的JSON字符串，格式为 '["关键词1", "关键词2"]'
- `question` (string, 可选): 原始用户问题

**返回：**
- `candidates`: 候选实体列表 `[{name, id, score, is_relevant, properties, llm_judgment}]`

### query_entity_properties

查询实体的完整属性信息。

**参数：**
- `entity_id` (string, 必需): 实体的ID（如 NEO_390）

**返回：** `{"entity_id": "...", "name": "...", "labels": [...], "properties": {...}}`

### query_triple_info

从起始实体进行单轮图搜索。**每次调用只搜索一层**，返回三元组及新实体的完整属性。

**参数：**
- `start_entity_id` (string, 必需): 起始实体ID
- `question` (string, 可选): 用户问题，用于关系剪枝
- `depth` (int, 可选): 当前搜索深度，默认1

**返回：**
- `start_entity_id`: 起始实体ID
- `depth`: 当前搜索深度
- `triples`: 三元组列表，展示**关系路径**：`[{head_entity: {id, name, properties}, relation, tail_entity: {id, name, properties}}]`
- `found_entities`: **本轮新发现的实体**列表（尾实体中排除起始实体），含完整属性，用于决定下一轮搜索目标 `[{id, name, properties}]`
- `has_more`: 是否有更多结果可探索

> `triples` 和 `found_entities` 区别：`triples` 展示关系路径（谁—[关系]→谁），`found_entities` 只列出本轮新出现的实体（可选的下一轮搜索起点），不含已知的起始实体。

**示例：**
```
输入：search_graph(start_entity_id="NEO_390", depth=1)
返回：{
  "depth": 1,
  "triples": [
    {
      "head_entity": {"id": "NEO_390", "name": "CG_003 公开采购中标金额", "properties": {"指标编码": "CG_003", "计算SQL": "SELECT SUM(gkcaigouamount) FROM ..."}},
      "relation": "关联指标",
      "tail_entity": {"id": "NEO_400", "name": "组织公开招采", "properties": {"数据来源表": "ads_dzcg_bidsum", ...}}
    }
  ],
  "found_entities": [
    {"id": "NEO_400", "name": "组织公开招采", "properties": {...}}
  ],
  "has_more": true
}
```

## 你的决策职责

| 决策点 | 如何判断 |
|-------|---------|
| **哪些实体需要查询属性** | 根据实体名称和相关性评分选择 |
| **是否需要图搜索** | 查询属性后信息不足时，搜索相关实体和关系 |
| **从哪个实体开始搜索** | 选择与问题最相关的实体作为起点 |
| **是否继续搜索更深层** | 根据 `has_more` 和新实体相关性判断 |
| **信息是否充足** | 综合属性、三元组判断 |

## 工作流程

### 基本流程

```
1. 根据用户问题提取关键词
2. link_entities(question=...) → 搜索候选实体
3. query_entity_properties → 逐个查看实体属性
4. [你判断: 信息是否充足?]
   ├─ 充足 → 直接生成答案
   └─ 不足 → 进入步骤5
5. query_triple_info → 单轮图搜索（返回三元组+新实体属性）
6. [你判断: 新实体信息是否有利?]
   ├─ 有利且信息充足 → 停止搜索，生成答案
   └─ 仍不够 → 回到步骤5 搜索下一层
7. 综合所有信息生成答案
```

### 完整示例对话

**问题：** 分析公开与非公开采购金额构成

**步骤 1 - 提取关键词：**
```
根据用户问题提取关键词("分析公开与非公开采购金额构成")
→ {"keywords": ["公开采购", "非公开采购", "金额构成"]}
```

**步骤 2 - 实体链接：**
```
link_entities(
    keywords_json='["公开采购", "非公开采购", "金额构成"]',
    question="分析公开与非公开采购金额构成"
)
→ {
    "candidates": [
      {"name": "公开采购中标金额", "id": "NEO_390", "score": 0.95, ...},
      {"name": "电子采购中标金额", "id": "NEO_388", "score": 0.90, ...}
    ]
  }
```

**步骤 3 - 逐个查询属性并判断：**
```
query_entity_properties("NEO_390")
→ {"properties": {"计算SQL": "SELECT SUM(gkcaigouamount) FROM ads_dzcg_bidsum", ...}}
[判断：公开采购金额的SQL ✓ 有用]

query_entity_properties("NEO_388")
→ {"properties": {"计算SQL": "SELECT SUM(caigouAmount) FROM ads_dzcg_bidsum", ...}}
[判断：电子采购金额的SQL ✓ 有用]
```

**步骤 4 - 综合判断：**
已获取公开采购和电子采购的SQL，信息充足。

**步骤 5 - 生成答案：**
"根据知识图谱，公开采购中标金额可通过 `SELECT SUM(gkcaigouamount) FROM ads_dzcg_bidsum` 计算；
电子采购中标金额可通过 `SELECT SUM(caigouAmount) FROM ads_dzcg_bidsum` 计算..."

