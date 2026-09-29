---
noteId: "424c8dd0b2a411f196e53319be00f7c1"
tags: []

---

# Knowledge Extract V2

本目录只包含两部分：

- `benchmark/`：17 个冻结的 dev case，以及对应的 Markdown、PlantUML ontology、golden 和 manifest hash；
- 算法源码：ontology 驱动的两阶段实体/关系抽取、解析、验证和图结果组装。

## 公共入口

```python
from algorithm.knowledge_extract.v2 import extract
from algorithm.knowledge_extract.v2.chunk_context import ExtractionChunk

result = extract(
    ontology=ontology,
    chunks=[ExtractionChunk(chunk_id="chunk-0", text=chunk_text)],
)
```

V2 只消费上游已解析、已切分的 chunk，不接收完整 Markdown 或 raw string，不在算法内解析或改变 chunk 边界。
chunk 可以是 `ExtractionChunk`，也可以是符合其字段契约的 dict（缺少 `chunk_id` 时自动生成并给出 `CHUNK_ID_GENERATED` 警告；重复 `chunk_id` 改名保留并给出 `DUPLICATE_CHUNK_ID` 警告）。
入口会深拷贝 chunk 与嵌套 metadata；元数据补全、重复 ID 消歧和路由不会修改调用方对象。offset 回退、非法 span、缺失/自引用/跨文档邻接引用会形成结构化 warning，并汇总到 `profile.chunk_contract`。
完全缺失 `document_id` 时按顺序补默认文档号；文档确有标题结构（Markdown 标题、`第X章/节/条` 或编号标题）时，为缺失者派生 `section_id` / `section_title` / `clause_number`（见 `profile.metadata_enrichment`），无标题的平文文档不注入假结构，文本内容始终不变。
纯空白 chunk 只作为证据锚点保留、不参与扫描（见 `profile.scannable_chunks` / `skipped_chunks`）。
本体为空或未解析出任何 class 时：strict 模式抛出 `ExtractionValidationError(error_code="ONTOLOGY_EMPTY")`；
permissive 模式返回结构化失败（`error_code="ONTOLOGY_EMPTY"` / `TARGET_SCOPE_EMPTY`，不调用 LLM）。
目标类型可为插件元类 UID（点分 key，如 `公文本体元模型.公文管理单元`），按逐级去包前缀回退到类符号解析；简单名跨包歧义时不静默选中。
调用方不能传入 legacy document-type 字段或抽取策略；V2 会根据 ontology 指纹与 chunk 文本确定性生成 `profile.document_route`。
PlantUML 在这里是受限本体 DSL，不是渲染器：只编译 class、属性、继承、identity 与白名单关系；interface/enum/entity 等未支持语义会显式诊断，`!include`、宏、环境变量和外部数据读取等预处理能力一律以 `UNSUPPORTED_PREPROCESSOR` 拒绝。identity 必须引用本类或继承属性；深层继承属性会物化，多父类 identity 冲突与父子属性类型冲突是编译错误。
路由只信强信号（标准号、文号〔YYYY〕N号、公文标题形式；词汇与"第X条"不参与），匹配前先做 NFKC 与全角/排版斜杠（∕、／、⁄）、破折号变体折叠；段内采信规则为文档头锚定 + 正文多命中阈值——首个实质内容 chunk（空行、批量头与纯标题行不算文档头）命中的信号视为文档身份直接生效，仅出现在正文后续 chunk 的信号需 ≥2 个 chunk 且命中比例 ≥50% 才采信，单次正文命中按疑似引用记录（`route.chunks.*.unconfirmed`）不贴标签；产出多标签 `labels`（`official_document` 与 `standard` 可并存，`kind` 以公文优先，`source` 为 `chunks`/`ontology`/`fallback`）；批量拼接上传时按 `document_id` 与 `## 文件:` 头分段，每个 chunk 使用本段的领域提示词，标准确定性补全以 `"standard" in labels` 门控、不随聚合 `kind` 被公文抢占而跳过。
调用时输出逐 chunk 路由（日志「逐chunk路由输出」），并在 `profile.chunk_routes` 以 `{chunk_id: kind}` 保持兼容；完整解释位于 `profile.chunk_route_details`，包含 `scores`、`signals`、`decision`、`abstain_reason` 和 `router_version`。chunk 中部出现新的 `## 文件:` 头时不会自行重切片，而会拒判为 generic/ambiguous，要求上游按文档边界重新切片。`profile.ontology_compiler_version` 用于回放本体编译行为。

稳定输出仅包含：

```json
{
  "entities": [
    {
      "type": "Terminology",
      "name": "知识地图",
      "properties": {"TermName": "知识地图"},
      "mapping_score": 0.88,
      "confidence": 0.9321,
      "confidence_status": "accepted",
      "gate_status": "passed",
      "confidence_details": {
        "type": "heuristic_score",
        "source": "multi_signal",
        "method": "mapping_bounded_multi_signal_v3",
        "calibrated": false,
        "status": "accepted",
        "mapping_status": "accepted",
        "signals": {
          "mapping": {"score": 0.88, "weight": 0.55},
          "evidence": {"score": 1.0, "weight": 0.25},
          "canonical_mapping": {"score": 1.0, "weight": 0.2}
        },
        "gates": []
      }
    }
  ],
  "relations": [
    {
      "from_type": "Terminology",
      "from_name": "知识地图",
      "rel_type": "BELONGS_TO",
      "to_type": "BusinessDomain",
      "to_name": "知识管理",
      "mapping_score": 0.82,
      "confidence": 0.82,
      "confidence_status": "needs_review",
      "gate_status": "review",
      "confidence_details": {
        "type": "heuristic_score",
        "source": "multi_signal",
        "method": "mapping_bounded_multi_signal_v3",
        "calibrated": false,
        "status": "needs_review",
        "mapping_status": "low_confidence",
        "signals": {
          "mapping": {"score": 0.82, "weight": 0.55}
        },
        "gates": ["mapping_low_confidence"]
      }
    }
  ]
}
```

`mapping_score` 是 `[0, 1]` 范围内的确定性 ontology-fit 基线；`confidence` 以它为上界，证据完整度和 canonical mapping 只能下调、不能把低质量 mapping 晋升成高置信度。关系 mapping 已包含 evidence/canonical legality，因此关系 confidence 不再重复计权，只保留缺证据、mapping 状态、基数超限等决策门控。`confidence` 仍是 `calibrated=false` 的启发式质量分，不能解释为“事实有 92% 概率正确”。`confidence_status` 表示 accepted/needs_review/rejected 决策，`gate_status` 表示门控是否 passed/review/blocked。重复事实采用保守下界并合并最严重 gate；在没有 provenance 独立性证明时，不把重复 chunk 当作独立概率支持。具备独立人工标注校准集后，应保留原始 `mapping_score`，再新增 sigmoid/isotonic 校准版本。

评测 runner 使用上游 `knowledge_pipeline` 切块，再通过 `extract_harness` 获取内部诊断信息。
`candidate_generation.py` 与 `prompt_router.py` 共同定义单一基础 chat prompt：所有实体、关系和修复调用都使用共享 system message 与统一 user 模板，领域差异只来自自动路由解析出的 domain extension。
解析层按《文档解析行为规范》§10 输出的表格统一是 JSON（`{"columns", "rows"}`），这只表示存储结构，不表示规则场景。`table_semantics.py` 联合当前本体允许的规则类型与非空源记录中的条件、结果、故障原因或处置证据自动判定；分类列、文件名和 RuleID 本身都不足以触发规则归纳。设备台账、参数测量和公文收文表按普通事实处理，公文表单的批示不会进一步展开为职责、任务或约束实体。混合表只对有规则证据的记录启用规则提示，不按类别合并普通对象；缺 RuleID 的真实规则仍保留且不编造编号。

`profile.table_semantics` 记录每个表格 chunk 的 `mode`（`facts/rules/mixed/uncertain`）、`rule_types`、`rule_rows`（JSON 表与数据行从 1 开始的序号）和判定原因。证据不足时使用普通事实策略并限制规则候选类型；表外正文独立处理。这是确定性的保守识别，未覆盖的领域字段可能需要扩展语义别名，不代表完整的语义召回验证。

规则候选还须通过源记录行的证据检查，不能仅依靠提示词约束。分窗后的证据按原表序号、数据行号及 JSON 行内容等值校验回溯；同名记录保留所在行的引文，避免全局首次匹配将规则绑定到普通记录。

JSON 形态仍用于表格结构保护、公文表单字段保存与标准「表N 标题」的结构支撑：切片器把 ```json 围栏与 JSON 保护在同一片段，裸 JSON chunk（围栏被历史切片撕到相邻片段）同样识别，表题与表格数据分属相邻 chunk 时结构支撑跨一片判定。

公文按来源角色分流，格式（Markdown/JSON）不决定抽取类型：`official_document_scope.py` 根据公文本体、文件名、表单字段及明确的附件段落标题自动判定，并将结果写入 `profile.official_document_roles`。

- 表单只抽取 `OfficialDocument`（公文管理单元），其字段作为管理单元属性；不从批示展开其他实体或关系。
- 正文抽取其他本体类型，包括 `DocumentUnit`（公文结构单元），不从正文新增管理单元。
- 附件不进入实体、属性、关系或修复 LLM 上下文；`official_attachments.py` 按来源文件生成 `OperationalInformation`，信息类型为“附件”，保留完整解析原文和 `source_files` 文件引用。同名不同来源的附件使用独立身份，不与正文知识实体合并。

附件文件以“附件/附录/attachment/appendix”前缀或明确的角色 metadata 识别；正文中的独立附件标题会切出附件后续片段，普通“见附件”引用不会切分。无角色信息且无可识别名称/标题的独立文件需要上游补充来源 metadata。`profile.chunk_source_ids` 保留拆分片段到原输入的映射，保证返回图的证据位置不漂移。附件节点随抽取结果返回，由应用服务持久化；此逻辑不会调用未开放的图写入能力。

Excel/CSV 仍由上游解析：每个工作表渲染为 `## <sheet>` 加一个 JSON 记录表。后端适配器把 `source_file`、`source_type`、字符 range、sheet 名、列名和数据行范围写入 `ExtractionChunk`；V2 不重新解析工作簿。记录表在实体候选阶段按“最多 20 行且约 6000 字符”双阈值无损分窗，子窗保留原 sheet 与行范围。旧式 `.xls` 只有在 pandas 环境提供 `xlrd` 时可读，否则返回明确错误并要求转为 `.xlsx`/`.csv`；公式按缓存值读取，合并单元格只保留左上角值，这两项限制会出现在解析 metadata 的 `table_semantics` 中。

## Benchmark 校验与评测

```bash
.venv/bin/python -m algorithm.knowledge_extract.v2.benchmark_lint
.venv/bin/python eval_knowledge_extract_v2_simple.py --local
.venv/bin/python eval_knowledge_extract_v2_official.py --local
.venv/bin/python eval_knowledge_extract_v2_standard.py --local
```

LLM 连接由 `KG_EXTRACT_LLM_*` 环境变量配置。算法不会读取或写入 Neo4j、Elasticsearch、benchmark golden、部署凭据或运行时数据。

Thinking 默认全局关闭（`KG_EXTRACT_LLM_DISABLE_THINKING=1`）。设为 `0` 后，仅 `KG_EXTRACT_THINKING_PASSES`（默认 `relation,repair_relation`）使用更大的 `KG_EXTRACT_THINKING_MAX_TOKENS`；实体/属性 pass 仍关闭思考。

本地 Jev 判别默认关闭（`KG_EXTRACT_JEV_ENABLED=0`）。打开前先把 HF 分类器挂到 `KG_EXTRACT_JEV_MODEL_PATH`（可用 `docker/download_models.py --jev`），再跑：

```bash
.venv/bin/python -m algorithm.knowledge_extract.v2.jev_probe
```

中文公文/标准分布不校准就保持关闭。Jev 只做实体模糊合并和关系 pair 判断，不替代确定性去重或 `HAS_PART` 组边。
