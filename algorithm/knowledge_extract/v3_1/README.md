# 知识抽取 V3.1

通用语义 harness：主 LLM 发现实体、补全属性、召回关系并处理疑难项；Clef 判断候选支持、类型、别名和属性冲突；代码验证本体、原文引用、端点、预算和执行状态。

Backend 设置 `KG_EXTRACT_ALGO_VERSION=v3.1` 后使用本包。`latest` 保持独立实现。配置与评测说明见 [V3.1 语义 Harness 与评测](../../../docs/算法/知识抽取V3.1语义Harness与评测.md)。

当前准确性复核、V2 对照、开源机制取舍和最新 mini 结果见 [准确性复核与改进](../../../docs/算法/知识抽取V3.1准确性复核与改进.md)。模型通过原文行 ID 引用证据，输出仍还原为 V2 兼容的 quote、来源和字符区间。

```python
from algorithm.knowledge_extract.v3_1 import ExtractionChunk, extract_aggregate

result = extract_aggregate(
    ontology=ontology_text,
    chunks=[ExtractionChunk(chunk_id="chunk-0000", text=source_text)],
)
```

输入复用 V2 `ExtractionChunk`。`extract(chunks, ontology)` 返回 V2 公开结果，未完整成功会抛出相同契约的抽取异常；`extract_aggregate` 保留部分图与 `status/verification/run_trace` 诊断，调用方须读取质量状态。

V2 兼容的是输入输出结构。旧 `repairer/entity_adjudicator/max_workers/prompt_variant/retry_base_seconds` 参数接收但不执行旧编排；`max_attempts` 只允许 1，旧 `max_repair_rounds=2` 按一轮处理。Backend 即使返回非空图，也可能有 `quality.status=partial` 和 `graph_publishable=false`，不能仅据图非空判断成功或可沉淀。

测试可注入 `llm`、`decider` 或 `clef_client`，关闭 `use_scheduler`；运行时默认关闭 thinking。真实模型配置沿用项目主模型配置，Clef 使用根配置中的 `CLEF_SERVICE_URL`。
