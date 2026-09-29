# 知识沉淀评测

改完解析、切片、抽取或沉淀流水线后，在仓库根目录对指定 case 跑一次脚本。默认只测后台 HTTP，不 `import algorithm` / `backend`。

默认连 `http://127.0.0.1:8006`。机器上 Backend 已在跑时，拷走本目录即可评测。

抽取算法版本由 Backend 环境变量 **KG_EXTRACT_ALGO_VERSION** 控制（`latest` 默认 / `v2` / `v3`）：默认 `latest` 使用 `algorithm/knowledge_extract/latest/` 这一独立代码包（当前与 v3 一致，但未来会独立演进）。后台 `/api/v1/graphs/extract`、沉淀流水线和插件 MCP 工具 `kg_extract_v2` 都走同一个开关。本脚本不直接 import 算法包，测的是线上这条链路；做版本对比评测时显式设 `KG_EXTRACT_ALGO_VERSION=v2` 或 `v3`，再跑同一套评测即可对比。

## 做了什么

**评测集。** 按文档类型放 case，每个 case 有原文件和标准答案：

```text
eval/
  run.sh / run.py / params.yaml

test_data/eval/
  评测数据
  <类型>/
    ontology/                 # 该类型共用本体
    <case>/
      docs/                   # 原文件（pdf / docx / xlsx / png …）
      extract/golden.json     # 抽取标准答案
      ontology/               # 可选；有则覆盖类型级本体
```

当前正式集：公文 1 个、标准 6 个、规则 2 个（其中 `故障记录test(1)` 的 golden 为空，只跑通不评分）。`中车RAG验证数据集/` 是造 golden 的源材料，评测扫描会跳过。

**评测模式。** 后台评测归一化成三类（`--mode`，可组合）：

| 模式 | 含义 | 调用 | 输入 |
| --- | --- | --- | --- |
| `extract` | 仅知识抽取 | `POST /api/v1/graphs/extract` | `--from-run` 复用的切片（chunks.json） |
| `chunk-extract`（默认） | 切片+抽取 | `POST /api/v1/documents/chunk` + extract | docs/ 现解析，或 `--from-run` 复用 document.md |
| `pipeline` | 全流程知识沉淀 | `POST /api/v1/pipelines/knowledge-ingestion` | docs/ 原文件 |

`extract` 模式只重跑抽取这一步，切片固定不变——这是对比抽取算法版本（v2/v3）的推荐方式：先跑一次 `chunk-extract` 冻结切片，切换 Backend 的 `KG_EXTRACT_ALGO_VERSION` 后再 `--mode extract --from-run <上次运行>`，两次喂入完全相同。插件走 MCP `kg_extract_v2`，同样受版本开关控制。

**模块通过标准。** 解析 / 切片只验接口成功且输出非空；抽取对照 `golden.json` 打 P / R / F1 / Acc。复用的中间产物在 scores.json 里记 `status: reused` 与来源路径。

插件侧没有独立的 parse / chunk 工具：插件阶段复用后台解析出的 Markdown；若只跑 `--stage plugin`，会先调一次解析接口再调 MCP。

**打分。** 默认 LLM-judge（语义一对一匹配，允许同义、别名、格式差），summary 里附 identity（type+name 精确集合）对照。judge 失败则该 case 降级 identity。需要纯精确匹配时加 `--match-mode identity`。空 golden 仍会跑并落盘预测，但不计入分数。

预测只写 `tmp/eval/runs/<时间戳>/`，不会覆盖 `test_data/eval/<类型>/<case>/extract/golden.json`。

## 如何使用

### 准备

1. Backend 已启动（默认 `http://127.0.0.1:8006`，MCP 默认 `{backend}/mcp`）。
2. 默认 LLM-judge 需要 `KG_EXTRACT_LLM_BASE_URL`（可选 `KG_EXTRACT_LLM_MODEL` / `KG_EXTRACT_LLM_API_KEY`）。未配 base_url 时自动改 identity，不会假装用大模型评过。显式 `--match-mode llm` 且未配地址则退出。
3. 需要鉴权时设环境变量 `AUTHORIZATION`。

地址也可用 `QASYSTEM_BACKEND_URL` / `QASYSTEM_MCP_URL`，或改 `params.yaml`。流水线选项在 `params.yaml` 的 `options`，与生产 `PipelineOptions` 对齐。不要把密钥写进 YAML。

### 命令

先给脚本执行权限（只需一次）：

```bash
chmod +x eval/run.sh
```

`run.sh` 会把工作区指到本目录，再转调 `run.py`。后面所有参数都原样传给 `run.py`。

---

**按路径评测（推荐）**

```bash
./eval/run.sh test_data/eval/公文/关于开展OpenClaw开源智能体安全风险排查工作的通知
```

在仓库根目录也可以：

```bash
python3 eval/run.py test_data/eval/test_data/eval/公文/关于开展OpenClaw开源智能体安全风险排查工作的通知
```

脚本从路径推出类型和 case。默认 `--stage backend` + `--mode chunk-extract`：**解析 / 切片 / 抽取并对抽取结果评分**，不跑沉淀、不调 MCP。传类型目录则跑该类型全部 case：

```bash
./eval/run.sh test_data/eval/规则
```

---

**对比抽取算法版本（v2/v3）**

默认 `KG_EXTRACT_ALGO_VERSION=latest` 跑的是 `latest` 包（当前与 v3 一致）。要对比 v2/v3，先显式固定 Backend 环境：

```bash
# 1. 固定 Backend 环境为 v2，先冻结切片
KG_EXTRACT_ALGO_VERSION=v2 ./eval/run.sh test_data/eval/标准 --mode chunk-extract
#    记下运行目录 tmp/eval/runs/<时间戳>

# 2. 改 Backend 环境为 v3，然后只重跑抽取（喂入完全相同的切片）：
KG_EXTRACT_ALGO_VERSION=v3 ./eval/run.sh test_data/eval/标准 --mode extract --from-run <时间戳>
```

两次切片完全相同，分数差只来自算法版本。每次运行的 `summary.json` 会记录服务端回报的 `extract_versions`。

---

**全量评测（只测后台）**

```bash
./test_data/eval/run.sh
```

扫公文 / 标准 / 规则下全部 case，默认只跑后台。抽取对照 golden：配了 judge 走 LLM-judge，否则 identity。

---

**后台 + 插件**

```bash
./eval/run.sh test_data/eval/--stage all
```

每个 case 先跑后台，再跑插件 `kg_extract_v2`。MCP 会话在多 worker 下可能失败，插件回归时再加此项。

---

**全流程知识沉淀**

```bash
./eval/run.sh test_data/eval/公文/某case --mode pipeline
```

打 `POST /api/v1/pipelines/knowledge-ingestion`（内部走完整 解析→切片→抽取）并对结果评分。也可与模块模式组合：`--mode chunk-extract pipeline`；旧参数 `--system` 等价于追加 pipeline。

---

**只测插件**

```bash
./eval/run.sh test_data/eval/--stage plugin
```

只调 MCP `kg_extract_v2`。插件没有 parse/chunk，脚本会先用后台解析接口把 `docs/` 转成 Markdown，再交给插件。适合只改了 MCP 工具封装、抽取算法本身已在后台测过时用。

---

**只扫描评测集（不调服务）**

```bash
./eval/run.sh test_data/eval/--dry-run
```

列出将要跑的类型、case、原文件数、golden 规模和本体路径。拷到部署机后先跑这一步，确认目录被识别，且不需要 Backend / judge。

---

**按类型筛选**

```bash
./eval/run.sh test_data/eval/公文
./eval/run.sh test_data/eval/--groups 公文,标准
```

路径与 `--groups/--cases` 不要混用。`--groups` 仍支持逗号分隔多类型。

---

**按 case 筛选**

```bash
./eval/run.sh test_data/eval/规则/Simple_故障规则的诊断
./eval/run.sh test_data/eval/--groups 规则 --cases Simple_故障规则的诊断
```

---

**精确匹配打分**

```bash
./eval/run.sh test_data/eval/--match-mode identity
```

不用 LLM-judge，实体按 type+name、关系按五元组做集合精确匹配。未配 `KG_EXTRACT_LLM_BASE_URL` 时即使 `params.yaml` 写了 `llm` 也会自动改 identity；显式 `--match-mode llm` 才会因缺地址退出。

---

**质量门禁**

```bash
./eval/run.sh test_data/eval/--min-f1 0.5
```

在接口都成功的前提下，检查已评分 case 的实体 F1（后台系统、后台抽取、插件三者取最低）。低于 `0.5` 则退出码 3。适合 CI 或发版前卡住明显回退；阈值按集子自己定。

---

退出码：`0` 全部成功，`1` 配置/发现失败，`2` 有 case 失败，`3` 质量门禁未过。

### 看结果

每次运行写入 `_runs/<时间戳>/`，不会覆盖 `extract/golden.json`。目录已 gitignore。

```text
_runs/<YYYYMMDD-HHMMSS>/
  params.yaml                 # 本次拷贝的评测参数
  params.resolved.json        # 解析后的地址 / 阶段 / 模式 / 评分口径
  summary.json                # 整次运行总览：状态、P/R/F1、耗时、extract_versions（服务端回报的抽取算法版本）
  <类型>/<case>/
    parse_<文件名>.json       # 后台解析接口回包
    document.md               # 拼好的 Markdown
    chunks.json               # 切片回包
    backend_extract.json      # 后台抽取回包（预测图在 data 里）
    scores.json               # 该 case 状态、分数、各步耗时
    backend_system_<文件>.json  # 仅 --system：沉淀流水线回包
    plugin_extract.json       # 仅插件阶段：MCP 抽图结果
    plugin_error.json         # 仅插件失败时
```

| 文件 | 看什么 |
| --- | --- |
| `summary.json` | 各 case 成败、后台抽取（以及可选沉淀/插件）的 P/R/F1/Acc；`started_at` / `finished_at` / `elapsed_seconds` 是整次运行耗时 |
| `scores.json` | 单案明细。`elapsed_seconds` 是该案墙钟时间；`backend.modules.parse/chunk/extract.elapsed_seconds` 是各接口耗时；抽取/沉淀/插件另有 `score_elapsed_seconds`（对照 golden 打分，LLM-judge 时会明显变长） |
| `document.md` | 解析结果，便于对照原文 |
| `chunks.json` | 实际送进抽取的切片 |
| `backend_extract.json` | 生产接口原样回包，排抽取失败或图结构时用 |

终端默认只打后台抽取：P/R/F1/Acc 与 标准/抽出/对上/多抽/漏抽（实体、关系分行）。未跑的插件、沉淀不出现在总览里；加 `--system` 或 `--stage plugin|all` 才打印对应块。case 日志同样按步输出耗时和抽取计数。

### 加 case

在对应类型下新建 `<case>/docs/` 和 `<case>/extract/golden.json`。`golden.json` 用 `entities` / `relations`，或生产接口同款 `nodes` / `relationships`。确认真空图可写 `"score_empty": true`。

## 使用注意

- 标准类 golden 很大（数百实体/关系）。默认 LLM-judge 会多次调模型，耗时长；想先看字面匹配用 `--match-mode identity`。
- `规则/故障记录test(1)` 未标注，只跑通不评分。
- 公文 case 含多附件：单个文件解析失败不再拖死整案，会记 `failed_files`，其余文件继续。`--mode pipeline` 时沉淀同样按文件继续。
- parse / chunk 只验接口成功和输出非空，没有独立 golden；质量分只打在图抽取上。
- 插件没有 parse/chunk 工具，抽取的是整篇 Markdown，和后台「先切片再抽取」路径不完全相同。
- `中车RAG验证数据集/` 是造集源，不是评测入口。拷目录做评测时可以不带它。
