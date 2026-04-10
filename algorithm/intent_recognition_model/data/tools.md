# 工具列表

## 数据库相关工具

### SQL Execute
**描述**：此工具用于在已存在的数据库中执行 SQL 查询。

**参数**：
- `sql` (string)：要执行的 SQL 查询语句
- `DB URI` (string)：数据库的 URI 地址

### Text to SQL
**描述**：提供数据库上下文和用户的问题，来得到一个 SQL 语句。

**参数**：
- `query` (string)：用户的问题
- `table` (string)：指定哪些表用于提供给LLM作为上下文，如不指定，则使用所有表。多张表用半角逗号隔开。
- `DB URI` (string)：数据库的 URI 地址

### Get Table Schema
**描述**：从数据库中获取表结构。

**参数**：
- `table` (string)：指定想要哪些表的结构，如不指定，则使用所有表。多张表用半角逗号隔开。
- `DB URI` (string)：数据库的 URI 地址

### CSV Query
**描述**：运行在 CSV 文件上的 SQL 查询。

**参数**：
- `sql` (string)：SQL 查询语句。表名为 `csv`。
- `csv_file` (string)：csv 文件。

## 文档处理工具

### convertDocument
**描述**：将上传的文档 (PDF/Word/TXT) 转换为标准Markdown文本。

**参数**：
- `input_path` (string, 必填)：文档路径
- `output_filename` (string)：输出文件名

## 知识图谱工具

### addTriples
**描述**：批量添加三元组到知识图谱，用于知识更新与扩展。

**参数**：
- `triples` (array, 必填)：三元组列表
- `context` (string)：来源上下文

### extractKnowledge
**描述**：从文本中提取与指定对象相关的结构化知识，生成知识三元组。

**参数**：
- `main_object` (string, 必填)：主要提取对象
- `text` (string, 必填)：待抽取的文本内容
- `use_templates` (boolean)：是否使用模板抽取

### generateCypher
**描述**：将用户问题自动转换为Neo4j Cypher查询语句。

**参数**：
- `question` (string, 必填)：用户问题
- `graph_schema` (string)：知识图谱结构

### GenerateAnswer
**描述**：根据用户问题从知识图谱中生成详细答案。

**参数**：
- `question` (string, 必填)：用户问题
- `detailed` (boolean)：是否返回详细信息

## 知识提取与意图识别工具

### knowledgeExtract
**描述**：输入文本并抽取结构化知识。

**参数**：
- `main_object` (string, 必填)：抽取主对象类型，如 Term / RuleType / SystemElement / Glossary
- `text` (string, 必填)：待抽取文本
- `use_templates` (boolean)：是否使用模板抽取
- `llm_base_url` (string)：LLM 基础 URL
- `llm_model` (string)：LLM 模型
- `llm_api_key` (string)：LLM API 密钥

### answerGenerationAsk
**描述**：答案生成问答。

**参数**：
- `question` (string, 必填)：用户问题
- `detailed` (boolean)：是否返回详细信息

### deepRecognizelIntent
**描述**：深度意图识别。

**参数**：
- `text` (string, 必填)：待识别文本

### getIntentPlan
**描述**：获取计划。

**参数**：
- `domain` (string, 必填)：意图所属业务域名称，例如 采购管理域 / 销售分析域
- `intent_name` (string, 必填)：意图名称，例如 看构成 / 问明细
