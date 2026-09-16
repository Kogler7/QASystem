# Knowledge Extract V2

本目录只包含两部分：

- `benchmark/`：17 个冻结的 dev case，以及对应的 Markdown、PlantUML ontology、golden 和 manifest hash；
- 算法源码：ontology 驱动的两阶段实体/关系抽取、解析、验证和图结果组装。

## 公共入口

```python
from algorithm.knowledge_extract_v2 import extract
from algorithm.knowledge_extract_v2.chunk_context import ExtractionChunk

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

`candidate_generation.py` 与 `prompt_router.py` 共同定义单一基础 chat prompt：所有实体、关系和修复调用都使用共享 system message 与统一 user 模板，领域差异只来自自动路由解析出的 domain extension。

## Benchmark 校验

```bash
.venv/bin/python -m algorithm.knowledge_extract_v2.benchmark_lint
```

LLM 连接由 `KG_EXTRACT_LLM_*` 环境变量配置。算法不会读取或写入 Neo4j、Elasticsearch、benchmark golden、部署凭据或运行时数据。
