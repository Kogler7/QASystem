# JEV 决策层

本文是后续开发的实现说明。领域微调现在不做。默认抽取仍是 `KG_EXTRACT_ALGO_VERSION=latest`。远程 `develop` 上的 [`algorithm/knowledge_extract/latest/`](../../algorithm/knowledge_extract/latest/) 已是现行 `v3` 的独立副本，不再是窗口抽取 harness。

仓库里 `algorithm/knowledge_extract/v2/jev_*.py` 和 `KG_EXTRACT_JEV_*` 只是早期探索：游戏 checkpoint 上的中文提示，关系分区没有进抽取主路径。那些阈值、文案和接线没有权威性。新工作不读取、不扩展、不打开 `KG_EXTRACT_JEV_ENABLED`。

## 当前仓库里有什么

[`algorithm/knowledge_extract/jev_extract/`](../../algorithm/knowledge_extract/jev_extract/) 复制的是提交 `e9d3015` 时的旧 `latest`（窗口抽取 harness），不是现在这份 v3 形态的 `latest`。`KG_EXTRACT_ALGO_VERSION=jev` 会进入这份副本；默认仍是现在的 `latest`。Backend 给 `jev` 传入 v2 的 `ExtractionChunk`，因为该副本还不接受 v3/latest 的字段 dict。

已经落地、并且不改抽取事实的部分：

- `algorithm/jev/`：任务契约、`render_state`、`training_record`、`route`，以及不加载权重的 `DecisionService`。目录存在也只会返回 `domain_checkpoint_not_ready`，不会读取 `/models/nanojev`。
- `algorithm/graph_align/`：短名单、属性冲突和 `link` / `conflict` / `new` 注解。
- `jev_extract` 的 `extract_aggregate`：`KG_JEV_ENABLED=0` 时不增加字段。任务开关打开时只附加 `jev` 影子报告，实体、关系和 `graph` 对象保持不变。
- `eval/tools/jev/validate_jsonl.py`：检查 JSONL 是否符合契约。它不生成标签。

还没有做：领域微调、`KG_JEV_APPLY`、替换 review 大模型、删除关系、把注解写入知识空间。review 大模型调用仍在 [`harness.py`](../../algorithm/knowledge_extract/jev_extract/harness.py) 第 232–277 行。

## 目标

JEV（Qwen3-0.6B decision heads，推理形态是 `state + questions`，一次前向给出 choice 概率）只替换封闭判断，用来保住质量并去掉这些判断上的大模型调用。生成式抽取、补抽和修复继续走现有 LLM。

三处判断：

1. 文档内同指称去重。替换 `jev_extract` 里的 `task='review'` 大模型调用。这是速度收益最大的一步。
2. 关系原文支持。只删除高置信的不支持关系。这是质量门，耗时高于 `latest` 基线则保持影子。
3. 入图对齐。抽取图进入知识空间之前，判断新节点与已有节点是重合、冲突还是不同个体。现行沉淀没有这一步的 LLM：`graph.write` 未开通，`algorithm/conflict_detection` 不在树里。做成独立模块，不进默认沉淀。

## 目录

| 路径 | 职责 | 现在 |
| --- | --- | --- |
| `algorithm/knowledge_extract/latest/` | 默认抽取。不要为 JEV 改这里 | 保持原样 |
| `algorithm/knowledge_extract/v2/`、`v3/` | 其他人的抽取版本 | 保持原样 |
| `algorithm/knowledge_extract/jev_extract/` | `latest` 的可修改副本。同指称和关系过滤只改这里 | 已复制；影子报告已接，不改事实 |
| `algorithm/jev/` | 决策服务。不 import 抽取包，不写图谱 | 契约与跳过逻辑已实现；不加载权重 |
| `algorithm/graph_align/` | 入图短名单、属性冲突、注解。不读 Neo4j，不写库 | 已实现 |
| `eval/tools/jev/` | JSONL 校验。产出写 `./tmp/jev/` | 校验脚本已有；标签导出未做 |

共享文件只允许加法，且默认行为不变：

- [`version.py`](../../algorithm/knowledge_extract/version.py) 增加标签 `jev`，`DEFAULT_VERSION` 仍是 `latest`。
- [`knowledge_extract_adapter.py`](../../backend/app/services/knowledge_extract_adapter.py) 的 `run_aggregate` 增加 `jev -> jev_extract.extract_aggregate`。`latest` / `v2` / `v3` 分支不动。用函数内导入，避免 `latest` 启动去加载实验包。
- `.env.example` 追加默认关闭的 `KG_JEV_*`。不改 `KG_EXTRACT_ALGO_VERSION=latest`，不把 `KG_EXTRACT_JEV_ENABLED` 设为 1。

新开关用 `KG_JEV_*`。不要复用 `KG_EXTRACT_JEV_*`，否则 v2 探索代码会在有人沿用旧变量时被误触发。

评测和联调要显式 `KG_EXTRACT_ALGO_VERSION=jev`。把副本同步回 `latest` 是以后的单独替换，不在这一轮做。

## 配置

代码已经读取这些变量。总开关和三个任务开关默认关闭。`KG_JEV_MODEL_PATH` 指向一个目录时，当前版本仍返回 `domain_checkpoint_not_ready`，不加载该目录里的权重。

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `KG_JEV_ENABLED` | `0` | 总开关。关闭时 `jev_extract` 不附加 `jev` 字段 |
| `KG_JEV_DOCUMENT_COREF` | `0` | 文档内同指称影子 |
| `KG_JEV_RELATION_SUPPORT` | `0` | 关系支持影子 |
| `KG_JEV_GRAPH_ALIGN` | `0` | 入图对齐影子。调用方须另行传入已有节点；不传则该任务记 `no_existing_nodes` |
| `KG_JEV_MODEL_PATH` | 空 | 领域 checkpoint 目录。空或目录不存在时预测结果为 `skipped`，不加载 `/models/nanojev` |
| `KG_JEV_CHOICE_CONFIDENCE` | `0.9` | 低于该概率的 choice 不得改图 |

`remote-only` 不加载本地权重，预测记 `skipped`。

总开关关闭时，结果对象与关闭前相同，不要塞空的 `jev` 键。任一任务打开时，只增加报告，不改 `entities` / `relations` / `graph`。用判断改图要等领域 checkpoint 通过下文的评测门禁，并另加一个默认关闭的替换开关（建议名 `KG_JEV_APPLY`）。在那之前，即使置信度很高，也只把提议写进 `result["jev"]`。

JEV 不可用、超时或低置信时：不合并、不删关系、不把新节点挂到已有节点。确定性底线保持：`(type, name)` 精确合并，以及分片 uid / 三元组去重。不回退到游戏权重。替换开关打开之前，review 大模型仍按现在的 `harness.py` 运行，避免没有领域模型时质量下降。

## 决策服务

新建 `algorithm/jev/`，公开面保持很小：

- `CONTRACT_VERSION = "jev-decision-v1"`
- `get_jev_config() -> dict`
- `render_state(task_id, payload) -> str`，版式以下文为准，训练和推理必须同一函数
- `training_record(...) -> dict`，产出一行 JSONL
- `DecisionService.predict(task_id, payloads) -> Prediction`。`Prediction.status` 为 `decided` 或 `skipped`，`reason` 说明跳过原因，`judgments` 在跳过时为空。
- `route(judgment, task_id) -> "merge" | "drop" | "link" | "keep"`

`Judgment` 至少含 `state_id`、`choice`、`probabilities`、`confidence`、`status`（`decided` 或 `skipped`）、`reason`。

`predict` 在总开关关闭、路径为空、目录缺失或 `remote-only` 时返回 `status=skipped`。目录存在但本版本没有接上领域推理时，原因是 `domain_checkpoint_not_ready`。不要在这个包里实例化抽取 LLM，不要 `import algorithm.knowledge_extract`，也不要加载游戏 checkpoint。测试通过注入 `predictor` 覆盖 `decided` 分支。

推理请求沿用 NanoJev 的题目形状。题目正文放在契约常量里，调用方不能另写一套 criteria：

```json
{
  "id": "pair-1",
  "state": "<render_state 的输出>",
  "questions": {
    "verdict": {
      "type": "choice",
      "instructions": "<任务常量>",
      "criteria": {"same_referent": "...", "different": "...", "uncertain": "..."}
    }
  }
}
```

关系支持题的 question id 用 `label`，选项是 `supported` / `unsupported`。另外两题的 question id 用 `verdict`。

`route` 的规则：

- `status != decided`，或置信度低于 `KG_JEV_CHOICE_CONFIDENCE`：`keep`
- `document_coref` 且 choice 为 `same_referent`：`merge`；其余 `keep`
- `relation_support` 且 choice 为 `unsupported`：`drop`；其余 `keep`
- `graph_align` 且 choice 为 `same_node`：`link`；其余 `keep`
- choice 不在该任务标签集合内：`keep`

`route` 只给出动作名。真正改图的代码在替换开关打开之后才调用，并且仍要经过下面的控制器硬校验。

## 三道题

状态用固定行序、UTF-8、属性键排序。空字段写空字符串，不要省略行，避免训练分布和线上不一致。

### document_coref

只比较同一 `type`、规范化名称不完全相同、且名称有实质重叠的实体。规范化：去掉空白并 casefold。实质重叠：较短名称长度至少为 2，且一方包含另一方，或双方共享一个长度至少为 2 的连续片段。`(type, name)` 完全相同的对交给现有精确合并，不要送进 JEV。

选项：`same_referent`、`different`、`uncertain`。

```text
task: document_coref
type: {type}
left_name: {left_name}
right_name: {right_name}
left_properties: {按键排序的 JSON}
right_properties: {按键排序的 JSON}
left_quote: {左侧首条 quote，最多 180 字}
right_quote: {右侧首条 quote，最多 180 字}
```

指令：判断两侧是否为给定 type 下的同一指称。名称包含、缩写或别名可以是同一指称。上下位、整体与部分、不同个体选 `different`。原文或属性不足以判断时选 `uncertain`。

高置信 `same_referent` 以后组成 `merges`，交给副本里的 `Registry.apply_merges`（[`facts.py`](../../algorithm/knowledge_extract/jev_extract/facts.py) 第 299 行）。属性冲突、低置信和自环仍由该函数拒绝。JEV 不判断属性值是否冲突。

插入点就在 `jev_extract/harness.py` 第 232 行的 `if semantic_review`。替换开关关闭时保持现有 review LLM。打开且本任务 `decided` 之后，用路由为 `merge` 的对调用 `apply_merges`，并跳过第 252 行的 `invoke(..., 'review', ...)`。`skipped` 时仍走原来的 review LLM，直到一次对照评测证明领域模型可以单独承担合并。

### relation_support

对已经进入 registry、且两端名称能在同一原文窗里定位的关系提问。JEV 不生成关系和 quote。

选项：`supported`、`unsupported`。

```text
task: relation_support
window: {含两端名称的原文，最多 180 字}
source: {from_name} ({from_type})
target: {to_name} ({to_type})
relation: {rel_type}
relation_note: {本体中该关系的说明，没有则空}
```

指令：只根据 window 判断这条本体关系是否成立。共现、列举、否定或标题并列而没有关系证据时选 `unsupported`。

只有替换开关打开且路由为 `drop` 时才从副本的 registry 删除该关系。影子阶段只记录。

### graph_align

新节点与调用方传入的已有节点。候选不是全图：同一类型且名称满足上面的重叠条件，已有节点过多时每侧保留最短的若干条（实现时定一个上限，建议 8）。知识空间检索可以以后把 top-k 填进这个短名单，检索本身不属于 JEV。

选项：`same_node`、`distinct`、`uncertain`。

```text
task: graph_align
type: {type}
new_name: {new_name}
existing_name: {existing_name}
new_properties: {JSON}
existing_properties: {JSON}
new_quote: {最多 180 字}
existing_uid: {已有节点 uid}
```

`same_node` 且两侧同名属性都非空且值不同：注解为 `conflict`，不覆盖已有值。值是否相同用去空白后的字符串比较；语义等价留给以后，不要在第一版里交给 JEV。`same_node` 且无属性冲突：注解为 `link`，带已有 uid，不新建。`distinct`、`uncertain`、`skipped`：注解为 `new`。

`algorithm/graph_align/` 只被 `jev_extract` 在影子报告里调用。不要改 [`algorithm/knowledge_precipitation.py`](../../algorithm/knowledge_precipitation.py)。写接口开通前，注解停在 `jev` 版本的结果里。

## 影子报告

`KG_JEV_ENABLED=1` 时，`jev_extract.extract_aggregate` 返回的聚合结果增加：

```json
{
  "contract": "jev-decision-v1",
  "tasks": {
    "document_coref": {
      "status": "skipped",
      "reason": "checkpoint_missing",
      "proposals": []
    }
  }
}
```

`proposals` 里每条含两端标识、`choice`、`confidence`、`route`。跳过时 `proposals` 为空，`candidate_count` 仍记录可送审的对数。`graph_align` 的条目再加 `annotation`：`link`、`conflict` 或 `new`。报告不得写进实体属性。

## 训练

初始化用 NanoJev 的 `stage1`，不要用 `unified-games-v1`。游戏数据不混入。训练脚本是 checkpoint 自带的 `scripts/train_pipeline_decisions.py`，目标 `gold_distribution`，精度 bf16，单卡即可。权重只写入 `HOST_MODELS_DIR` 下的新目录，不进 `qasystem-env-v1`，不覆盖 `/models/nanojev`。

每行 JSONL：

```json
{
  "id": "doc1-coref-1",
  "state_id": "doc1",
  "split": "train",
  "state": "<与 render_state 相同>",
  "questions": {"verdict": {"type": "choice", "instructions": "...", "criteria": {}}},
  "gold_probs": {"verdict": {"same_referent": 1.0, "different": 0.0, "uncertain": 0.0}},
  "gold_probs_kind": "observed_label",
  "metadata": {"task": "document_coref", "contract": "jev-decision-v1", "source_group_id": "doc1"}
}
```

`split` 只使用 `train`、`dev`、`test`。按文档或知识空间切分，同一文档的改写不能跨 split。`gold_probs` 为 one-hot，拿不准的样本标签是 `uncertain` 而不是丢弃。

标签来源：评测 golden 里应视为同一节点却被拆开的对，以及同类型、名称相近但 golden 保持分开的对；golden 关系五元组相对同一窗口里的共现负例；知识空间中同指称与近名不同体的样例。导出脚本放 `eval/tools/jev/`，先调用训练器的 `--validate-only`。若训练器拒绝某个字段，只改 `training_record`，并同时改推理请求，使两侧仍一致。

这一步依赖 GPU 和人工确认过的标签，不在当前实现范围内。

## 实现顺序

1. 修正 `jev_extract/__init__.py` 的说明，标明分叉提交 `e9d3015`。补 `algorithm/jev` 与 `algorithm/graph_align` 的包和单元测试。此步不改抽取结果。
2. 注册版本 `jev`，并给适配器加一条惰性分发。补测试：默认仍是 `latest`；`jev` 调到 `jev_extract.extract_aggregate`；`v9` 仍拒绝。
3. 实现 `render_state`、`training_record`、`route`、名称重叠和属性冲突。用固定字符串断言版式，避免以后训练数据对不上。
4. `extract_aggregate` 在总开关关闭时返回值与现在一致；打开且没有 checkpoint 时增加 `skipped` 报告，实体和关系不变。
5. 有了 dev 标签之后再训练。影子提议的合并精确率稳定前，不要加 `KG_JEV_APPLY`。
6. 只在副本里用 `apply_merges` 替换 review LLM。然后做关系删除。入图注解仍然不写库。
7. 评测必须显式 `KG_EXTRACT_ALGO_VERSION=jev`：`python3 eval/run.py --match-mode llm --fail-on-empty --min-f1 0.5`。关系 F1 或单 case 耗时相对 `latest` 基线变差，就关掉删除、只留影子。
8. 三关都过才放宽 `KG_JEV_CHOICE_CONFIDENCE`。不要把 JEV 放进生成式抽取，也不要在同一次改动里把副本同步回 `latest`。

测试放在 `algorithm/jev/tests/`、`algorithm/graph_align/tests/`、`algorithm/knowledge_extract/jev_extract/tests/`，并把这些目录加进根目录 `pytest.ini` 的 `testpaths`。不要改 `tests/backend/test_v2_jev.py`，也不要改现有 latest/v2/v3 测试的期望。

## 不要做的事

- 不要改 `latest`、`v2`、`v3` 的算法正文来试 JEV。
- 不要把游戏 checkpoint 或 v2 harness 的 criteria 当作领域模型。
- 不要在 JEV 不可用时合并、删关系或链接已有节点。
- 不要让决策服务依赖抽取 LLM、沉淀流程或图谱写入。
- 不要把训练脚本、JSONL 和评测输出放进 `algorithm/knowledge_extract/`。
