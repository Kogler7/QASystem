# Phase 1 Step 1 基准集报告

- 冻结时间：2026-08-11T16:32:14+08:00
- 抽取模块：`knowledge_extract_v2`
- 模式：`stub:gold`（stub=True）
- LLM 端点已配置：False；模型：deepseek-v4-flash-0731(default)
- 说明：stub 模式仅用于验证评测链路，不能作为正式基线。正式基线需配置 KG_EXTRACT_LLM_BASE_URL / KG_EXTRACT_LLM_API_KEY 后以 --mode live 重新冻结。

## 汇总（macro 平均）

| 指标 | 值 |
| --- | --- |
| Entity F1 | 100.0% |
| Entity Precision | 100.0% |
| Entity Recall | 100.0% |
| Property Normalized Accuracy | 100.0% |
| Property Exact Accuracy | 100.0% |
| Relation F1 | 100.0% |
| Relation Precision | 100.0% |
| Relation Recall | 100.0% |
| Schema Error Rate | 0.0% |
| Composite Accuracy | 100.0% |
| 统一指标（候选，未冻结） | 100.0% |

统一指标口径：候选统一指标（未冻结）：0.3*entity_f1 + 0.2*property_normalized_accuracy + 0.3*relation_f1；复合用例再计入 0.2*composite_accuracy；某分量不适用（如无 gold 关系）时按剩余权重重新归一化。

## 失败归因（汇总）

| 失败类型 | 数量 |
| --- | --- |
| JSON 解析失败 | 0 |
| LLM 请求失败 | 0 |
| 重复实体 | 0 |
| 悬空关系 | 0 |
| Schema 错误 | 0 |

## 分用例

| 用例 | 类别 | Entity F1 | Prop Acc | Relation F1 | Schema Err | Composite | 统一指标 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| standard-01 | standard | 100.0% | 100.0% | 100.0% | 0 | - | 100.0% |
| official-document-01 | official_document | 100.0% | 100.0% | 100.0% | 0 | - | 100.0% |
| terminology-01 | terminology | 100.0% | 100.0% | - | 0 | - | 100.0% |
| ontology-class-extraction-01 | ontology_class_extraction | 100.0% | 100.0% | - | 0 | - | 100.0% |
| simple-rule-01 | simple_rule | 100.0% | 100.0% | 100.0% | 0 | - | 100.0% |
| composite-rule-01 | composite_rule | 100.0% | 100.0% | 100.0% | 0 | 100.0% | 100.0% |
| official-document-02 | official_document | 100.0% | 100.0% | 100.0% | 0 | - | 100.0% |

## 失败归因（分用例）

| 用例 | JSON 解析失败 | LLM 请求失败 | 重复实体 | 悬空关系 | Schema 错误 | error_code |
| --- | --- | --- | --- | --- | --- | --- |
| standard-01 | 0 | 0 | 0 | 0 | 0 |  |
| official-document-01 | 0 | 0 | 0 | 0 | 0 |  |
| terminology-01 | 0 | 0 | 0 | 0 | 0 |  |
| ontology-class-extraction-01 | 0 | 0 | 0 | 0 | 0 |  |
| simple-rule-01 | 0 | 0 | 0 | 0 | 0 |  |
| composite-rule-01 | 0 | 0 | 0 | 0 | 0 |  |
| official-document-02 | 0 | 0 | 0 | 0 | 0 |  |
