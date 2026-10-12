# Knowledge Extract V3.1

本目录保留算法代码与单元测试。接收上游已解析、已切片的 `ExtractionChunk`，按实体候选、属性补全及实体校验修复流程完成本体约束抽取，不在算法内部解析文档或更改上游 chunk 边界。

## 当前能力边界

v3.1 已删除 LLM 关系分类、关系候选枚举、关系语义核验、确定性边组装、复合成员边、关系修复、关系覆盖检查与关系评分。`raw.relations`、`graph.relationships`、`intermediate_model.relations` 和 `triples` 均为空。关系本体声明仍用于本体解析及实体类型路由；通用关系字段/序列化契约保留兼容。其他算法版本不受影响。

## 公共入口

```python
from algorithm.knowledge_extract.v3_1 import extract
from algorithm.knowledge_extract.v3_1.chunk_context import ExtractionChunk

result = extract(
    ontology=ontology_text,
    chunks=[ExtractionChunk(chunk_id="chunk-0", text=chunk_text)],
)
```

`extract()` 保留 `entities / relations` 接口，`relations` 固定为空列表；`extract_harness()` 返回内部诊断结果。LLM 配置沿用 `KG_EXTRACT_LLM_*`。服务算法选择值为 `KG_EXTRACT_ALGO_VERSION=v3.1`。

## 提示词与上下文

- `prompt_templates.py`：公共 System + 阶段约束、统一 JSON User 外壳和结构化重试反馈。
- `prompt_router.py`：V2 本体与受限切片信号路由、阶段上下文裁剪、消息装配。
- `skills/*/SKILL.md`：用户维护的领域规则；`skill_registry.py` 和 `skill_planner.py` 负责发现、读取与请求内一次性编译。
- `domain_skills.py`：无模型的低层兼容预览，只从 Markdown 读取策略正文。
- `output_contracts.py`：所有阶段的输出约束及动态 schema 构造。

全部阶段使用两条消息 `system + user`。User 固定为 `task`、`domain_policy`、`ontology`、`documents`、`stage_context`、`output_contract`，重试时追加 `retry_feedback`。第一步输出 `type + mention + properties + evidence`，同时抽取当前请求窗口支持的本体属性；`mention` 直接成为原文 `name`。`evidence.property_quotes` 按属性记录引文。为兼容旧输出，省略 properties 的候选仍会进入补全。JSONL 逐行解析及所有普通文本调用保持不变。

公共入口自动执行“程序确定领域 → 读取 skill 固定快照 → 按阶段编译并装配规则”，不调用模型选择领域或规划技能。发送给模型的 `domain_policy.rule_blocks` 保留每段的 `skill_id / version / section / text`，无适用技能时为空数组。按同一技能、版本和规则段去重，不再逐行拼接去重。通过 `skills_dir` 参数可替换技能目录；默认使用本包的 `skills/`。维护方式与运行边界见下文。

已知实体、核验候选及修复问题放在 `stage_context`，不能作为新的原文证据。抽取、补全和修复继续输出 JSONL，独立核验继续输出 JSON object；均为普通文本调用。字段字典及四阶段样例见[消息与文档路由](../../../docs/算法/知识抽取V3.1提示词与文档路由.md)。

完整说明、精简清单、用法和真实模型样例见[提示词与文档路由](../../../docs/算法/知识抽取V3.1提示词与文档路由.md)。

## 当前执行与可信边界

1. 深拷贝输入切片，物化目标类型迭代器，确定文档路由并加载请求内固定技能快照。`profile.input_manifest` 记录各切片内容及元数据的 SHA-256；它用于核对输入，不是模型结果缓存键。
2. 按类型和有界来源窗口提取 JSONL 候选。名称及引文都无法定位时进入带反馈重试；不能接受无来源实体。模型不提供可信字符偏移，程序定位后回映原切片。
3. 对每个新属性先定位其引文，再独立调用语义核验。实体名和值共现不能证明属性含义；仅 `yes` 接受，`no`、`uncertain` 或核验失败均移除该新值。此前已经核验且未改变的属性可复用本次运行的收据。核验上下文为引文及前后各 200 字，仍受完整请求预算约束。
4. 按完全相同的 `type + name` 合并出现记录。相同属性值保留全部来源收据；不同值需模型综合，未解决冲突保留告警并返回部分/失败状态。改值不能继承旧值的无关支持收据。已扫描、无冲突、无待处理邻片的实体跳过补全。
5. 补全按单键和来源窗口流式处理。模型必须恰好返回请求中的实体键；不得增键、漏键、重复、改名或改类型。名称的大小写、全半角、内部空格保持原样，UID 使用原始键的稳定哈希。
6. 最终逐项校验本体、实体与属性收据的来源、字符偏移、引文和 span ID。修复遍历全部相关切片，不再静默限制为前 3 片；无进展、耗尽轮数或硬校验失败均如实写入停止原因。`verification.valid` 表示没有硬错误，`coverage_complete` 另行表示是否仍有覆盖缺口。

模型适配器每次收到独立的 schema/messages 副本，不能修改本地校验契约或下一次重试的原文。局部上下文遇到文档、受限章节边界立即停止，环路不重复引用；`chunk_id` 必须是非空且无首尾空白的字符串。编码与名称的对应关系也需要所提供原文支持，不能凭模型记忆补全。

修复只接收问题涉及的实体键；`uncovered_block` 对应切片才允许发现新实体。自定义 `repairer` 输入完全深拷贝，候选经过同一本体契约校验，来源坐标与属性收据由程序重建。无效响应或异常按 schema/request/timeout 记录终态。

独立语义核验使用同一运行时 LLM，并未接入 Clef。它能约束接受条件，但不是事实正确性的形式化证明；新增核验会增加模型调用量。映射评分是完整性等规则分数，不是事实真实性概率；诊断对象保留实际评分状态。

单一 `evidence` 保留兼容，全部来源存于 `evidences` 和 `property_evidence`。聚合接口可在 `best_effort` 下保留部分事实，但有硬错误时不能报告成功；Backend 不因属性核验失败删除已定位实体，输入预算失败则按对应实体切片归因。

## 长文本请求预算

- `KG_EXTRACT_V31_WINDOW_CHARS=2000`：上游切片内部的请求窗口，重叠 100 字符（小窗口不超过四分之一）；不改上游 chunk ID，证据偏移回映原切片。所有字符都被覆盖，不再使用 repair 的 `[:8000]` 静默截断。
- `KG_EXTRACT_V31_MAX_INPUT_BYTES=48000`：完整序列化消息的上限，含 System、skill、本体、元数据、来源和累计状态。UTF-8 字节数用作保守 token 上界，并取请求/环境 `max_input_tokens` 的较小值，另留 2048 字节重试空间。
- 窗口还参考模型输出 token 上限；单次超时、输出截断或上下文超限会拆分来源/类型后重试，最多递归 12 层；文档总 deadline 耗尽后不再发起调用或递归拆分。大本体按类型分批访问全部允许类型，不因文本未命中词面提示而删掉某类。
- 累计属性及其证据本身超过最小请求预算时，明确返回 `LLM_INPUT_BUDGET_EXCEEDED` 并保留已有事实，不截断证据、不无限重试。不能保证任意大实体或任意小模型上下文均能完整抽取。
- 按请求窗口并发，单个超长切片也能使用配置的 worker 数；惰性生成窗口，至多预提交 `2 * max_workers` 个任务。完整提示词仅保留前 `KG_EXTRACT_V31_TRACE_CALLS=32` 次。其余调用保留输入字节数、耗时、状态及 usage，避免长文诊断信息无限复制输入。


调度器的并发等待、QPM 等待及重试退避都受文档 deadline 限制；取得调度名额后、模型/修复回调返回后再次检查，迟到结果不按成功接受。任意自定义同步 LLM 或回调的内部执行仍需其自身实现调用超时。模型提供方异常只保留分类后的诊断信息，不将原始异常中的认证头、请求体写入结果或日志。

## 维护领域 Skill

### 2. 用户维护哪些文件

按本轮范围要求，全部实现及默认技能都放在 v3.1 内：

```text
algorithm/knowledge_extract/v3_1/skills/
  official_document/SKILL.md
  rule/SKILL.md
  trouble/SKILL.md
```

三份文件分别对应公文、规则和故障检修 benchmark；公文技能包含表单的条件规则。路由只输出公文、标准或 generic；rule/trouble 保留为资产但不再自动命中，标准暂不提供 skill。它们是应用运行资产，不是 Codex 个人技能，不需要安装到 Codex。

默认使用本包的 `skills/`。调用公共入口时可传 `skills_dir="/path/to/skills"` 替换目录；它必须是进程实际可读路径。没有新增环境变量或修改根配置。容器源码只读时，在宿主机维护对应源码目录。技能随外部源码交付，不需要重建环境镜像；同一路径文件更新后，新请求读取新内容，已开始请求继续使用已加载内容。

新增技能文件使用 `SKILL.md`（也接受 `skill.md`），以名称或 `route_hints` 绑定已支持的文档类型。新增文档类型需要同步扩展确定性分类器与测试，模型不能跨类型选技能。

### 3. 文件格式

```markdown
---
name: equipment_record
description: 设备维护记录，按设备、工作事项与责任主体识别候选。
version: "1"
stages: [entity, property]
route_hints: [trouble]
---

说明：领域规则由程序按阶段装配，不改写 System、本体、原文或输出契约。

## rules:entity

- 保留原文明确描述的设备与维护事项；不得把日期、编号等标量值单独建实体。
- 类型只使用本体允许的标识。

## rules:property

- 综合同一事项的有关记录补充已接受实体的缺失属性。
- 不得猜测未记录的处置结果。
```

字段约定：

| 字段或规则段 | 含义 |
| --- | --- |
| `name` | 唯一技能 ID，小写字母开头，之后允许字母、数字、下划线、短横线，最长 64 字符。 |
| `description` | 供维护者阅读的描述；说明适用内容和抽取目标。 |
| `version` | 非空字符串，请用引号；语义修改后更新版本。程序另记录真实内容 SHA256。 |
| `stages` | 支持的阶段，从 `entity / property / repair` 选择。实体修复组合 entity、property 和 repair 的适用规则段。 |
| `## rules:all` | 跨该技能所声明阶段适用的规则段。 |
| `## rules:entity` 等 | 对应阶段规则段；程序组合 `all` 与当前策略阶段的段。 |
| `route_hints` | 可选字符串数组。声明绑定的文档类型；名称或该字段匹配才被程序选中。simple/generic 始终禁用技能。 |

二级 `rules:` 标题是规则段边界。其它说明文字只供维护者阅读；只有适用规则段正文会进入抽取 User。技能目录不存在、为空、缺少对应类型/阶段时返回空策略，无需模型规划；原文、本体和输出契约继续传给抽取模型。空规则段、重复段、无效元数据、重复 ID 或跨目录符号链接仍明确报配置错误，不能伪装成空抽取结果。

### 4. 请求内只编译一次

程序按 V2 同等逻辑，根据本体和受限切片上下文确定路由一次，再按 skill 名称或 `route_hints` 匹配规则文件。一次性预编译 entity、property、repair 阶段的 `all` 与阶段规则；实体候选与核验组合 entity/property 规则，属性补全使用 property 规则，实体修复还追加 repair 专用规则；all 段只发送一次。

不再发送 `read_skills` / `assemble` 模型请求。所有切片、批次及重试复用已编译策略，且每次独立抽取请求仍在 `domain_policy` 中携带一份适用规则。任务、本体、原文、输出契约保持各请求自身的内容，不参与技能缓存键。

审计 `profile.prompt_assembly` 的 mode 为 `deterministic_skill_compile`，`plans` 只有一条请求级记录，包含 route、read_skills（ID、版本和原始文件 SHA256）及 stages（各阶段选中规则段）；calls 和 attempts 固定为空。技能配置错误显式失败，不回退成空结果。


## 测试、参考与发布状态

```bash
python -m pytest -q algorithm/knowledge_extract/v3_1/tests \
  tests/backend/test_knowledge_extract_v31.py \
  tests/backend/test_knowledge_extract_adapter.py \
  tests/backend/test_extraction_request_config.py \
  tests/backend/test_extraction_quality.py
```

2026-10-12 提交前，上述算法与 Backend 测试合计 **387 passed、1 skipped**；跳过项缺少 `test_data/eval/standard/ontology/Standard_Ontology.puml`。本轮只完成本地回归并同步 88 Git 仓库的 `develop`，未部署服务、未验证真实模型效果与耗时。此前 88 测试属于[历史 P1/P2 记录](../../../docs/算法/知识抽取V3.1-P1P2修复与88验证-20261012.md)，不能作为本轮发布证明。

基准数据放 `test_data/`，线上评测走 `eval/run.py`，输出放 `tmp/`；完整发布门禁按根目录 `AGENTS.md` 执行。技能随外部源码交付；本轮未增加生产依赖，不需要重建环境镜像。
