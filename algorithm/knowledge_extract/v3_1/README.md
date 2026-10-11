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
- `object_identity.py`：依据原文 object 声明统一名称与引用，保留文档范围和对象编号。

User 固定为 `task`、`output_contract`、`domain_policy`、`ontology`、`documents`。第一步只输出 `type + mention + evidence.quote`，只读取当前 chunk；后续阶段按原有规则使用已接受实体、属性和局部上下文。`name / properties` 是后处理结果，不是第一步模型字段。

公共入口自动执行“模型请求读 skill → 模型生成计划 → 程序保真装配”。通过 `skills_dir` 参数可替换技能目录；默认使用本包的 `skills/`。维护方式与运行边界见下文。

User 保持原五段 JSON 模板；各顶层及嵌套字段的含义见[User 字段字典](../../../docs/算法/知识抽取V3.1提示词与文档路由.md#11-user-原模板与字段字典)。

完整说明、精简清单、用法和真实模型样例见[提示词与文档路由](../../../docs/算法/知识抽取V3.1提示词与文档路由.md)。

## 测试与评测

测试统一在 88，使用现有 `qasystem_backend`，不另建评测 Backend：

```bash
python -m pytest -q algorithm/knowledge_extract/v3_1/tests
```

2026-10-11 删除关系抽取后的相关算法及 Backend 契约测试：本地 265 passed、1 skipped（缺少标准本体）。本次未部署到 88。完整发布门禁仍按根目录 `AGENTS.md` 执行。

基准数据放 `test_data/`，线上链路评测统一走 `eval/run.py`，输出放 `tmp/`。历史 benchmark lint 公共工具保留在 `algorithm.knowledge_extract.v2.benchmark_lint`；v3.1 不再重复维护同一工具。

---
noteId: "1dbff8b0c54d11f192edfbc308186266"
tags: []

---

### 用户维护 Skill 与 User 装配

更新时间：2026-10-11。实现范围是用户维护 Markdown 技能、程序按 V2 路由编译一次、保真填充 User。当前接入 v3.1 公共 Python 入口；尚未部署到 Backend。全部本轮源码、测试、技能与说明均位于 v3.1，未改公共运行层和其它算法版本。

### 1. 执行流程与职责

```text
用户维护若干 SKILL.md
        ↓ 每个抽取请求读取一份固定内容快照
V2 规则确定类型（本体优先，必要时结合前 12 片的受限正文与元数据）
        ↓ 按类型匹配技能
程序一次性预编译各阶段规则，记录版本和 SHA256
        ↓ 各切片、批次复用，只填充 domain_policy
公共抽取 System + 阶段约束 + 五段 JSON User
        ↓ 原有抽取链路
分片实体候选 → 按 type + name 集中来源 → LLM 跨片合并与属性补全 → 实体校验与修复 → 输出实体
```

装配过程不调用模型。请求内持有固定规则快照，各阶段、切片和批次只复制对应策略，输入变化不会触发重新选择或编译。

抽取 System 使用公共规则加阶段约束：实体、属性各自接收对应规则，实体修复另有限定问题范围。程序原样保留以下四段，只替换 `domain_policy`：

- `task`：当前阶段、已知实体、修复问题及允许的范围。
- `output_contract`：独立构建的输出契约及 schema。
- `ontology`：该阶段已裁剪的本体目录。
- `documents`：当前 chunk 与该阶段原有允许的邻居。

第一步仍只输出 `type + mention + evidence.quote`，不输出 `name`、属性或关系。原文位置由实际文档和既有后处理决定，技能编译不计算或改写位置。

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
| `stages` | 支持的阶段，从 `entity / property / repair` 选择。实体修复沿用 entity 的领域规则。 |
| `## rules:all` | 跨该技能所声明阶段适用的规则段。 |
| `## rules:entity` 等 | 对应阶段规则段；程序组合 `all` 与当前策略阶段的段。 |
| `route_hints` | 可选字符串数组。声明绑定的文档类型；名称或该字段匹配才被程序选中。simple/generic 始终禁用技能。 |

二级 `rules:` 标题是规则段边界。其它说明文字只供维护者阅读；只有适用规则段正文会进入抽取 User。技能目录不存在、为空、缺少对应类型/阶段时返回空策略，无需模型规划；原文、本体和输出契约继续传给抽取模型。空规则段、重复段、无效元数据、重复 ID 或跨目录符号链接仍明确报配置错误，不能伪装成空抽取结果。

### 4. 请求内只编译一次

程序按 V2 同等逻辑，根据本体和受限切片上下文确定路由一次，再按 skill 名称或 `route_hints` 匹配规则文件。一次性预编译 entity、property、repair 阶段的 `all` 与阶段规则；实体修复仍复用 entity 规则。

不再发送 `read_skills` / `assemble` 模型请求。所有切片、批次及重试复用已编译策略，且每次独立抽取请求仍在 `domain_policy` 中携带一份适用规则。任务、本体、原文、输出契约保持各请求自身的内容，不参与技能缓存键。

审计 `profile.prompt_assembly` 的 mode 为 `deterministic_skill_compile`，`plans` 只有一条请求级记录，包含 route、read_skills（ID、版本和原始文件 SHA256）及 stages（各阶段选中规则段）；calls 和 attempts 固定为空。技能配置错误显式失败，不回退成空结果。

### 5. 如何调用

既有公共调用自动启用确定性编译：

```python
from algorithm.knowledge_extract.v3_1 import extract_harness
from algorithm.knowledge_extract.v3_1.chunk_context import ExtractionChunk

result = extract_harness(
    ontology=ontology_text,
    chunks=[ExtractionChunk(chunk_id="example-1", text=chunk_text)],
    llm=runtime_llm,  # 应用注入的模型运行接口
    use_scheduler=False,  # 仅当 runtime_llm 已统一管理调度时使用
)
plans = result["profile"]["prompt_assembly"]["plans"]
```

`extract()`、`extract_aggregate()` 都经该入口启用编译。`extract()` 只保留最终公共图结果，调试时用 `extract_harness()` 查看请求级编译审计和技能哈希。

直接调用低层 `extract_split()` 同样按请求编译一次；单独调用 `build_chat_messages()` 时默认生成一次性预览，也可传入请求内共用的 `assembly_planner`。

可注入独立规划器：

```python
from algorithm.knowledge_extract.v3_1.skill_registry import SkillRegistry
from algorithm.knowledge_extract.v3_1.skill_planner import SkillAssemblyPlanner

# 每个抽取任务创建新实例，不再接受 LLM runner。
planner = SkillAssemblyPlanner(SkillRegistry("/path/to/skills"))
result = extract_harness(
    ontology=ontology_text, chunks=chunks, llm=runtime_llm,
    assembly_planner=planner, use_scheduler=False,
)
```

规划器按请求创建，不跨请求复用。全部阶段预编译一次，不随 chunk 或实体批次重新编译；技能文件在请求开始时加载，运行过程中不会因文件编辑而更换规则。

### 6. 与底层调度的边界

`skill_registry.py`、`skill_planner.py` 不导入其它算法版本，不创建模型客户端或调用模型。实际抽取仍沿用既有 `generate_structured()` 调度与重试路径。

**技能装配层不等于全系统调度解耦。**版本目录下原有 scheduler/client 和共享 `request_runtime` 对版本客户端的依赖仍然存在。合并后的 Backend 适配器已将输入转换为 v3.1 自身的 `ExtractionChunk`；算法与 Backend 接口回归测试通过，但不代表运行中服务已部署或通过完整发布门禁。

### 7. 验证与成本

历史首片路由与 benchmark 分类调整（本次已替换为 V2 规则）在本地 `.venv/bin/python -m pytest -q algorithm/knowledge_extract/v3_1/tests` 验证：188 passed、1 skipped（缺少标准本体）。覆盖五份公文 benchmark 首片、JSON 表单、类型隔离、空目录、simple 空技能与跨 chunk 不改路由。

上述本地验证时，全仓测试收集曾因缺少 `docling` 和旧 `algorithm.knowledge_extract.v3_1.decisions` 引用失败；合并后的受版本控制 Python 源码已无该旧模块引用。尚未执行本次改动的运行中 Backend 全量评测、基线对比和部署，不能视为已通过发布门禁。

2026-10-11 冲突修复后，在 88 的现有 `qasystem_backend` 中使用独立源码快照验证 v3.1 全部单测，以及 Backend 的 `test_knowledge_extract_v31.py`、`test_knowledge_extract_adapter.py`、`test_extraction_request_config.py`、`test_extraction_quality.py`：267 passed、1 skipped（仓库缺少标准本体）。保留了换行符/BOM 与原始文件哈希测试、文档类型隔离测试和公文实体阶段规则。日志归档于 `tmp/v31-conflict-fix-20261011/`，未替换运行服务源码。

以下为此前装配层在 88 上的历史验证记录，不代表本次修改已在服务器验证：

测试统一在 88 的现有 `qasystem_backend` 内，独立快照 `/app/tmp/v3_1-skills-local-20261011-miuTD2`。相关命令：

```bash
python -m pytest -q -p no:cacheprovider \
  algorithm/knowledge_extract/v3_1/tests
```

结果：165 passed、1 skipped、15 subtests passed。跳过原因仍是缺少既有标准本体 fixture。覆盖自定义技能发现、文件编辑生效、请求内快照、规则段选择、非法计划拒绝、原文与契约保真、四阶段消息装配、公共入口接线及并发重复规划合并。

真实模型初测发现服务商可能退回 `json_object` 模式：规划提示词必须显式携带自己的 `response_schema`，不能只通过 SDK 参数传入。本次已补齐并加入回归断言。规划契约与后续抽取契约明确区分。

采用模型规划会增加推理成本：有相关技能时通常新增两轮请求（选读和计划），类型无技能时不增加规划调用；有候选技能但模型选空时新增一轮。请求内仅对完全相同输入复用计划，不声称整体提速。尚未执行全量效果评测或实际服务发布。


真实模型 `deepseek-ai/DeepSeek-V4-Flash` 已完成选读公文技能、生成计划和实体抽取三轮调用，本次耗时 4.562 秒。输入为“关于开展设备巡检的通知\n设备部负责开展设备巡检。”，选用 `official_document` 的 `all` 规则段，返回公文标题、设备部、开展设备巡检三个实体，流程状态 succeeded。输入与第一步契约保留原样，三个实体的模型原始 offset 均触发既有纠正逻辑；简化本体还产生三条缺少 identity 的告警。此样例只验证单段算法包链路，不代表 Backend HTTP 整链或全量效果门禁通过。

本次验证产物保存在根目录 `tmp/v3_1-skill-builder/88-version-local/`，其中 `v3_1-skills-local-results.xml` 是测试结果，`v3_1-skills-local-live/` 包含真实 System、User、模型首步原始输出、调用记录和最终结果。它们是测试产物，不进入运行源码包。

### 按 type + name 完成实体

属性调用以完全相同的 `type + name` 为键，每个键只进入一个批次（同一类型默认每批 24 个唯一键），相关切片完整放入 `documents`。`task.known_entities[].occurrences` 保留每次出现的切片 ID、原始属性与证据，不预先拼接或覆盖属性值。LLM 结合全部来源，为每个键返回一个完整的 `type`、`name`、`properties` 与 `evidence.quote`。同名不同类型分开；不做别名、模糊名称归并。

程序校验键集合、唯一性、本体属性和证据，再收集模型结果；不生成抽取实体 ID，不执行属性 updates 或程序属性合并。旧 Backend 图输出协议所需的 `uid` 仅在兼容输出边界计算，不进入 LLM message 或公开实体 JSON。`output_contract` 不含 `version`、`status`。候选非空时，属性响应不得返回空实体集合。

User 的诊断字段在序列化边界移除：ontology 只发 catalog，domain_policy 只发 profiles 下的 extraction_rules，task 不再发送 input_scope。技能选择与版本审计仍保留。属性 schema 按单一类型生成，避免跨类型属性混用；实体和修复保持 span 候选结构，属性返回完整实体结构。
