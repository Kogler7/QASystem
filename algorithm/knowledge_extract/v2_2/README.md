---
noteId: "338dfad0c30d11f1aea7cbe984ab9484"
tags: []

---

# Knowledge Extract V2.2

本目录只包含算法源码：ontology 驱动的两阶段实体/关系抽取、解析、验证和图结果组装。

Benchmark 数据集（17 个冻结的 dev case，以及对应的 Markdown、PlantUML ontology、golden 和 manifest hash）位于 `test_data/eval-v2/`，作为历史冻结集保留（单测 fixtures 与造 golden 工具的数据源）；在线链路的评测统一走 `eval/run.py`（设置 `KG_EXTRACT_ALGO_VERSION=v2.2` 即评测本包），均不属于本目录。

## 公共入口

```python
from algorithm.knowledge_extract.v2_2 import extract
from algorithm.knowledge_extract.v2_2.chunk_context import ExtractionChunk

result = extract(
    ontology=ontology,
    chunks=[ExtractionChunk(chunk_id="chunk-0", text=chunk_text)],
)
```

V2 只消费上游已解析、已切分的 `ExtractionChunk`，不接收完整 Markdown、raw string 或 dict，不在算法内解析或改变 chunk 边界。
调用方不能传入 legacy document-type 字段或抽取策略；V2 会根据 ontology 指纹与 chunk 文本确定性生成 `profile.document_route`。

稳定输出仅包含：

```json
{
  "entities": [
    {"type": "Terminology", "name": "知识地图", "properties": {"TermName": "知识地图"}}
  ],
  "relations": [
    {
      "from_type": "Terminology",
      "from_name": "知识地图",
      "rel_type": "BELONGS_TO",
      "to_type": "BusinessDomain",
      "to_name": "知识管理"
    }
  ]
}
```

评测 runner 使用上游 `knowledge_pipeline` 切块，再通过 `extract_harness` 获取内部诊断信息。
实体、属性和修复仍走共享 system message。关系阶段在实体冻结后按本体签名召回局部证据候选，再小批分类；领域差异只来自自动路由解析出的 domain extension。当前步骤见 [V2.2 抽取逻辑](../../../docs/算法/知识抽取V2.2抽取逻辑.md)。

## Benchmark 校验与评测

```bash
.venv/bin/python -m algorithm.knowledge_extract.v2_2.benchmark_lint
KG_EXTRACT_ALGO_VERSION=v2.2 .venv/bin/python eval/run.py test_data/eval
```

LLM 连接由 `KG_EXTRACT_LLM_*` 环境变量配置。算法不会读取或写入 Neo4j、Elasticsearch、benchmark golden、部署凭据或运行时数据。

## 设计与运维文档

- [V2.2 抽取逻辑](../../../docs/算法/知识抽取V2.2抽取逻辑.md)
- [V2.2 Clef 优化设计](../../../docs/算法/知识抽取V2.2-Clef优化.md)
- [V3.1 流程说明](../../../docs/算法/知识抽取V3.1流程说明.md)
- [V3.1 代码审查](../../../docs/算法/知识抽取V3.1全流程代码审查-2026-10-11.md)
- [现行决策模型部署](../../../docs/部署/决策模型部署.md)
