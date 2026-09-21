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

评测 runner 使用上游 `knowledge_pipeline` 切块，再通过 `extract_harness` 获取内部诊断信息。
`candidate_generation.py` 与 `prompt_router.py` 共同定义单一基础 chat prompt：所有实体、关系和修复调用都使用共享 system message 与统一 user 模板，领域差异只来自自动路由解析出的 domain extension。

## Benchmark 校验与评测

```bash
.venv/bin/python -m algorithm.knowledge_extract_v2.benchmark_lint
.venv/bin/python eval_knowledge_extract_v2_simple.py --local
.venv/bin/python eval_knowledge_extract_v2_official.py --local
.venv/bin/python eval_knowledge_extract_v2_standard.py --local
```

LLM 连接由 `KG_EXTRACT_LLM_*` 环境变量配置。算法不会读取或写入 Neo4j、Elasticsearch、benchmark golden、部署凭据或运行时数据。

Thinking 默认全局关闭（`KG_EXTRACT_LLM_DISABLE_THINKING=1`）。设为 `0` 后，仅 `KG_EXTRACT_THINKING_PASSES`（默认 `relation,repair_relation`）使用更大的 `KG_EXTRACT_THINKING_MAX_TOKENS`；实体/属性 pass 仍关闭思考。

本地 Jev 判别默认关闭（`KG_EXTRACT_JEV_ENABLED=0`）。打开前先把 HF 分类器挂到 `KG_EXTRACT_JEV_MODEL_PATH`（可用 `docker/download_models.py --jev`），再跑：

```bash
.venv/bin/python -m algorithm.knowledge_extract_v2.jev_probe
```

中文公文/标准分布不校准就保持关闭。Jev 只做实体模糊合并和关系 pair 判断，不替代确定性去重或 `HAS_PART` 组边。
