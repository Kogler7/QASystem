---
noteId: "e0be0e40b26c11f195eb8f32b5f7472f"
tags: []

---

# Knowledge extraction V3

V3 是独立的 ontology 驱动抽取器。V2、Backend、解析器、部署和已有 benchmark 标注均不修改。

## 调用

```python
from algorithm.knowledge_extract.v3 import ExtractionChunk, extract_detailed

result = extract_detailed(
    [ExtractionChunk("chunk-1", "张三在星海公司工作。", document_id="document-1")],
    "class Person\nclass Company\nPerson --> Company : worksFor",
    api_config="eval_api.md",
)
print(result["status"], result["entities"], result["relations"])
```

`extract` 是同一入口的别名包装，返回包含状态与评分的完整结果。也可注入 `llm(messages)`，离线测试不需要网络或第三方 SDK。

默认配置：硅基流动 `deepseek-ai/DeepSeek-V4-Flash`，`response_format={"type":"json_object"}`，`stream=False`，`enable_thinking=False`，temperature 0，输出上限 16,384 tokens。凭据从 `eval_api.md` 读取，环境变量 `KG_EXTRACT_V3_API_KEY`、`KG_EXTRACT_V3_BASE_URL`、`KG_EXTRACT_V3_MODEL` 可覆盖。文件中的密钥不会进入模型 messages 或评测配置。

JSON Mode 只保证语法，V3 在本地检查 ontology、属性类型与关系端点。非流式让控制器拿到完整 JSON 后统一校验；关闭 thinking 是初始效果/成本选择，尚未声称胜过 thinking。参数依据：[SiliconFlow Chat Completions](https://docs.siliconflow.cn/docs/api/chat-completions-post)。

## 流程与边界

1. 复用 V2 的 PlantUML 解析器，保留原始 ontology 定义与 notes，编译类、继承、属性、identity 和关系签名。
2. 只用一个 chat 模板，任务操作是 `extract` / `amend` / `review`。实体、属性和关系联合提议；无领域分流或关系名黑名单。模型输出紧凑 JSON，省略重复的默认字段，降低长文档输出截断风险。
3. 实体唯一键是 `(type,name)`；关系使用 `from_type/from_name/rel_type/to_type/to_name`，与 golden 一致。模型输入与抽取结果不使用实体或关系 ID。每文档共享已知节点与跨窗口上下文，最终按上述键去重，不凭跨文档共现创造新关系。
4. 每文档至多一次补漏、两次修复、一次条件触发的语义 review。N 个观察窗口、D 个文档，调用上限 `2*N + 4*D`；重试也计数。不同文档默认 4 路并行，同一文档按顺序处理，输出按输入文档顺序归集；全局预算在锁内检查及预留。可设 `max_workers=1` 串行。

原分片不改写。长块默认按 3,000 字符观察，保留源 chunk、原始偏移和邻近上下文；每次输入有 128,000 UTF-8 字节的保守上限。超大 ontology 无法装入时显式返回 incomplete，当前没有自动拆分 ontology。总 token 预留可由 `max_total_tokens` 限制；预留量是保守估算，真实用量记录在 trace 中。

补漏最多观察 12 个源窗口且受长度限制，未访问数量写入诊断。已知实体上下文按当前提及名称和近期事实选取，较长属性不重复发送，完整事实仍保存在结果中。截断恢复仅允许一层二分并占用两个修复名额；仍截断会标记 incomplete。

修复采用增量添加：省略已有事实不等于删除；不接受任意删除/替换。冲突属性保留首值及 `alternatives`，不把矛盾标量拼接。

文档节点数达到 `review_min_entities=40` 时，LLM 在同一模板的 `review` 操作中检查重复节点。每个候选包含 **type、name、完整 properties、alternatives 和对应原文 source_texts**，不凭名字相似自动合并。每节点最多两个 1,200 字符原文片段，记录省略与截断；整个请求仍受输入字节预算限制，未 review 节点数写入诊断。可用 `semantic_review=False` 关闭。候选超过 `review_max_entities=160` 时只做有界检查。

LLM 只提合并建议；控制器要求现存 canonical 名称、同类型、组内所有非空属性及历史备选值无冲突、merge confidence 至少 0.8，并拒绝合并后新产生的自环。通过后原子合并属性和来源、同步更新关系端点并去重，事实分数取保守值。review 不能任意删除节点，也不会根据 golden 选择名称。误分类或缺乏可靠同指依据的节点保留。属性值使用确定性比较；语义相近但写法不同的冲突值目前也会阻止合并，这是保守限制。

## 数据与评分

- 属性省略 null、空字符串、空集合和未知占位符；保留 0 / false。实体确实存在时可以 `properties={}`，没有支持关系时可以返回空关系集合。
- 模型和内部校验使用 ontology 属性标识；最终输出将属性及其评分、备选值的键转为 ontology 显示名，与既有公开格式一致。同一个类存在重复显示名时保留稳定标识，避免键覆盖。
- 大整数保持整数精度；小数若无法通过 Python float 无损往返，会保留为数值字符串，避免悄悄改变原值。
- 明确 unsupported、非法类型、未知引用和错误方向拒绝接受。有依据但不确定的事实保留，`uncertain` 分数至多 0.49，`contextual` 至多 0.79。
- `confidence` 是未校准的支持分数，不是事实正确概率。重复提议不会自动提高分数，实体与属性分别评分。
- `source_refs` 只能引用本轮真实输入；缺省时附上窗口来源。`locator_status` 表示引文定位情况，定位不准不改变事实分数，也不参与 benchmark 匹配。
- 相同 document_id 的块共享上下文；缺失 document_id 时每块独立并记录诊断。语义合并仅在文档内进行；最终跨文档只按完全相同的 `(type,name)` 去重，不构造新关系。

## PDF / Word / Excel / Markdown

输入边界沿用上游 `ExtractionChunk`。Markdown 正文、Word 解析内容、PDF OCR Markdown、Excel 的列/行 JSON 均可作为 text；`page_no` 和 `metadata` 中的 sheet/row/column 信息用于定位。不要把鉴权上下文放入分片元数据。

这里验证的是**解析结果到抽取器的契约**，没有新实现原始文件解析器。真实 PDF 依赖现有 OCR/MinerU 管线；已有 `PDFProcessor` 本身是占位实现。Excel 可由现有 `ExcelProcessor.process_excel()` 保留 sheet/columns/data，Word 可由 `DocxProcessor.process()` 提供 content。解析丢字、丢表头的影响没有被这些契约测试覆盖。

## 验证与 benchmark

从仓库根目录执行：

```bash
PYTHONPATH=algorithm python3 -B -m unittest discover -s algorithm/knowledge_extract/v3/tests -p 'test_*.py' -q
PYTHONPATH=. python3 -B -m algorithm.knowledge_extract.v3.benchmark --versions v3 v2 --workers 4 --runs 1 --output-dir algorithm/knowledge_extract/v3/artifacts/paired
```

评测器冻结源文档、本体和 golden 的 SHA256，并从冻结副本执行。运行前保存源码到 `code/`、安全配置与源码哈希到 `run_metadata.json`。抽取器只得到文档和本体；golden 仅评测器读取。每例保存预测、状态、成本和原子写入的检查点；配置或源码变化会拒绝复用旧检查点。`--runs 3` 可做重复配对实验；输出包含每轮指标和标准差，不能挑最好一轮。

实体采用 `(type,name)`，关系采用 `(from_type,from_name,rel_type,to_type,to_name)` 的 V2 identity 口径，属性按实体身份/属性/值计分。实体属性中的重复 name 字段统一不评分。所有低分事实均进入主表；evidence 不评分。失败案例保留分母。另附 strict_summary，将未完整成功的案例记为零预测。

当前数据发现 29 例，其中 19 例同时具备 Markdown 和 golden；9 例缺 Markdown，1 例缺 golden。不能把这些结果解释为全部原始 PDF/Excel 的质量或未见领域泛化。已知命名改写会影响 identity 指标，例如标注概括名称与原文长短名称不同；当前不借助 golden 改写抽取名称。

为保持 V2 评测输入口径，runner 将每个 Markdown 文件视为独立文档；部分 standard 案例同时含完整文件、分页文件或重复解析版本，均进入两版输入。当前不自动推断分页的共同 document_id，也不去除这些副本。这会增加请求数，并限制分页间上下文；后续改变选择策略时必须同时重跑两版，不能与旧分数直接拼接。

`artifacts/` 是本地实验记录，含文档及模型响应，不纳入源码版本控制。早期 `baseline-20260917` 的冻结器遗漏 golden 副本，未真正运行抽取；该目录不作为效果证据。正式对照以最终报告指定的运行目录为准。
