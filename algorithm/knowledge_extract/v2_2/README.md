---
noteId: "338dfad0c30d11f1aea7cbe984ab9484"
tags: []

---

# Knowledge Extract V2.2

本目录只保留 v2.2 算法源码、单元测试和就地说明。设计与评测说明在根 docs/算法，输出统一在 tmp/eval。

| 路径 | 内容 |
| --- | --- |
| `*.py` / `tests/` | 抽取实现与单测 |
| 根 `docs/算法/` | 抽取逻辑、Clef 说明、关系方案、评测结果说明 |
| 根 `tmp/eval/imported-zyx/v2.2/20261013/` | 本次合并归档的历史评测产物，不提交 Git |

Benchmark 数据集仍在 `test_data/eval-v2/`；在线链路评测走 `eval/run.py`（`KG_EXTRACT_ALGO_VERSION=v2.2`）。

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

最近一次 official_01 关系阶段评测：`docs/official_01评测结果-20261012.md`，原始批次在 `output/20261012-011048/`。

## 设计与运维文档

- [V2.2 抽取逻辑](../../../docs/算法/知识抽取V2.2抽取逻辑.md)
- [V2.2 Clef 优化设计](../../../docs/算法/知识抽取V2.2-Clef优化.md)
- [长文档关系抽取方案](../../../docs/算法/知识抽取V3.1长文档关系抽取方案-20261012.md)
- [official_01 评测结果（2026-10-12）](../../../docs/算法/official_01评测结果-20261012.md)
- [V3.1 流程说明](../../../docs/算法/知识抽取V3.1流程说明.md)
- [V3.1 代码审查](../../../docs/算法/知识抽取V3.1全流程代码审查-2026-10-11.md)
- [现行决策模型部署](../../../docs/部署/决策模型部署.md)
