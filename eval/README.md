---
noteId: "c7cee6b0c2e911f1aea7cbe984ab9484"
tags: []

---

# 知识沉淀评测

改完解析、切片、抽取或沉淀流水线后，在仓库根目录对指定 case 跑一次脚本。默认只测后台 HTTP，不 `import algorithm` / `backend`。

默认连 `http://127.0.0.1:8006`。Backend 已在跑时，在仓库根目录执行即可；评测数据在 `test_data/eval/`，不要只拷 `eval/` 目录。

抽取算法版本默认由 Backend 的 `KG_EXTRACT_ALGO_VERSION` 控制。现在也可在脚本中传 `--extract-version v2/v3/v3.1/latest`、`--extract-provider <服务端配置名>`、`--extract-model <模型>` 和超时覆盖项，在同一个 Backend 上逐次选择配置，无需重启。完整规则和供应商登记方式见 [抽取请求配置覆盖](../docs/接口/抽取请求配置覆盖.md)。MCP 工具仍使用服务端默认配置。

## 做了什么

**评测集。** 按文档类型放 case，每个 case 有原文件和标准答案：

```text
eval/
  run.sh / run.py / params.yaml
  mini_eval.sh / mini_eval.py  # 每类 1–2 个 case 的小样本入口

test_data/eval/
  评测数据
  <类型>/
    ontology/                 # 该类型共用本体
    <case>/
      docs/                   # 原文件（pdf / docx / xlsx / png …）
      extract/golden.json     # 抽取标准答案
      ontology/               # 可选；有则覆盖类型级本体
```

当前正式集在 `test_data/eval/`，类型目录是英文名：

| 类型目录 | 含义 | case | 说明 |
| --- | --- | --- | --- |
| `official` | 公文 | 5（`official_01` … `official_05`） | 多数含通知 PDF + `表单.pdf` 两个附件 |
| `rule` | 规则 | 1（`rule_01`） | 有 golden，会评分 |
| `simple` | 简单规则/术语 | 1（`Simple_故障规则的诊断`） |  |

不再使用中文类型目录名（`公文`/`标准`/`规则`）。`中车RAG验证数据集/` 若出现会被扫描跳过。

`run.py` 的参数文件默认是 [`eval/params.yaml`](params.yaml)。若 `test_data/eval/params.yaml` 存在则覆盖它。`run.sh` 只转调 `run.py`，工作区默认仍是 `test_data/eval`。下文的 `mini_eval` 入口默认固定读取 `eval/params.yaml`。

**评测模式。** 后台评测归一化成三类（`--mode`，可组合）：

| 模式 | 含义 | 调用 | 输入 |
| --- | --- | --- | --- |
| `extract` | 仅知识抽取 | `POST /api/v1/graphs/extract` | `--from-run` 复用的切片（chunks.json） |
| `chunk-extract`（默认） | 切片+抽取 | `POST /api/v1/documents/chunk` + extract | docs/ 现解析，或 `--from-run` 复用 document.md |
| `pipeline` | 全流程知识沉淀 | `POST /api/v1/pipelines/knowledge-ingestion` | docs/ 原文件 |

`extract` 模式只重跑抽取这一步，切片固定不变——这是对比抽取算法版本（v2/v3/v3.1）的推荐方式：先跑一次 `chunk-extract` 冻结切片，再让待比较的两个版本都用 `--mode extract --from-run <冻结批次>` 运行，保证两边模式与输入一致。插件走 MCP `kg_extract_v2`，同样受版本开关控制。

**模块通过标准。** 解析 / 切片只验接口成功且输出非空，尚无完整语义质量标注；抽取对照 `golden.json` 打 P / R / F1 / Jaccard。Jaccard 为 `TP/(TP+FP+FN)`，旧 JSON 字段 `accuracy` 保留为兼容别名，不代表普通分类准确率。复用的中间产物在 scores.json 里记 `status: reused` 与来源路径。

插件侧没有独立的 parse / chunk 工具：插件阶段复用后台解析出的 Markdown；若只跑 `--stage plugin`，会先调一次解析接口再调 MCP。

**打分。** 默认 LLM-judge：在本体类型硬约束下判断语义一致。实体看 `type` / `name` / `properties`，关系看 `rel_type` / 两端点 / `properties`，并结合两侧端点实体属性判断指代：

- 实体 `type` 以及关系 `rel_type`、`from_type`、`to_type` 分别要求经过 NFKC、casefold、去空白与下划线后完全一致。只容忍这些书写差异，不允许 judge 自由推断类型同义词或单复数等价。程序会硬过滤跨类型候选，并在 `rejected_type_pairs` 记录原集合索引。
- `name` 指向同一现实对象即可：同义表达、别名、简称与全称、规范名、标点/空格/大小写/全半角/繁简差异，以及不改变指代的修饰（如"的通知""（试行）"）都算一致。
- `properties` 只在**两侧都填写了**的属性键上比较，值语义一致即通过（数值 100 与 100.0、日期与单位的不同写法、全半角与空格差异都算一致）。只有一侧填写（缺失、空串、空数组）**不算冲突**；双方详略不同但内容不矛盾（一方是另一方的节选/摘要/更完整的抄录）**不算冲突**；只有互相排斥的取值（最大值 5% 与 10%、发布机构不同）才判为不匹配。
- 本地自动匹配对属性值采用更保守的归一化：保留 `+`、`-`、`.`、`/`、`%` 等符号，不会把 `-5` 与 `5`、`1.0` 与 `10`、`5%` 与 `5` 合并为相同值；不同写法是否语义等价仍交给 judge 判断。
- 关系 judge 同时接收 golden 与 predicted 各自的端点实体 `properties`。上下文按当前窗口的 `(type, name)` 去重，支持 golden 用“排查要求二”这类短标签、预测用正文表述的情况；同一实体键下的不同属性候选分别保留，不拼成新实体。上下文只辅助判断，不依据已选实体映射自动增加关系 TP。
- `evidence` 从不参与匹配。

匹配是一对一，并按**最大基数**求解：`(g0,p0)/(g0,p1)/(g1,p0)` 判 2 对，而不是先到先得只判 1 对。

批量判定分三步，候选边统一做全局一对一匹配：

1. 条目身份经过格式归一化后相同、满足本体类型硬约束且两侧共享属性值归一化相同的项，形成全部本地候选边。名称归一化只移除指定空白、括号和标点，不做字面相似度预筛；数字语义符号保留，属性值采用上面的保守规则。若本地最大匹配已达到 `min(golden数量, predicted数量)`，命中数不可能再增加，该集合无需调用 judge。
2. 本地匹配未饱和时，按 **完整 golden 窗口 × predicted 窗口** 扫描，保留本地已匹配项，避免提前锁死顶点而漏掉全局最优匹配。`judge_golden_batch_size`（默认 40）条 golden × `judge_batch_size`（默认 20）条 predicted 组成一次 prompt，每对条目在本轮扫描中出现一次。judge 返回所有认可的候选边，不能在每个窗口内先做一对一裁剪；程序剔除违反本体类型约束的候选，再合并本地与 judge 候选统一求最大匹配。双侧窗口避免把数百条 golden 全塞进单次 prompt；`judge_max_concurrency`（默认 4）控制并发，每个 worker 使用独立客户端。
3. **聚焦补判**：身份归一化一致、类型相容，只因属性或关系端点上下文冲突而无法本地确认的候选，单独组成小窗口再判一次。这些候选只能由 judge 决定指代，而整窗提示（一次数百对）下模型会整窗回空数组，空数组能通过 schema 校验，会把它们静默丢成漏抽。补判只增加候选边，最终仍由统一最大匹配决定，不放宽判对口径；本地匹配已饱和时不调用 judge，也不做补判。

实测依据：88 机 `official_02` 关系窗口一次送审 40×8=320 对，judge 返回 HTTP 200 + 空数组，两条五元组与 golden 逐字一致的候选被判成漏抽、关系 F1 归零；同模型同数据换成 2×2 小窗口后立即判出这两对。补判后该 case 关系 TP 由 0 恢复为 2，实体侧另恢复 1 条。

summary 里另附 identity（type+name 精确集合）对照。judge 失败仍保存 `identity_fallback` 分数和诊断，但剔除出正式 LLM `overall` / `quality` 汇总，并使质量门禁失败（无执行失败时退出码 4）。需要纯精确匹配时显式加 `--match-mode identity`。没有有效 golden 的 case 仍会保存预测，但不计分；默认 LLM 评测要求所选 case 和模块都有有效评分，详见「质量门禁」。

两种口径的逐条结果（对上/漏抽/多抽清单、judge 每批耗时）落在 `match_details.json`。`judge_coverage` 记录 `golden` / `predicted` / `auto_matched` / `auto_candidate_pairs` / `pairs_total` / `pairs_presented` / `windows` / `empty_windows` / `llm_candidate_pairs` / `llm_matched` / `full_sweep` / `rejected_type_pairs`，以及聚焦补判的 `deferred_candidates` / `focused_windows` / `focused_pairs_presented` / `focused_empty_windows` / `focused_candidate_pairs`。`pairs_presented == pairs_total` 只说明所有计划候选出现在裁判输入中，不能证明模型没有遗漏或误判；本地已饱和时两者均为 0，`llm_matched` 也可以为 0。`empty_windows` 只作排查线索，窗口内本来没有匹配时也会为空；`empty_windows` 与 `focused_candidate_pairs` 同时非零就说明整窗空数组确实吞掉过候选（补判已捞回）。`focused_empty_windows` 表示补判小窗口本身也返回空数组——此时是模型给出的有依据的“不匹配”，不再视为漏判。

关系属性在图结构转换时保留。本地关系候选也验证端点上下文：相同五元组但端点共享属性冲突时必须交给 judge；多个上下文候选须全部兼容，缺失上下文沿用缺失属性不冲突规则。另有 `aligned_relations` 按选中的实体映射约束关系端点与方向，**只作诊断，不参与正式关系 F1 门禁**：多解实体映射可能产生不同关系分数，它不是完整图对齐最优值。当前共享属性不冲突不等于属性完备；原文证据支持性也未被主指标覆盖。详细依据及 88 机测试记录见 [评测方案审计与改进](../docs/算法/评测方案审计与改进.md)。

88 机 `official_01` 初评暴露了裁判把部门与活动/交互配对、缺少端点上下文时 8 个关系窗口全部返回空数组的问题。因此该次 F1 不能直接归因于抽取算法。类型硬约束、属性归一化和端点上下文均已改变评分口径，需要重新评分并建立新基线，不能沿用初评数值作版本优劣结论。

Chat 适配器调用 Chat Completions 接口；SystemOne 适配器使用完整 yes/no 选择，缺失或异常选择使评分失败。`eval.judge_response_format: auto` 会为官方 `api.deepseek.com` 选择 `json_object → plain`；其他兼容服务使用 `json_schema → json_object → plain`。仅在服务端明确拒绝当前格式时尝试下一个格式；认证错误、限流、超时、服务端故障及返回内容无效会直接记录评分失败。可设为 `json_object` 或 `json_schema` 显式指定起始格式。DeepSeek 的 Chat 接口只声明支持 `text` / `json_object`；其 Responses 接口使用另一套参数，本客户端未接入。参见 [DeepSeek Chat API](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/)。

`eval.judge_disable_thinking: true` 向官方 DeepSeek 发送 `thinking: {type: disabled}`，向官方 SiliconFlow（`api.siliconflow.cn` / `api.siliconflow.com`）发送 `enable_thinking: false`，其他兼容服务不附加这些厂商参数。参见 [SiliconFlow Chat API](https://docs.siliconflow.cn/docs/api/chat-completions-post)。评分只解析最终 `content`，不会用 `reasoning_content` 代替答案；空内容或被截断的回答判为评分失败。提示词明确索引范围、候选边数量上限与输出结束条件。每条输入显式标注 `golden_index` / `predicted_index`，索引是**窗口内**下标；评分代码通过窗口保存的原索引列表回填全局下标，即使列表不连续也不使用起点加偏移代替映射。所有格式均检查数组结构、整数类型与索引边界；越界响应使评分失败。`judge_batches` 保留每次尝试的格式、状态、响应 ID、结束原因、token 用量及内容长度，不保存模型原始文本或鉴权信息；失败时保留已完成调用的诊断。

预测只写 `tmp/eval/runs/<时间戳>/`，不会覆盖 `test_data/eval/<类型>/<case>/extract/golden.json`。

批量跑多个 case / 多附件时，单个文件或单个 case 失败不会停下后面的任务：该单元最多重试 3 次，仍失败则跳过并记入 `failed_files` / `skipped_after_retries`，已成功的解析、切片、抽取产物下一轮直接复用。Ctrl+C 会先把当前进度写入 `summary.json`（带 `interrupted`）再退出。

## 如何使用

### 准备

1. Backend 已启动（默认 `http://127.0.0.1:8006`，MCP 默认 `{backend}/mcp`）。
2. 默认 LLM-judge 使用 Clef，需要能访问其服务。地址和模型按命令行 → `QASYSTEM_JUDGE_BASE_URL/MODEL` → `eval.judge_base_url/judge_model` → Clef 默认值读取，不再随抽取供应商变化。Chat API key 可兼容 `KG_EXTRACT_LLM_API_KEY`；不把抽取 API key 发给默认 Clef。具体替换方法见下文。只检查链路时显式指定 `--match-mode identity`；`--dry-run` 不调用或要求 judge。
3. 需要鉴权时设环境变量 `AUTHORIZATION`。

地址也可用 `QASYSTEM_BACKEND_URL` / `QASYSTEM_MCP_URL`，或改 `params.yaml`。流水线选项在 `params.yaml` 的 `options`，与生产 `PipelineOptions` 对齐。不要把密钥写进 YAML。

### 命令

**小样本评测（mini_eval，日常迭代先用这个）**

```bash
# 先查看本次选择，不调用 Backend 或 judge
python3 eval/mini_eval.py --dry-run

# 使用 params.yaml 中的评分和流水线配置，运行选中的 case
python3 eval/mini_eval.py

# 固定已有切片，仅重跑所选 case 的知识抽取
python3 eval/mini_eval.py --mode extract --from-run <冻结切片批次> --match-mode llm
```

也可用 `bash eval/mini_eval.sh`，或 `python3 -m eval.mini_eval`。默认扫描 `test_data/eval/`，读取 `eval/params.yaml` 的 `mini_eval` 配置；可通过 `--workspace`、`--params` 显式指定其他位置。mini 入口固定使用这里的参数文件，不自动采用工作区内另一份 `params.yaml`。

```yaml
mini_eval:
  cases_per_type: 1
  per_type:
    official: 2
  selection: first
  seed: 42
```

每类数量只能为 **1 或 2**；候选不足时取全部。`per_type` 可按类型目录名覆盖数量。默认 `first` 按 case 目录名排序选择，当前会选 `official_01`、`official_02` 和 rule、simple 各自唯一的 case，共 **4 个**。完整评测集共 **7 个** case；standard 与 official_06–10 已移出当前评测集。

需要随机抽样时，将 `selection` 改为 `random`：固定整数 `seed` 可在评测集不变时复现选择；设为 `null` 则每次重新抽样。启动时打印策略和 case 列表，实际运行将配置与精确 case 路径保存到 `params.resolved.json` 的 `mini_eval`、`targets` 字段。

mini 入口直接调用统一的 `run.py`，不复制评测数据，不另建评分链路；支持 `--mode`、`--from-run`、`--reuse-extract-from`、`--match-mode`、耗时/F1 门禁等现有参数。额外的位置路径及 `--groups`、`--cases` 不允许与自动抽样混用；配置错误或未选出任何 case 时直接报错，不会回退到全量。结果仍写入 `tmp/eval/runs/<时间戳>/`。mini 用于快速回归，不能替代发布前的全量评测门禁。

---

先给脚本执行权限（只需一次）：

```bash
chmod +x eval/run.sh
```

`run.sh` 只把参数原样传给同目录的 `run.py`。工作区默认 `test_data/eval`，参数默认 `eval/params.yaml`。建议在仓库根目录执行。

---

**先扫一遍评测集（不调服务）**

```bash
./eval/run.sh --dry-run
```

确认能认出 `official` / `standard` / `rule` / `simple` 共 16 个 case。拷到部署机后先跑这一步。`--dry-run` 只扫描选择与配置，不调用服务，也不清理历史批次。

---

**按路径评测（推荐）**

```bash
./eval/run.sh test_data/eval/official/official_01
```

在仓库根目录也可以：

```bash
python3 eval/run.py test_data/eval/official/official_01
```

默认 `--stage backend` + `--mode chunk-extract`：**解析 / 切片 / 抽取并对抽取结果评分**，不跑沉淀、不调 MCP。传类型目录则跑该类型全部 case：

```bash
./eval/run.sh test_data/eval/rule
```

---

**对比抽取算法版本（v2/v3/v3.1）**

默认 `KG_EXTRACT_ALGO_VERSION=latest` 跑的是 `latest` 包（当前与 v3 一致）。要对比 v2/v3/v3.1，先显式固定 Backend 环境并重启服务。下面以 v2 与 v3.1 对比为例；仅在评测脚本进程设置变量不会切换运行中的 Backend：

```bash
# 1. 先生成冻结切片；本批次不是后续 extract 模式的可比基线
./eval/run.sh test_data/eval/standard --mode chunk-extract --match-mode identity
#    记下运行目录 tmp/eval/runs/<时间戳>

# 2. 在 Backend 环境中设置 KG_EXTRACT_ALGO_VERSION=v2 并重启，建立 extract 基线
./eval/run.sh test_data/eval/standard --mode extract --from-run <冻结批次> --match-mode identity

# 3. 在 Backend 环境中设置 KG_EXTRACT_ALGO_VERSION=v3.1 并重启，使用同一冻结输入
./eval/run.sh test_data/eval/standard --mode extract --from-run <冻结批次> --match-mode identity \
  --baseline tmp/eval/runs/<步骤2的批次>
```

步骤 2、3 保持输入、模式与评分配置一致，只切换待比较的抽取版本。`summary.json` 记录服务端回报的 `extract_versions`。标准类 golden 很大，可先用 identity 排查；正式结论应将步骤 2、3 都切为 `--match-mode llm` 重新评测。第一次 `chunk-extract` 不能直接作为 `extract` 的 `--baseline`，两者模式和耗时范围不同。

---

**全量评测（只测后台）**

```bash
./eval/run.sh
```

扫 `official` / `standard` / `rule` / `simple` 下全部 case，默认只跑后台并使用 LLM-judge；缺少评分地址会退出 1。正式开测可先显式 `--match-mode identity` 跑通链路，再用 LLM-judge 正式评测。

**修复评分后补评并继续未完成的 case**

先停止原批次，确认它不再写入结果，再启动新批次：

```bash
python3 eval/run.py --stage backend --mode extract \
  --from-run <原冻结切片批次> \
  --reuse-extract-from <已保存抽取结果的批次> \
  --match-mode llm --runs-retention-days 0
```

此参数仅适用于后台 `extract` 模式，并要求 `--from-run` 提供固定切片。新批次读取并保存旧 `backend_extract.json`，重新评分；尚未保存抽取结果的 case 正常调用 Backend。旧批次保持原样，损坏的旧产物明确报错。仅在本次抽取版本、输入和选项未变、需要修复评分或续跑时使用；对比不同抽取版本时不要复用预测。

复用结果会保留已知的原抽取耗时，并记录来源；原批次尚未写出耗时的结果会标记为未知，不能用来证明性能门禁通过。补评 case 的耗时包含已知的原处理耗时与本次重新评分耗时，不能把仅补评的耗时当作重新抽取的耗时。

严格评分与基线比较还要求来源批次保存 `chunks.json` 和有效的 `manifest.json`，证明旧预测使用了本次相同的切片和本体。缺少这些证据的旧结果可以用于诊断，但不能通过 `--require-scored`；应使用同一冻结输入重新抽取建立新基线。新批次会复制原始切片字节，并在抽取返回后立即保存 `backend_extract.timing.json`，避免评分异常或同批重试把 API 耗时清零。

---

**后台 + 插件**

```bash
./eval/run.sh --stage all
```

每个 case 先跑后台，再跑插件 `kg_extract_v2`。MCP 会话在多 worker 下可能失败，插件回归时再加此项。

---

**全流程知识沉淀**

```bash
./eval/run.sh test_data/eval/official/official_01 --mode pipeline
```

打 `POST /api/v1/pipelines/knowledge-ingestion`（内部走完整 解析→切片→抽取）并对结果评分。也可与模块模式组合：`--mode chunk-extract pipeline`；旧参数 `--system` 等价于追加 pipeline。

---

**只测插件**

```bash
./eval/run.sh --stage plugin
```

只调 MCP `kg_extract_v2`。插件没有 parse/chunk，脚本会先用后台解析接口把 `docs/` 转成 Markdown，再交给插件。适合只改了 MCP 工具封装、抽取算法本身已在后台测过时用。

---

**按类型筛选**

```bash
./eval/run.sh test_data/eval/official
./eval/run.sh --groups official,standard
```

路径与 `--groups/--cases` 不要混用。`--groups` 用目录名：`official,standard,rule,simple`。

---

**按 case 筛选**

```bash
./eval/run.sh test_data/eval/simple/Simple_故障规则的诊断
./eval/run.sh --groups simple --cases Simple_故障规则的诊断
```

---

**替换抽取模型和裁判模型**

普通评测与快速评测默认裁判为 `clef-flash`，使用 `/v1/systemone` 逐候选对选择判断；语义规则、关系端点上下文、最终一对一匹配和裁判失败门禁继续由统一评分器维护。
默认地址取 `CLEF_SERVICE_URL`，未设置则为 `http://127.0.0.1:8188`。88 的 Clef 当前位于 Compose 内部网络：在同网络容器中用 `http://clef:8188`；宿主机或开发机运行评测时，须指定实际可达的服务地址，不要将未开放的宿主端口当成可用地址。

抽取模型和裁判独立配置。例如使用已在 Backend 注册的供应商与模型，并切换到 API 裁判：

```bash
python3 eval/run.py --stage backend --mode extract --from-run <冻结切片批次> \
  --extract-version v3 --extract-provider siliconflow --extract-model deepseek-ai/DeepSeek-V4-Flash \
  --judge-adapter chat --judge-model deepseek-flash --judge-base-url https://api.deepseek.com/v1 \
  --judge-key-env QASYSTEM_JUDGE_API_KEY --case-attempts 1
```

换 Clef 裁判用 `--judge-adapter systemone --judge-model clef-flash --judge-base-url <Clef地址>`。参数优先级：命令行 > `QASYSTEM_JUDGE_*` > YAML > Clef 默认值；密钥仅从环境变量读取，默认 `QASYSTEM_JUDGE_API_KEY`，Chat 可兼容抽取 API key。抽取的 provider 必须预先在 Backend 配置，不从评测命令传入供应商密钥或地址。

组合实验先固定切片、golden、本体和预算，再更换抽取模型。测试不同裁判时可复用同一批预测重新评分，避免重复抽取消耗；更换抽取模型时不能复用旧预测。裁判改变后须分别建立基线，不能用不同裁判的分数直接证明抽取质量提升。manifest 保存抽取配置与裁判 model/base_url/adapter，不包含密钥。

**固定切片的模型组合评测**

完整操作与边界见 [固定切片与模型组合评测](../docs/算法/固定切片与模型组合评测.md)：包含准备/执行分离、四模型方案、更换 judge 补评、切片策略实验、并发和产物说明。

`python -m eval.model_matrix --workspace test_data/eval --params eval/params.yaml --plan <方案.json> --output tmp/eval/matrix/<批次> --workers 4` 会先统一准备切片，再经 `eval/run.py` 比较多个抽取模型。`--prepare-only` 仅准备输入；之后用 `--prepared <准备批次>/inputs` 复用同一份切片。准备阶段遇到解析部分失败或切片兜底会停止，避免悄悄改变比较输入。

方案 JSON 包含 `cases`、`extractors`、`judge` 与可选 `comparison`。每个 extractor 填 `id/provider/model/version`，本地模型可填 `health_url` 等待就绪；judge 填 `id/adapter/model/base_url`，API key 仅用 `key_env` 指定变量名。`comparison` 填 `extractor` ID 与另一套 `judge`，只重新评分已保存预测，缺失预测另存覆盖清单，不重新抽取。每个组合各有独立日志、manifest、summary 和退出码；某模型启动失败不作为正常抽取质量结果。

评测驱动可在 `qasystem_backend` 内执行，模型与 Clef 使用同一 Docker 网络的服务名。本轮本地服务统一采用 `qasystem-env-v1`，模型目录只读挂载，记录真实上下文容量及输出预算；测试结果不纳入运行时源码包。

**精确匹配打分**

```bash
./eval/run.sh --match-mode identity
```

不用 LLM-judge，实体按 type+name、关系按五元组做集合精确匹配。identity 只在显式配置为该模式时使用；LLM 模式缺少评分地址会退出 1，不会自动切换口径。

---

**质量门禁（发布前防回归）**

```bash
./eval/run.sh --match-mode llm --require-scored --fail-on-empty --min-f1 0.5 \
  --max-case-seconds 1800 --max-stage-seconds parse=300,chunk=120,extract=900
```

发版前跑全量正式 LLM 评测，检查以下门禁；任一不过即非零退出：

- `--fail-on-empty`（默认开启，`--no-fail-on-empty` 关闭）：已评分 case 的抽取预测为 0 节点（golden 非空）时，该 case 记为失败，`error=empty_extraction`，退出码 2。extract 接口本身失败的 case 不受影响（本来就已失败）；切片为空按整篇降级继续跑的行为也不变（那是降级不是失败）。
- `--min-f1 0.5`：已评分 case 的实体 F1（后台系统、后台抽取、插件三者取最低）低于阈值则门禁未过，退出码 4。
- `--min-relation-f1 <阈值>`：各抽取模块的主关系 F1 低于阈值则退出 4。默认 `0` 表示暂不启用，待新基线确定阈值后加入发布命令；golden 与预测均无关系的维度不参与，golden 无关系但预测出关系仍检查。
- `--require-scored` / `--no-require-scored`：要求每个所选 case 有标注、每个应评模块都有有效评分。未显式指定时，LLM 模式、任一 F1 阈值大于 0 或指定 `--baseline` 都会启用。探索性无标注预测可以显式关闭，但不能据此证明完整发布评测通过。
- LLM 模式中裁判失败、降级或产生无效指标始终挡住质量门禁；`--no-require-scored` 不会放行 `identity_fallback`。诊断与精确对照仍保存，正式 LLM 汇总只纳入有效 LLM 评分。
- `--max-case-seconds 1800` / `--max-stage-seconds parse=300,chunk=120,extract=900`：单 case 墙钟 / 分阶段耗时预算（用 summary 里的 `elapsed_seconds` 与 `backend.modules.<stage>.elapsed_seconds` 判定，逗号分隔、容忍空格，未列出的阶段不检查）。超预算**不改** case 状态（接口成败是既成事实），记入 `gate.budget_violations`，退出码 4。
- `--baseline <运行目录或summary.json>`：执行下文的同口径逐案比较，数据、评分口径或结果不可比时同样不能通过。

门禁结果落在 `summary.json` 的 `gate` 块：

```json
"gate": {
  "empty_failures": ["official/official_03"],
  "budget_violations": [{"case": "standard/standard_01", "stage": "extract", "elapsed": 912.5, "budget": 900}],
  "min_f1": {"threshold": 0.5, "below": [{"case": "official/official_05", "f1": 0.42}]},
  "quality": {
    "issues": [],
    "min_relation_f1": {"threshold": 0.0, "below": []},
    "passed": true
  },
  "passed": false
}
```

以上仅为结构示例。`gate.passed` 汇总 F1、耗时、评分有效性和基线比较的质量判定；`empty_failures` 是执行失败清单，空抽取仍优先走退出码 2。全部未评分时，若启用 `require-scored` 则门禁失败；未启用且无其他违规时 `passed=null` 表示未判定，不是质量合格。

**同口径基线比较**

```bash
# 与同样使用 chunk-extract、同一全量数据和 judge 配置的新基线比较
./eval/run.sh --match-mode llm --require-scored --min-f1 0.5 \
  --baseline tmp/eval/runs/<有效新基线> \
  --max-f1-drop 0.02 --max-latency-increase 0.2 \
  --max-case-seconds 1800 --max-stage-seconds parse=300,chunk=120,extract=900
```

`--baseline` 接受运行目录或 `summary.json` 的路径。比较前校验 case 集合、输入内容、golden、本体、复用输入、评测代码哈希、阶段/模式、匹配口径与 judge 配置。抽取模型和选项记录在 manifest，允许作为实验变量变化；它们不替代服务端回报的算法版本。

- `--max-f1-drop` 默认 `0.02`，表示每个 case、每个应评模块的实体与关系 F1 最多下降 **2 个百分点**。
- `--max-latency-increase` 默认 `0.2`，表示逐 case 的 API 阶段耗时最多相对增加 **20%**；比较不包含 judge 评分耗时。两边都复用的 parse/chunk 不比较耗时；复用预测必须保留已知原抽取耗时，不能用补评耗时替代。
- 基线文件不存在或不是有效 summary 对象时退出 1；缺少 manifest、旧评分代码、数据/口径不一致、失败批次或评分/耗时记录缺失都会使基线门禁未过，退出 4（有执行失败时优先退出 2）。
- 首次建立新基线不传 `--baseline`，先完成正式全量门禁，再选取合格批次。旧批次必须使用当前评分器重新评价；有可验证输入来源的可重评分，缺少 manifest/本体来源证据的须按相同冻结输入重新抽取。不能直接拿旧分数宣布提升或退化。
- 比较抽取版本时，两边都跑 `extract --from-run <同一冻结批次>`；不要把生成切片的 `chunk-extract` 批次直接作为 `extract` 基线。

---

退出码：`0` 所执行检查未失败（无评分探索批次不等于质量已验证），`1` 配置/发现失败，`2` 有 case 失败（含重试后跳过、空抽取判失败），`4` 无执行失败但质量门禁未过（实体/关系 F1、耗时、裁判失效、缺评分或基线问题）。优先级 `1 > 2 > 4 > 0`。旧退出码 `3`（min-f1 专用）已废弃，并入 `4`。

### 看结果

每次运行写入 `tmp/eval/runs/<时间戳>/`，不会覆盖 `extract/golden.json`。目录已 gitignore。

```text
tmp/eval/runs/<YYYYMMDD-HHMMSS>/
  params.yaml                 # 本次拷贝的评测参数
  params.resolved.json        # 解析后的地址 / 阶段 / 模式 / 评分口径
  manifest.json               # 输入与评测代码 SHA256、评分契约、选项；不包含密钥
  summary.json                # 整次运行总览：状态、P/R/F1、耗时、extract_versions（服务端回报的抽取算法版本）
  report.md                   # 本次评测详细报告：运行信息 / 门禁与退出码 / 总览 / 逐 case / 问题清单 / 最慢 case Top5
  <类型>/<case>/
    parse_<文件名>.json       # 后台解析接口回包
    document.md               # 拼好的 Markdown（--from-run 的 chunk-extract 模式复用它跳过解析）
    chunks.json               # 切片回包
    backend_extract.json      # 后台抽取回包（预测图在 data 里）
    scores.json               # 该 case 状态、分数、各步耗时
    match_details.json        # 逐条匹配明细（对上/漏抽/多抽清单，judge 分批耗时）
    backend_system_<文件>.json  # 仅 --system：沉淀流水线回包
    plugin_extract.json       # 仅插件阶段：MCP 抽图结果
    plugin_error.json         # 仅插件失败时
```

| 文件 | 看什么 |
| --- | --- |
| `report.md` | 批次详细报告（每批结束自动生成，含 resume/复用与中断批次）：头部的运行信息（起止时间、backend、评分口径、judge 降级摘要、抽取版本）、门禁与退出码语义（empty_failures、F1/耗时阈值与实际值）、总览表（实体/关系的 golden/predicted/P/R/F1）、逐 case 明细（状态、P/R/F1、parse/chunk/extract 各阶段耗时、总耗时、抽取版本、失败原因）、问题清单（失败/空抽取/judge 降级/超预算）、最慢 case Top5 |
| `summary.json` | 各 case 成败和 P/R/F1/Jaccard；`started_at` / `finished_at` / `elapsed_seconds` 是整次运行耗时，每个 case 完成后更新。`overall` 是有效同口径 micro，`quality` 包含覆盖率、case/group 均值与分组结果；`overall_identity` 单列精确对照。`gate` 记录空抽取、实体 F1、耗时和 `quality` 评分有效性/关系阈值检查；`baseline_comparison` 是同口径基线判定与逐项差异 |
| `manifest.json` | 源文件、golden、本体，以及实际复用 Markdown/切片的 SHA256；`evaluator` 记录 `run.py`、`quality.py`、`provenance.py` 的内容哈希（包含未提交修改）；`settings` 与 `scoring_contract` 记录安全配置和评分契约，不记录 API key / Authorization。同一内容也嵌入 `summary.manifest` |
| `scores.json` | 单案明细。`attempts` 是本 case 实际尝试次数；重试耗尽为 `skipped_after_retries: true`。`elapsed_seconds` 是该案墙钟时间；`backend.modules.parse/chunk/extract.elapsed_seconds` 是各接口耗时；抽取/沉淀/插件另有 `score_elapsed_seconds`（对照 golden 打分，LLM-judge 时会明显变长）。`empty_extraction` 标记已评分但抽取预测为 0 节点（配合 `--fail-on-empty` 时该案 `status=failed`、`error=empty_extraction`）。多附件 case 的 parse / 沉淀有 `per_file` 数组：逐文件状态、耗时、尝试次数、产出规模（解析字数 / 图谱节点关系数），失败带 `error` |
| `match_details.json` | 每个已评分块的 `entities` / `relations` 命中、漏抽、多抽明细。LLM-judge 另有 `aligned_relations` 诊断、`judge_batches`（窗口原索引、耗时、响应格式与失败尝试；聚焦补判的批次带 `focused: true`）、`judge_coverage` 和 `identity` 精确对照 |
| `document.md` | 解析结果，便于对照原文；`--from-run` 且 `--mode chunk-extract` 时复用它跳过解析（同批重试也会复用），因此仍需保留 |
| `chunks.json` | 实际送进抽取的切片 |
| `backend_extract.json` | 生产接口原样回包，排抽取失败或图结构时用 |

终端默认只打后台抽取：P/R/F1/Jaccard 与 标准/抽出/对上/多抽/漏抽（实体、关系分行）。未跑的插件、沉淀不出现在总览里；加 `--system` 或 `--stage plugin|all` 才打印对应块。case 日志同样按步输出耗时和抽取计数。

`quality.modules.<模块>` 同时提供 `coverage`、`micro`、`case_macro`、`by_group` 和 `group_macro`。micro 先累加计数；case_macro 对有效 case 等权；group_macro 先按组计算 case_macro 再对有信息的组等权。某维度 golden 与 predicted 都为空时不参加该维度宏平均，以 `empty_cases` 记录；汇总没有可评条目时为 `null`。覆盖率中的 `failed`、`unscored` 可以与其他状态重叠，不应相加当成样本总数。失败 case 若已有有效评分（如空抽取）保留其实际分数；缺失评分不伪造为 0，必须结合覆盖率和门禁解释，不能把条件均值当成全链路成功率。

### 历史批次清理

实际评测通过配置与输入检查后，清理 `tmp/eval/runs/` 下过期的历史批次；`--dry-run` 不清理历史：

- 保留期默认 **30 天**，按批次目录名的时间戳（`<YYYYMMDD-HHMMSS>`）判定；命名不符合该格式的目录不动。
- 配置走 `params.yaml` 的 `runs_retention_days`，命令行 `--runs-retention-days <天数>` 优先；`<=0` 表示不清理。
- 被 `--from-run`、`--reuse-extract-from` 或 `--baseline` 引用的批次即使过期也跳过。
- 清理结束打印摘要：删除了几个批次、释放多少空间；单个目录删除失败只记日志，不影响评测。

### 加 case

在对应类型下新建 `<case>/docs/` 和 `<case>/extract/golden.json`。`golden.json` 用 `entities` / `relations`，或生产接口同款 `nodes` / `relationships`。确认真空图可写 `"score_empty": true`。

## 使用注意

- 默认 LLM-judge 会多次调模型；可先用 `--match-mode identity` 检查链路，正式质量门禁仍使用 LLM 模式。
- `rule/rule_01` 有 40 个实体、0 条关系，会评分（不是空 golden）。
- 公文 `official_*` 多数含多附件：单个文件解析失败会立刻重试，最多 3 次，仍失败则跳过该文件并记 `failed_files`，其余文件继续；逐文件耗时、尝试次数与产出在 `per_file`。`--mode pipeline` 时沉淀同样按文件重试后跳过。
- 扫类型目录时，单个 case 的切片/抽取/打分抛错或失败同样最多重试 3 次，然后跳过进入下一个 case，不会把整批停掉。无 docs、本体缺失这类配置错误不重试。
- parse / chunk 只验接口成功和输出非空，没有独立 golden；质量分只打在图抽取上。
- 插件没有 parse/chunk 工具，抽取的是整篇 Markdown，和后台「先切片再抽取」路径不完全相同。
- `中车RAG验证数据集/` 是造集源，不是评测入口。拷目录做评测时可以不带它。

### 已下线版本的对照说明

`v3.2`（单通用提示词联合抽取）与旧 `v3.1`（V2 副本 + prompt router）实现已从代码库移除，对应的提示词对照评测无法再运行；历史结论保留在 [V3.2 提示词与评测](../docs/算法/知识抽取V3.2通用提示词与评测.md)。当前 `v3.1` 指语义 harness，评测方式见 [V3.1 语义 Harness 与评测](../docs/算法/知识抽取V3.1语义Harness与评测.md)。
# 小规模诊断评测

`python3 eval/quick_eval.py --extract-version v3` 使用 `eval/quick_params.yaml`，固定选择
`official_01`、`official_03`、`official_04` 三个完整用例，覆盖普通公文、短公文和密集表格/截断压力。
默认从 `test_data/eval` 读取已有 `chunks.json`；不会重新解析，也不会复用旧抽取结果。
可在 `quick_eval.cases` 中更换 1–3 个用例，须保持各轮输入、golden、模型和预算相同。

```bash
# 88 上复用之前已冻结的输入；需要 Backend 已恢复 siliconflow 供应商配置。
python3 eval/quick_eval.py \
  --workspace /mnt/data3/qasys/qasystem/tmp/eval/comparisons/20261011T032213Z/workspace \
  --extract-version v3

# 同集测试另一版本，裁判仍独立配置为 DeepSeek 官方。
python3 eval/quick_eval.py --extract-version v2
# 只核验用例和固定切片，不调用 API。
python3 eval/quick_eval.py --dry-run
```

凭据从现有 judge 环境变量读取，不写入配置。默认 LLM 评分；临时 `--match-mode identity`
可去掉裁判调用，但不能将 identity 分数与 LLM 分数直接比较。配置项及 `--extract-provider`
可切换已登记的供应商；请求输入/输出 token 预算仍来自 Backend 供应商配置。

- 外层 `--case-attempts 1`，partial 不再整篇重抽；算法内部拆批和必要重试保留。
- 默认不限制整轮、文档耗时；不覆盖供应商的网络请求超时。靠缩小评测集控制工作量，不承诺固定时间结束。
- `poll_timeout_seconds: 0` 表示一直等待 Backend 完成，兼容 inline 与排队模式。
- 可显式指定 `--wall-seconds`，但诊断评测不使用该硬时限。
- 每轮独立保存 `tmp/eval/quick/<UTC时间-随机编号>/run.log` 与 `status.json`；
  `run.log` 中指向统一入口生成的 `tmp/eval/runs/<批次>/summary.json` 和报告。
- 快速集用于局部回归比较，不能替代全量门禁。比较不同版本时必须保持相同的模型、超时配置和输入；不能直接对比之前被硬预算截断的结果。

统一入口也支持直接传 `python3 eval/run.py ... --case-attempts 1`，默认仍为 3 次，
该参数只影响外层整篇尝试，不改变传输重试。
