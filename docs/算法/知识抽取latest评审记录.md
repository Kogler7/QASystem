---
noteId: "ada531e0b26d11f195eb8f32b5f7472f"
tags: []

---

# V2 review 与 V3 设计决策

## 主要发现

|优先级|V2 中的证据|影响|V3 的处理|
|---|---|---|---|
|高|`prompt_router.py:622` 的关系配对描述包含 ID/类型，遗漏实际名称；`relation_resolver.py:144` 的上游候选本有名称|模型无法可靠地从 entity-N 知道关系两端是什么|直接用 type/name 标识实体和关系端点，展示属性；关系与端点一起提议|
|高|`relation_resolver.py:14` 的固定关系集合及后续排除逻辑|部分合法 ontology 关系绕过通用 LLM 抽取，依赖领域规则|关系签名全由输入 ontology 决定，没有固定关系名黑名单|
|高|`candidate_generation.py:918` 以当前 chunk 的引文匹配过滤关系|有语义支持但跨段、OCR 换行或引文不完全一致的关系被丢弃|校验真实来源 chunk；引文定位独立于事实接受和 confidence|
|中|`property_enrichment.py:247` 可接受空字符串更新，`mapping_scorer.py:101` 按属性键覆盖率计分|空值也可能增加覆盖分数。探针中真实值与两个空字符串均得 1.0；该评分默认是注释，并非全部结果的硬过滤|省略空值，分开保存实体与属性支持度，评测具体属性值|
|中|`split_extractor.py:830` 的修复请求没有保留实际 assistant 输出|模型难以围绕自己的错误精确修改|保存 assistant JSON，再追加带路径与期望类型的校验问题|
|中|`prompt_router.py:584` 的首轮实体阶段未展示完整属性和关系约束|关系发现受先前实体集合限制|一次联合提议实体、属性、关系；有限补漏可以补端点|

这些结论针对 public `extract -> extract_harness -> extract_split` 路径。未把旧版 `extractor.py` 中所有逻辑都当作当前默认路径。以上是代码行为证据，具体指标收益由真实对照决定。

## 控制器与模型职责

模型负责理解 ontology 定义和文档、提出事实及支持度。Python 控制器负责预算、来源、类型与引用检查、稀疏属性、文档作用域、合并、冲突和停止条件。

控制器不会根据 golden 决定抽什么，也不通过不断增加业务 prompt 路由提升特定数据集分数。保留上下文中较完整的名称、避免把每个词拆成节点，是统一模板中的语义粒度规则。

按最新要求，语义合并使用节点类型、名称、完整属性和对应原文共同判断。LLM 提建议，控制器校验组内属性冲突、真实成员、关系自环与预算后应用；名称相似不作为自动合并条件。输出身份与 golden 的 type/name 及关系五元组一致。

原始文档/Excel/PDF 的解析仍属于已有上游管线。V3 只观察原分片，不改写上游 chunk 或部署接口；格式通用性取决于解析后是否保留正文、表头及定位信息。

## 评测解释

已有 benchmark 是开发集，不是新 holdout。固定 exact identity 口径有命名敏感性：golden 中概括的名称可能与原文长名称不同，关系计分又受两端名称共同影响。不能为消除这项差异在抽取运行时读取 golden 或硬编码标注命名。

评测同时报告 accepted-fact 与 strict-success 口径，以区分“抽出了部分事实”和“完整成功”。低 confidence 的错误仍是 FP。evidence 不参与匹配；不因定位不精确降低分数。

## 回归与边界

修改前与修改后均运行 V2 的 116 项 unittest：114 通过，两个既有失败仍分别是 `test_document_units_match_gold_01` 与 `test_standard_metadata` 的命名期望差异。没有为使这些测试变绿而修改 V2 或 golden。

V3 已通过离线契约测试和真实 JSON Mode 连通/小图抽取。完整效果以最终 benchmark 报告为准，不能用连通测试或 fake LLM 测试替代召回率/F1 证据。无第三方新依赖；评审当时环境没有 pytest、ruff、mypy/pyright，验证依赖 unittest、AST 解析及独立代码审查（本仓库现已统一使用 pytest 运行测试）。
