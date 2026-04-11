# 问题求解与智能问数 API 约定（整合版）

本文档按业务编排顺序整合：**问题求解**（问题定义 → 方案规划 → 组件匹配）与 **智能问数**（意图识别 → 上下文获取与指标匹配 → SQL 转换 → SQL 执行 → 图表可视化）。各节接口形态沿用原 Mock 约定；若字段命名在不同模块间存在差异，以各节请求/响应示例为准，编排层负责映射。

---

## 整体流程顺序


| 阶段       | 模块            | 说明                                                      |
| -------- | ------------- | ------------------------------------------------------- |
| **问题求解** | 1. 问题定义       | 将自然语言问题结构化，并产出知识链接与评估结果                                 |
|          | 2. 方案规划       | 基于查询（可与问题定义输出 JSON 衔接）生成方案模型                            |
|          | 3. 组件匹配       | 将方案描述匹配为可执行工具及参数                                        |
| **智能问数** | 1. 意图识别       | 输出业务域、场景、对象、意图类型与用户目标等                                  |
|          | 2. 上下文获取与指标匹配 | 输入结构化意图，输出指标/业务叙述类答案说明                                  |
|          | 3. SQL 转换     | Agent/Dify 工具：`text2sql`，由自然语言生成可执行 SQL（字符串）            |
|          | 4. SQL 执行     | Agent/Dify 工具：`sql_execute`，在指定库上执行 SQL 并返回 `result` 行集 |
|          | 5. 图表可视化      | 根据查询结果与展示类型生成图表结构与解读文案                                  |


---

## 一、问题求解

### 1. 问题定义

- **接口**：`POST /api/component/answer-generation/tools/problem-definition`

```http
POST /api/component/answer-generation/tools/problem-definition

{
  "question": "公开采购与非公开采购金额构成如何？"
}
```

**请求参数**：


| 参数         | 类型     | 必填  | 说明          |
| ---------- | ------ | --- | ----------- |
| `question` | string | 是   | 用户输入的自然语言问题 |


**响应示例**：

```json
{
  "success": true,
  "problem_model": {
    "问题ID": "QKB-001",
    "问题描述": "公开采购与非公开采购金额构成如何？",
    "问题类型": "描述型问题",
    "约束": ["需基于公开数据源"],
    "目标对象": "采购金额构成",
    "干系人": ["采购部门", "财务部门"],
    "目标": ["明确金额分配比例"],
    "当前状态": "分析阶段",
    "创建时间": "2026-04-08T00:54:30.393365",
    "相关实体": ["公开采购", "非公开采购", "金额构成"]
  },
  "link_result": {
    "linked_entities": [
      {
        "qid": "NEO_390",
        "properties": {
          "name": "CG_003 公开采购中标金额",
          "指标编码": "CG_003",
          "数据来源表": "ads_dzcg_bidsum"
        }
      }
    ],
    "all_candidates": [
      {
        "qid": "NEO_390",
        "properties": {
          "name": "CG_003 公开采购中标金额"
        },
        "relevance_score": 0.95,
        "can_answer": true,
        "reason": "指标名称直接包含'公开采购'且计算SQL明确统计金额"
      }
    ]
  },
  "data": {
    "pass": true,
    "understanding": "问题评估通过，信息完整"
  }
}
```

---

### 2. 方案规划

- **模块**：方案生成  
- **接口名**：方案生成

编排说明：请求参数形态与 **意图识别** 一致（`query` 为原始文本或内嵌 JSON 字符串）。典型用法是将 **问题定义** 响应中的 `problem_model`、`link_result` 等序列化后作为 `query` 传入；接口内部可先执行意图识别，再基于 `intent_name`、`domain` 等生成方案。

**请求参数**：


| 参数名     | 类型     | 必填  | 说明                                |
| ------- | ------ | --- | --------------------------------- |
| `query` | string | 是   | 用户原始查询（支持纯文本或 JSON 字符串，与意图识别接口相同） |


**请求示例**：

```json
{
  "query": "{
    \"success\": true,
    \"problem_model\": {
      \"问题ID\": \"QKB-001\",
      \"问题描述\": \"公开采购与非公开采购金额构成如何？\",
      \"问题类型\": \"描述型问题\",
      \"约束\": [\"需基于公开数据源\"],
      \"目标对象\": \"采购金额构成\",
      \"干系人\": [\"采购部门\", \"财务部门\"],
      \"目标\": [\"明确金额分配比例\"],
      \"当前状态\": \"分析阶段\",
      \"创建时间\": \"2026-04-09T00:54:30.393365\",
      \"相关实体\": [\"公开采购\", \"非公开采购\", \"金额构成\"]
    },
    \"link_result\": {
      \"linked_entities\": {
        \"NEO_390\": \"CG_003 公开采购中标金额\"
      }
    }
  }"
}
```

**工作流程**：

1. 使用相同 `query` 得到意图相关字段（与意图识别一致）。
2. 基于意图结果生成 **方案模型** 列表。

**返回参数**：


| 参数名                  | 类型      | 说明     |
| -------------------- | ------- | ------ |
| `success`            | boolean | 是否成功   |
| `data`               | object  | 返回数据   |
| `data.understanding` | string  | 理解说明   |
| `data.pass`          | boolean | 是否通过   |
| `data.方案模型`          | array   | 方案模型列表 |
| `data.方案模型[].方案ID`   | string  | 方案唯一标识 |
| `data.方案模型[].方案类别`   | string  | 方案类别   |
| `data.方案模型[].方案目标`   | string  | 方案目标   |
| `data.方案模型[].输入`     | array   | 输入列表   |
| `data.方案模型[].输出`     | array   | 输出列表   |
| `data.方案模型[].约束`     | array   | 约束列表   |
| `data.方案模型[].控制逻辑`   | string  | 控制逻辑   |
| `data.方案模型[].置信度`    | number  | 置信度    |


**响应示例**：

```json
{
  "success": true,
  "data": {
    "understanding": "计划存在",
    "pass": true,
    "方案模型": [
      {
        "方案ID": "SOL-001",
        "方案类别": "分析类问题解决方案",
        "方案目标": "针对问题 '公开采购与非公开采购金额构成如何？' 的解决方案",
        "输入": [
          "公开采购与非公开采购金额构成如何？"
        ],
        "输出": [
          "{是}。基于给定的知识三元组和我的知识，公开采购与非公开采购金额构成可以通过电子采购中标金额和电子采购品类中标金额等指标来关联和汇总。"
        ],
        "约束": [
          "基于知识图谱检索",
          "遵循最佳实践"
        ],
        "控制逻辑": null,
        "置信度": 0.9
      }
    ]
  }
}
```

---

### 3. 组件匹配

- **模块**：方案到物理组件匹配  
- **接口名**：方案到工具匹配

**请求参数**：


| 参数名    | 类型     | 必填  | 说明                    |
| ------ | ------ | --- | --------------------- |
| `plan` | string | 是   | 方案描述（可由方案规划产出摘要或人工拼装） |


**请求示例**：

```json
{
  "plan": "采购管理域的看构成计划方案：1.上下文获取与指标匹配：输入结构化意图，输出用指标描述的业务；2.SQL转换：输入指标描述的业务，输出SQL描述的业务；3.SQL执行：输入SQL，输出指标计算结果；4.图表可视化与答案生成：输入计算结果，输出图表+文字内容描述。"
}
```

**返回参数**：


| 参数名                               | 类型      | 说明      |
| --------------------------------- | ------- | ------- |
| `success`                         | boolean | 是否成功    |
| `data`                            | object  | 返回数据    |
| `data.understanding`              | string  | 理解说明    |
| `data.pass`                       | boolean | 是否通过    |
| `data.tools`                      | array   | 匹配的工具列表 |
| `data.tools[].name`               | string  | 工具名称    |
| `data.tools[].parameters`         | array   | 工具参数    |
| `data.tools[].parameters[].name`  | string  | 参数名称    |
| `data.tools[].parameters[].value` | string  | 参数值     |


**响应示例**：

```json
{
  "success": true,
  "data": {
    "understanding": "工具生成成功",
    "pass": true,
    "tools": [
      {
        "name": "Text to SQL",
        "parameters": [
          {
            "name": "query",
            "value": "采购金额构成分析"
          },
          {
            "name": "table",
            "value": "purchase_table"
          },
          {
            "name": "DB URI",
            "value": "mysql://user:pass@localhost:3306/purchase_db"
          }
        ]
      },
      {
        "name": "SQL Execute",
        "parameters": [
          {
            "name": "sql",
            "value": "SELECT purchase_type, SUM(amount) FROM purchase_table GROUP BY purchase_type"
          },
          {
            "name": "DB URI",
            "value": "mysql://user:pass@localhost:3306/purchase_db"
          }
        ]
      }
    ]
  }
}
```

**问题求解链路小结**（可与编排器对照）：

1. 问题定义 → 结构化问题与图谱链接
2. 方案规划 → `query` 承接上一步 JSON，产出 `方案模型`
3. 组件匹配 → `plan` 文本描述 → `tools` 列表（名称与参数供后续真实调用映射）

---

## 二、智能问数

### 1. 意图识别

- **模块**：意图识别  
- **接口名**：意图识别

**请求参数**：


| 参数名     | 类型     | 必填  | 说明                                            |
| ------- | ------ | --- | --------------------------------------------- |
| `query` | string | 是   | 用户原始查询（支持文本或 JSON 字符串；JSON 时常与问题定义/方案规划上下游一致） |


**请求示例（JSON 字符串形态）**：

```json
{
  "query": "{
    \"success\": true,
    \"problem_model\": {
      \"问题ID\": \"QKB-001\",
      \"问题描述\": \"公开采购与非公开采购金额构成如何？\",
      \"问题类型\": \"描述型问题\",
      \"约束\": [\"需基于公开数据源\"],
      \"目标对象\": \"采购金额构成\",
      \"干系人\": [\"采购部门\", \"财务部门\"],
      \"目标\": [\"明确金额分配比例\"],
      \"当前状态\": \"分析阶段\",
      \"创建时间\": \"2026-04-09T00:54:30.393365\",
      \"相关实体\": [\"公开采购\", \"非公开采购\", \"金额构成\"]
    },
    \"link_result\": {
      \"linked_entities\": {
        \"NEO_390\": \"CG_003 公开采购中标金额\"
      }
    }
  }"
}
```

**返回参数**：


| 参数名           | 类型     | 说明      |
| ------------- | ------ | ------- |
| `intent_id`   | string | 意图唯一标识  |
| `domain`      | string | 意图所属业务域 |
| `scenario`    | string | 意图所属场景  |
| `object`      | string | 意图对象    |
| `intent_name` | string | 意图名称    |
| `user_goal`   | string | 用户核心目标  |


**响应示例**：

```json
{
  "intent_id": "INTENT-001",
  "domain": "采购管理域",
  "scenario": "采购分析",
  "object": "采购金额",
  "intent_name": "看趋势",
  "user_goal": "分析采购金额的月度变化趋势"
}
```

---

### 2. 上下文获取与指标匹配

- **接口**：`POST /api/component/answer-generation/tools/answer-generation`

编排说明：本节入参为结构化 `**intent_output`**（字段名为 PascalCase）。编排层需将意图识别结果映射为下列结构（例如 `domain` → `Domain`，`intent_name` → `IntentType`，`user_goal` → `Goal` 等），以保证与 Mock 一致。

```http
POST /api/component/answer-generation/tools/answer-generation

{
  "intent_output": {
    "Domain": "采购管理域",
    "Scenario": "采购结构分析",
    "Object": "采购金额结构",
    "Goal": "公开采购与非公开采购金额构成如何？",
    "IntentType": "看构成"
  }
}
```

**请求参数**：


| 参数              | 类型     | 必填  | 说明                       |
| --------------- | ------ | --- | ------------------------ |
| `intent_output` | object | 是   | 意图识别的结构化输出，须包含 `Goal` 字段 |


**intent_output 字段**：


| 字段           | 类型     | 说明       |
| ------------ | ------ | -------- |
| `Domain`     | string | 业务域      |
| `Scenario`   | string | 业务场景     |
| `Object`     | string | 业务对象     |
| `Goal`       | string | 用户的问题/目标 |
| `IntentType` | string | 意图类型     |


**响应示例**：

```json
{
  "success": true,
  "answer": "根据业务模型识别到该问题对应的业务活动是：公开采购与非公开采购金额对比分析\n根据指标模型匹配：CG_003 公开采购中标金额\n推导：公开采购金额可通过指标CG_003计算得出（SQL: SELECT SUM(gkcaigouamount) AS value FROM ads_dzcg_bidsum WHERE year = #{year}），非公开采购金额需补充相关指标数据\n因此，业务被转化为用指标描述的业务，最终答案：公开采购金额通过CG_003指标计算，非公开采购金额需补充对应指标数据才能完成构成分析"
}
```

---

### 3. SQL 转换（Dify 工具：database）

本节为 **SQL 转换**独立模块。Agent **工具调用**形态：外层以工具名为键、值为具体入参；响应同样以工具名为键。与上一节 HTTP 组件接口并存，由编排层选择调用 Dify/Agent 工具或后端 HTTP。

- **工具名**：`text2sql`  
- **说明**：根据数据库连接与自然语言问题，生成可执行 SQL（返回为字符串，常含推理说明与最终 SQL 片段）。

**请求参数（`text2sql` 对象内字段）**：


| 参数名      | 类型     | 必填  | 说明                                                   |
| -------- | ------ | --- | ---------------------------------------------------- |
| `db_uri` | string | 是   | 数据库连接 URI，如 `mysql+pymysql://user:pass@host:port/db` |
| `query`  | string | 是   | 用户自然语言问句（中文或英文均可）                                    |


**请求示例**：

```json
{
  "text2sql": {
    "db_uri": "mysql+pymysql://user:123456@host:port/rail_assistant",
    "query": "公开采购与非公开采购金额构成如何"
  }
}
```

**返回参数**：


| 参数名        | 类型     | 说明                                           |
| ---------- | ------ | -------------------------------------------- |
| `text2sql` | string | 模型输出整段文本：可含推理说明及最终 **SQL**。调用方需解析或截取可执行 SQL。 |


**返回示例（字符串内容形态）**：

```text
（可选：模型对表名、字段含义、聚合与占比逻辑的推理说明……）

SELECT 
  SUM(CAST(gkcaigouamount AS DECIMAL)) AS public_procurement_amount, 
  SUM(CAST(caigouAmount AS DECIMAL)) - SUM(CAST(gkcaigouamount AS DECIMAL)) AS non_public_procurement_amount, 
  SUM(CAST(caigouAmount AS DECIMAL)) AS total_procurement_amount, 
  (SUM(CAST(gkcaigouamount AS DECIMAL)) / SUM(CAST(caigouAmount AS DECIMAL))) * 100 AS public_procurement_percentage, 
  ((SUM(CAST(caigouAmount AS DECIMAL)) - SUM(CAST(gkcaigouamount AS DECIMAL))) / SUM(CAST(caigouAmount AS DECIMAL))) * 100 AS non_public_procurement_percentage 
FROM ads_dzcg_bidsum 
WHERE caigouAmount IS NOT NULL AND gkcaigouamount IS NOT NULL 
LIMIT 5;
```

**JSON 包裹后的返回示例**：

```json
{
  "text2sql": "（推理说明……）\n\nSELECT SUM(CAST(gkcaigouamount AS DECIMAL)) AS public_procurement_amount, ... LIMIT 5;"
}
```

**本模块工具摘要**：


| 工具名        | 请求主结构              | 响应主结构          |
| ---------- | ------------------ | -------------- |
| `text2sql` | `db_uri` + `query` | 单字符串（推理 + SQL） |


---

### 4. SQL 执行（Dify 工具：database）

本节为 **SQL 执行**独立模块，与 **SQL 转换** 衔接：通常以上一节 `text2sql` 产出（经解析）的 SQL 作为本模块 `query` 入参。调用形态与上一节相同（工具名为键的对象）。

- **工具名**：`sql_execute`  
- **说明**：在指定数据库上执行 SQL，返回 JSON 字符串形式的查询结果（内含 `result` 行集）。

**请求参数（`sql_execute` 对象内字段）**：


| 参数名      | 类型     | 必填  | 说明                        |
| -------- | ------ | --- | ------------------------- |
| `db_uri` | string | 是   | 数据库连接 URI，与 `text2sql` 一致 |
| `query`  | string | 是   | 待执行的 SQL 语句               |


**请求示例**：

```json
{
  "sql_execute": {
    "db_uri": "mysql+pymysql://user:123456@host:port/rail_assistant",
    "query": "SELECT \n  SUM(CAST(gkcaigouamount AS DECIMAL)) AS public_procurement_amount,\n  SUM(CAST(caigouAmount AS DECIMAL)) - SUM(CAST(gkcaigouamount AS DECIMAL)) AS non_public_procurement_amount,\n  SUM(CAST(caigouAmount AS DECIMAL)) AS total_procurement_amount\nFROM ads_dzcg_bidsum;"
  }
}
```

**返回参数**：


| 参数名           | 类型     | 说明                   |
| ------------- | ------ | -------------------- |
| `sql_execute` | string | **JSON 字符串**。解析后见下表。 |


**内层结构（对 `sql_execute` 做 `JSON.parse` 后）**：


| 参数名      | 类型    | 说明                    |
| -------- | ----- | --------------------- |
| `result` | array | 查询结果行列表，每行为对象，键为列名或别名 |


**注意**：数值列可能被序列化为字符串（如 `"693475000"`），Mock 或前端需按业务转换类型。

**外层响应示例**：

```json
{
  "sql_execute": "{\"result\": [{\"non_public_procurement_amount\": \"216830140\", \"public_procurement_amount\": \"476644860\", \"total_procurement_amount\": \"693475000\"}]}"
}
```

**解析后**：

```json
{
  "result": [
    {
      "non_public_procurement_amount": "216830140",
      "public_procurement_amount": "476644860",
      "total_procurement_amount": "693475000"
    }
  ]
}
```

**本模块工具摘要**：


| 工具名           | 请求主结构              | 响应主结构                  |
| ------------- | ------------------ | ---------------------- |
| `sql_execute` | `db_uri` + `query` | JSON 字符串：`result[]` 行集 |


---

### 5. 图表可视化

- **模块**：图表生成  
- **接口名**：chart-generator

根据查询数据、指标、维度生成可渲染图表结构，支持柱状图、折线图、饼图、文本说明等。

**请求参数**：


| 参数名      | 类型     | 必填  | 说明                                                                        |
| -------- | ------ | --- | ------------------------------------------------------------------------- |
| `待处理的数据` | string | 是   | 查询结果序列化后的字符串（示例为 JSON 数组字符串）；与 `queryResult`（object）语义一致时实现可二选一           |
| `图表标题`   | string | 否   | 图表标题                                                                      |
| `图表类型`   | string | 否   | 如 `pie` / `bar` / `line`，对应 `displayType`：`bar` / `line` / `pie` / `text` |
| `配色方案`   | string | 否   | 如 `default`                                                               |


**请求示例**：

```json
{
  "待处理的数据": "[{\"采购类型\":\"公开采购\",\"金额\":477000000,\"占比\":68.8},{\"采购类型\":\"非公开采购\",\"金额\":217000000,\"占比\":31.2}]",
  "图表标题": "公开采购与非公开采购金额构成",
  "图表类型": "pie",
  "配色方案": "default"
}
```

**响应示例**：

```json
{
  "code": 200,
  "message": "生成成功",
  "chartData": {
    "title": {
      "text": "公开采购与非公开采购金额构成"
    },
    "series": [
      {
        "type": "pie",
        "radius": "55%",
        "data": [
          {
            "name": "公开采购",
            "value": 477000000
          },
          {
            "name": "非公开采购",
            "value": 217000000
          }
        ]
      }
    ]
  },
  "analysis": "公开采购金额占比68.8%，为主要采购方式，非公开采购占比31.2%。"
}
```

---

## 附录：接口与工具速览


| 流程段    | 模块/工具              | 形态                    | 返回主结构（摘要）                                                                    |
| ------ | ------------------ | --------------------- | ---------------------------------------------------------------------------- |
| 问题定义   | problem-definition | HTTP                  | `problem_model` + `link_result` + `data`                                     |
| 方案规划   | 方案生成               | HTTP（`query`）         | `data.方案模型[]`                                                                |
| 组件匹配   | 方案到工具匹配            | HTTP（`plan`）          | `data.tools[]`                                                               |
| 意图识别   | 意图识别               | HTTP（`query`）         | `intent_id` + `domain` + `scenario` + `object` + `intent_name` + `user_goal` |
| 上下文与指标 | answer-generation  | HTTP（`intent_output`） | `answer`                                                                     |
| SQL 转换 | `text2sql`         | 工具调用                  | 单字符串（推理 + SQL）                                                               |
| SQL 执行 | `sql_execute`      | 工具调用                  | JSON 字符串 → `result[]`                                                        |
| 图表     | chart-generator    | HTTP                  | `chartData` + `analysis`                                                     |


---

