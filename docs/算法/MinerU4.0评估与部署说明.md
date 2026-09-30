# MinerU 4.0 评估与部署说明

评估对象：MinerU **4.0.4**（2026-09 发布）对比当前在用的 **2.7.6**。全部为**本地推理，未调用任何远程 API**（见第 4 节本地性验证）。
评估样本：`test_data/parse/解析测试数据/GBT+48000.4-2026 标准数字化 第4部分：协同制定要求.pdf`。
复现方式见文末。

## 1. 结论速览

| 维度 | 2.7.6（旧） | 4.0.4（新） |
|---|---|---|
| 内部 API | `mineru.backend.pipeline.*` / `vlm_analyze` / `cli.common` | **完全重写**，改为 `mineru.parse()` / `MinerUParser` / `ParseResult` |
| 扩展 extra | `mineru[pipeline,vlm,vllm]` | `mineru[torch]` / `[full]` / `[all]` |
| 模型仓库 | PDF-Extract-Kit + MinerU2.5 VLM | `MinerU-4_models_onnx`(819MB) / `_torch` / `MinerU2.5-Pro-2605-1.2B`(+GGUF) |
| 配置方式 | `mineru.json`（`models-dir`、`config_version`）+ `MINERU_MODEL_*_PATH` | `MINERU_HOME`(默认 `~/.mineru`) + `MINERU_CONFIG` + `MINERU_MODEL_SOURCE/SMALL_BACKEND/VLM_ENGINE` |
| 解析档位 | pipeline / vlm 两种后端 | `flash` / `basic` / `standard` / `advanced` |
| 页眉页脚 | 丢弃（`discarded_blocks` 只在 content_list） | Markdown 里仍丢弃，但 **`structured_content()` 保留 `header`/`footer` 类型 + bbox 定位** |
| 渲染目标 | markdown / content_list | markdown / html / latex / docx / epub / **structured_content** / content_list / content_list_v2 / pdf |

**同一份 PDF 的实测（CPU，`basic` 档 + ONNX 小模型）：**

| 样本 | 指标 | 2.7.6 | 4.0.4 |
|---|---|---|---|
| 附录 14–16 页 | 耗时 | 1446.5s | **10.9s（约 133×）** |
| | `表 A.x` 交叉引用 | 0 | **9** |
| | 表格 | 7 张（HTML，经仓库后处理转 JSON） | 7 张（HTML，同样可转 JSON） |
| 第 1 页（封面） | 耗时 | 117.7s | **5.7s（约 21×）** |
| | 发布/实施日期 | 丢失 | **保留** |
| 第 7 页（正文） | 耗时 | 168.9s | **7.8s（约 22×）** |
| | `4.1 概述` | 丢失（输出 `41`） | **保留** |
| | 二级条款号个数 | 1 | **13** |
| | 顿号 `、` / 句号 `。` | 0 / 3 | **9 / 8** |

即：4.0 在本标准 PDF 上把 2.7.6 丢掉的**条款号、交叉引用与中文标点**基本都保住了，速度快 1–2 个数量级；表格仍是 HTML，仓库现有 `table_json.py` 转换规则对 4.0 输出**直接可用**（实测 7/7 转换成功，生成 `分阶段/提案登记` 这类单层列名）。

## 2. 对现有代码的影响

`algorithm/preproc/preproc_src/processors/ocr_processor.py` 依赖 8 个 2.x 内部模块，4.0 中**全部不存在**：

```
mineru/backend/pipeline/pipeline_analyze.py              缺失
mineru/backend/pipeline/model_json_to_middle_json.py      缺失
mineru/backend/pipeline/pipeline_middle_json_mkcontent.py 缺失
mineru/backend/vlm/vlm_analyze.py                         缺失
mineru/cli/common.py                                      缺失
mineru/utils/config_reader.py                             缺失
mineru/utils/enum_class.py（MakeMode）                    缺失
mineru/data/data_reader_writer.py                         缺失
```

因此**直接把镜像里的 mineru 升到 4.0 会让 `mineru`/`vlm` 两条路线全部 ImportError**，必须同时改适配层（或用新引擎名并行一段时间）。

## 3. 依赖冲突（升级前必须处理）

| 包 | 4.0.4 要求 | 仓库当前钉住 | 现状 |
|---|---|---|---|
| transformers | `>=5.10.1,<6` | `docling-ibm-models==3.11.0`、`sentence-transformers==5.1.2` 要 `<5.0` | **冲突**；两者最新版（`docling-ibm-models 4.0.3`、`sentence-transformers 6.1.0`）都已放宽到 `<6.0`，需一并升级 |
| pypdfium2 | `>=5.10.1,<6` | `pdftext==0.6.3`（2.x mineru 依赖）钉 `==4.30.0` | 只要不再装 2.x mineru 即可消除 |
| 其它新增 | `docvortex`、`mineru-llama-cpp`、`onnxruntime`、`gradio 6`、`fastapi/uvicorn/typer/aiosqlite/watchfiles` | — | 镜像体积与启动面变大，需要评估 |

结论：4.0 与 2.x **不能装在同一个环境**（pypdfium2 互斥），一次切换是单向的；若要灰度，只能像本次评估一样**用独立 venv/服务**并行。

## 4. 本地部署（已完成，无任何 API 调用）

本次评估**全部使用本地推理**，未调用 MinerU 远程 API。部署位置都在仓库内，系统 Python 的 2.7.6 未被改动：

| 项目 | 位置 |
|---|---|
| 4.0.4 运行环境 | `work/mineru4/venv`（`python3 -m venv --system-site-packages`，复用系统 torch 2.10） |
| 模型根目录 | `work/mineru4/home/models`（由 `MINERU_HOME` 指定，**2.0GB**） |
| 小模型（basic） | `MinerU-4_models_onnx` **819MB**（Layout/PP-DocLayoutV2、OCR、公式、表格） |
| VLM（standard） | `MinerU2.5-Pro-2605-1.2B-GGUF` **约 1.2GB**（llama.cpp，CPU 可跑） |
| 缓存/凭据 | `work/mineru4/{ms-cache,ms-credentials,hf-home}`（默认写 `~/.cache`、`~/.modelscope`，受限环境必须重定向） |

**本地性验证（可复现）**：`server_url` 为空、未配置 api_key，且把模型源钉成 `local`、关闭在线校验、代理指向黑洞端口后仍能解析成功——说明没有任何 HTTP 访问：

```bash
env MINERU_HOME=$PWD/work/mineru4/home MINERU_MODEL_SOURCE=local \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 \
    work/mineru4/venv/bin/python -c "
import mineru
from mineru.config import config
print('server_url:', repr(config.model.vlm.server_url), '| source:', config.model.source)
print(len(mineru.parse('…/GBT+48000.4-2026 ….pdf', tier='basic', page_range='7').markdown()), 'chars')"
```

对比测试脚本已把这一点固化：默认 `block_network=True`（黑洞代理）、`MINERU_MODEL_SOURCE=local`，并在报告里打印
`新版运行模式: source=local backend=onnx vlm=… server_url='' 本地优先=True 断网=True`；
一旦检测到远程服务配置会直接告警提示结果不可信。需要联网时须显式加 `--allow-network`。

**档位实测（同一份 PDF，CPU，本地离线）：**

| 档位 | 小模型 | VLM | 第 7 页耗时 | 第 1 页耗时 | 第 7 页字符 | `4.1 概述` |
|---|---|---|---|---|---|---|
| `basic` | ONNX | 不用 | 5.4s | 5.7s | 845 | ✅ |
| `standard`（4.0 默认） | ONNX | llama-cpp 1.2B | 11.2s | 2.1s | 845 | ✅ |
| 旧版 2.7.6 pipeline | torch | 不用 | 97.5s | 117.7s | 738 | ❌（输出 `41`） |

`standard` 档页 7 与 `basic` 输出一致（该页没有需要 VLM 的内容），耗时约为其 2 倍；封面页反而更快（2.1s），说明 VLM 是按需触发的。

## 5. 模型与部署路径

### 5.1 已落地的目录

宿主（`HOST_MODELS_DIR=/mnt/data3/qasys/models`，部署栈里 compose 把它挂到 `/models`）：

```
/mnt/data3/qasys/models/                 ← HOST_MODELS_DIR
├── mineru/                              2.x 旧模型（pipeline/vlm，仍被 2.7.6 使用）
├── docling-models/                      docling
└── mineru4/                             2.0GB  ← 4.0 模型（本次新增）
    ├── MinerU-4_models_onnx/            819MB  basic 档小模型
    ├── MinerU2.5-Pro-2605-1.2B-GGUF/    1.2GB  standard 档 VLM（llama.cpp）
    └── .locks/                          可写（777，空目录）
```

容器内对应 `/models/mineru4/{MinerU-4_models_onnx,MinerU2.5-Pro-2605-1.2B-GGUF,.locks}`。

### 5.2 必须注意：4.0 要在模型根目录写 `.locks`

`mineru/model/download.py` 的 `ModelRepo.lock_path()` 固定返回 `<base_dir>/.locks/<repo>.lock`，
下载、校验与解析都会 `FileLock` 它。**因此模型目录不能整体只读**：

* 做法：模型本体只读，**只把 `.locks` 这一层叠成可写**（已实测：只读仓库 + 空的可写 `.locks`，basic/standard 两档都能解析）；
* `.locks` 里不要保留旧的锁文件（只读的旧锁会直接 `PermissionError`），保持空目录即可；
* 4.0 的锁目录没有配置项可改，只能靠挂载层解决。

### 5.3 已写入的配置

* 部署栈 `/mnt/data3/qasys/.env`（已备份为 `.env.bak-mineru4`）追加：

  ```ini
  MINERU_HOME=/app/backend/data/mineru4      # 可写：锁/配置等运行时文件
  MINERU_MODEL_BASE_DIR=/models/mineru4      # 模型本体（只读挂载即可）
  MINERU_MODEL_SOURCE=local                  # 只用本地模型，不下载、不联网
  MINERU_MODEL_SMALL_BACKEND=onnx
  MINERU_MODEL_VLM_ENGINE=llama-cpp
  ```

  部署栈的 compose 里 `/models` 本来就是可写挂载（`- ${HOST_MODELS_DIR}:/models`，无 `:ro`），
  所以这侧不需要再动挂载。

* 仓库 `docker-compose.yml`（交付形态，`/models:ro`）新增一层可写挂载，模型本体仍只读：

  ```yaml
  - ${HOST_MODELS_DIR:-./deploy/models}:/models:ro
  - ${MINERU4_LOCK_DIR:-./deploy/models/mineru4/.locks}:/models/mineru4/.locks
  ```

  `MINERU4_LOCK_DIR`、`MINERU_HOME`、`MINERU_MODEL_BASE_DIR` 等已加入根 `.env.example`。

### 5.4 其他

* 下载：`mineru-models-download --tier basic|standard --small-backend onnx|torch --vlm-engine llama-cpp|vllm`。
* ModelScope/HuggingFace 缓存与凭据默认写到 `~/.cache/modelscope`、`~/.modelscope/credentials`，受限环境下需用
  `MODELSCOPE_CACHE`、`MODELSCOPE_CREDENTIALS_PATH`、`HF_HOME` 指到可写目录。
* 旧版 compose 里的 `MINERU_MODEL_PIPELINE_PATH` / `MINERU_MODEL_VLM_PATH` 与 `algorithm/preproc/mineru.json` 均为 2.x 语义，**4.0 不再读取**，需整段替换（当前仍保留给 2.7.6）。
* 当前这些变量**还没有任何代码读取**：适配层尚未迁移，重建容器不会有行为变化，因此本次没有重启服务。

### 5.5 校验命令

```bash
# 直接用暂存好的部署路径解析（只读模型 + 写 .locks），容器内把路径换成 /models/mineru4
env MINERU_HOME=/tmp/m4home MINERU_MODEL_BASE_DIR=/mnt/data3/qasys/models/mineru4 \
    MINERU_MODEL_SOURCE=local MINERU_MODEL_SMALL_BACKEND=onnx \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    work/mineru4/venv/bin/python -c "
import mineru
pdf='test_data/parse/解析测试数据/GBT+48000.4-2026 标准数字化 第4部分：协同制定要求.pdf'
for tier in ('basic','standard'):
    print(tier, len(mineru.parse(pdf, tier=tier, page_range='1').markdown()), 'chars')"
# 实测：basic 5.1s / standard 10.7s，各 162 字符
```

## 6. 适配层（engine=mineru4，已就绪）

新增引擎 `mineru4`，与既有引擎**入参、返回、输出格式完全一致**，属于纯增量改动：

| 位置 | 改动 |
|---|---|
| `algorithm/preproc/preproc_src/processors/mineru4_processor.py` | **新增**：调 4.x 公开 API，抹平版本差异 |
| `main_processor.py` | `ENGINE_MATRIX` 增加 `mineru4`；`_processor()` 分支；Office/图片转 PDF 分支覆盖它；参数与元数据透传 |
| `backend/app/schemas.py` | `ParserConfig.engine` 增加 `"mineru4"`（原有取值不变） |
| `docs/文档解析行为规范.md` | 引擎表、允许引擎表、`keep_page_edges` 适用范围补 `mineru4` |
| `tests/backend/test_mineru4_processor.py` | **新增** 19 例：契约、参数换算、JSON 表格、页边补回、版本不符报错、引擎注册 |

契约保持不变（这些都没有动）：

* `process_file(...)` 的参数与返回结构、错误码；失败仍写 `metadata["error"]`；
* markdown 里**表格仍是 ```json 代码块**（复用 `table_json.py`，同一形态）；
* `keep_page_edges` / `page_edge_min_chars` **同一套规则**（复用 `page_edges.py`：文本层取行、只补缺失行、顶/底分置）；
* 元数据键沿用 `processor` / `backend` / `keep_page_edges` / `page_edges` / `tables_json`；
* `auto` 选路不变，`mineru`（2.x）行为不变。

版本差异的抹平方式：

* `page_range`：0 基闭区间 → 4.0 的 1 基闭区间字符串（`(0,2)` → `"1-3"`，未限定 → `""`）；
* `ocr_mode`：`auto/on/off` → `auto/ocr/txt`，语义一一对应；
* `tier`：默认 `standard`（4.0 自身默认），可用环境变量 `MINERU4_TIER` 覆盖；
* 4.0 没有 `lang` / `table_enable` / `formula_enable`：**接受但忽略**，并如实记录在
  `metadata.mineru4_options.ignored_options`，避免调用方误以为生效；
* 装的是 2.x 时返回明确错误（"engine=mineru4 需要 MinerU 4.x，当前安装的是 2.7.6…"），
  而不是难懂的 ImportError。

**验证结果（venv 内真实 4.0.4，本地离线）：**

| 用例 | 结果 |
|---|---|
| 第 1 页 | `success=True`，222 字符；补回 `ICS01.120`/`CCS A00`（顶部）与两行发布机构（底部，4.0 默认 markdown 里没有）；日期已在正文 |
| 第 7 页 | `success=True`，863 字符；`4.1 概述` ✅、`GB/T1.1` ✅ |
| 附录 14–16 页 | `success=True`；**9 张表全部转为 JSON 代码块**，`<table>` 残留 0 |
| 同进程连续 4 次解析 | 全部成功（1.7–5.5s），无进程池异常 |
| **格式零漂移** | `keep_page_edges=off` 时适配层输出与 4.0 原生 markdown（经表格转 JSON）**逐字节一致**（各 845 字符） |

注意：当前容器镜像里仍是 MinerU 2.7.6，因此线上 `engine=mineru4` 会返回上面那条明确错误
（行为安全、不影响现有链路）。要真正启用需要重建镜像（见第 7 节依赖改造）。

## 7. advanced 档与镜像重建改动（已准备）

### 7.1 advanced 档已在本地部署并实测

4.0 的 `tier` 与内部 effort 的对应关系是 `flash/basic/standard/advanced` → `flash/medium/high/xhigh`；
`advanced` 走 **hybrid VLM 管线（xhigh）**，**模型与 standard 完全相同**（下载档位只有 basic/standard，
advanced 复用已下载的 ONNX 小模型 + 1.2B GGUF VLM），因此不需要额外下载模型，只要设置：

```ini
MINERU4_TIER=advanced     # 需要回落时改回 standard
```

实测（CPU + ONNX + llama.cpp，同一份 PDF）：

| 档位 | 第 1 页 | 第 7 页 | 第 7 页字符 | `4.1 概述` |
|---|---|---|---|---|
| basic | 5.7s | 5.4s | 845 | ✅ |
| standard | 2.1s | 11.2s | 845 | ✅ |
| **advanced** | 48.4s | 60.1s | 842 | ✅ |

结论：advanced 在 CPU 上约慢 5 倍，对这份"原生数字、文本层完好"的标准 PDF 没有质量增益
（输出与 standard 基本一致）；建议默认 `standard`，疑难版面（扫描件、复杂表格）再按需切 advanced。

### 7.2 镜像重建改动

> **⚠️ 本节早先描述的「独立 venv 并存」方案已被取代。**最终采用**单环境合并**：
> `docling==2.129.0` + `docling-ibm-models 4.0.3` + `sentence-transformers==6.1.0` +
> `transformers==5.17.0` + `numpy==2.2.6` + `mineru[torch]==4.0.4` 装在同一环境，
> MinerU 2.x 退役（`engine=mineru`/`vlm` 由 4.x 适配层承接）。
> 最终改动清单与验收结果见 `docs/MinerU4.0-README-已完成.md`；
> 适配层保留的「子进程模式」仅作为**仍是 2.x 的旧环境**的兼容回退，镜像不再自带 venv。
> 下面保留该方案的过程记录，供回溯。

**为什么不直接把主环境的 mineru 升到 4.0**：4.0 要 `transformers>=5.10.1`，而主环境
`docling-ibm-models==3.11.0` / `sentence-transformers==5.1.2` 要 `<5.0`；实测把 docling 升到兼容版
（2.129.0）后，它的 OCR 模型布局也变了（新版要 `RapidOcr/PP-OCRv6_*`，现有 `docling-models` 是
PP-OCRv4），**必须重新下载 docling 模型**并回归整条 docling 路线；另外 4.0 与 2.x 的 `pypdfium2`
互斥，切换是单向的、影响面最大。

所以镜像一度采用**并存**方案（主环境一行不动）；该方案**最终被单环境合并取代**，下表为过程记录：

| 文件 | 当时改动 | 最终状态 |
|---|---|---|
| `backend/requirements.txt` | 不改，仍 `docling==2.76.0` / `mineru==2.7.6` | **已改**：`docling==2.129.0` / `sentence-transformers==6.1.0` / `transformers==5.17.0` / `numpy==2.2.6`，移除 `mineru==2.7.6` |
| `backend/requirements.mineru4-venv.txt` | 新增 venv 依赖 | **已删除**（改为单环境，见 `backend/requirements.mineru-extras.txt` 的 `mineru[torch]==4.0.4`） |
| `Dockerfile` | 加 `python3-venv` + 建 `/opt/mineru4/venv` | **已回到单环境**：不建 venv；最终校验断言 `mineru` 4.x、`transformers` 5.x、`from mineru import parse` 可用 |
| `.env.example` | 4.0 变量（`MINERU_HOME`/`MINERU_MODEL_BASE_DIR`/`MINERU4_TIER`/`MINERU4_LOCK_DIR` 等），并移除 2.x 变量 | 相同（另去掉 venv 专用的 `MINERU4_PYTHON`/`MINERU4_TIMEOUT_SECONDS`） |
| `processors/mineru4_worker.py` | **新增**：在 venv 解释器里跑一次解析，产出 `markdown.md` / `structured.json` / `result.json` | 保留（作为仍是 2.x 的旧环境的子进程回退能力） |
| `processors/mineru4_processor.py` | 增加**子进程模式**：主环境有 4.x 就进程内调用，否则用 `MINERU4_PYTHON` 调 worker；对外契约不变，元数据记录 `mineru4_options.mode` |

镜像内布局：

```text
python3                        → mineru 2.7.6 + docling 2.76.0（现有链路不变）
/opt/mineru4/venv/bin/python   → mineru 4.0.4 + transformers 5.17（仅 engine=mineru4 使用）
/models/mineru4/               → 4.0 模型（宿主已暂存 2.0GB）+ 可写 .locks
```

**已按镜像布局验证**（系统 python 里是 2.7.6，用 `MINERU4_PYTHON` 指向 4.0 venv）：

| 用例 | 结果 |
|---|---|
| 封面 | `success=True`，222 字符，`mode=subprocess`、`version=4.0.4`，含 `ICS01.120`/`CCS A00` |
| 正文第 7 页 | 863 字符，`4.1 概述` ✅ |
| 附录 14–16 页 | 11109 字符，**9 张表全部转 JSON**，`<table>` 残留 0 |

### 7.3 重建后的验收

```bash
python -c "from importlib.metadata import version; assert version('mineru').startswith('2.')"
/opt/mineru4/venv/bin/python -c "from importlib.metadata import version; assert version('mineru').startswith('4.')"
/opt/mineru4/venv/bin/python /app/algorithm/preproc/preproc_src/processors/mineru4_worker.py \
    --pdf "<测试 PDF>" --out-dir /tmp/w4 --tier standard --page-range 1
python -c "import sys; sys.path.insert(0,'/app'); from algorithm.preproc.preproc_src.main_processor import DocumentProcessor as D; print(D(output_dir='/tmp/v').process_file('<测试 PDF>', model_name='mineru4', force=True, start_page_id=0, end_page_id=0)['metadata']['mineru4_options'])"
```

### 7.4 备选方案（以后要退役 2.x 时）

把主环境的 `mineru` 直接升到 4.0 可行，但要一并完成：`docling==2.129.0`（+ `docling-slim==2.129.0`、
`docling-ibm-models==4.0.3`、`docling-core==2.97.1`、`docling-parse==7.20.0`）、
`sentence-transformers==6.1.0`、`transformers==5.17.0`、`numpy<2.3`，并**重新下载 docling 模型**
（新版要 `RapidOCR/PP-OCRv6_*`）、回归 docling 路线、确认 `pip check` 干净。

## 8. 迁移步骤与当前状态

| 步骤 | 状态 |
|---|---|
| 1. 本地部署 4.0（独立 venv）+ 下载 basic/standard 模型（2.0GB） | ✅ 已完成（第 4 节） |
| 2. 新旧对比测试（`test_mineru_version_compare.py`）+ 实测三档 | ✅ 已完成（第 1、7.1 节） |
| 3. 4.0 模型暂存到交付目录 `/mnt/data3/qasys/models/mineru4`（含可写 `.locks`）+ 部署 `.env` | ✅ 已完成（第 5 节） |
| 4. 适配层 `engine=mineru4`（进程内 + 子进程双模式），契约不变 | ✅ 已完成（第 6 节） |
| 5. 镜像重建改动（venv 层 + 校验 + `.env.example`） | ✅ 已准备好（第 7.2 节），**尚未真正重建镜像** |
| 6. 重建 `qasystem-env-v1` 并重建 Backend 容器 | ⏳ 待执行（需要构建机执行 `build-image.ps1`） |
| 7. 线上灰度：解析接口分别用 `engine=mineru4`（4.0）与 `engine=mineru`（2.7.6）做 A/B | ⏳ 待执行 |

回归口径：同一批样本上确认**条款号、交叉引用、中文标点、表格转 JSON** 四项指标不回退
（对比脚本见第 9 节）。

注意：适配层虽然已就绪，但**当前运行的容器与镜像仍是 MinerU 2.7.6**：
`engine=mineru4` 在既没有 4.x、也没有可用的子进程解释器（`MINERU4_PYTHON`）时会返回明确错误，
不会影响既有链路。

## 9. 复现本次评估

```bash
# 1) 独立 venv + 4.0（不污染系统 python 的 2.7.6）
python3 -m venv --system-site-packages work/mineru4/venv
work/mineru4/venv/bin/pip install "mineru[torch]==4.0.4"

# 2) 下载 basic 档模型（缓存/凭据指到工作区）
export MINERU_HOME=$PWD/work/mineru4/home
export MODELSCOPE_CACHE=$PWD/work/mineru4/ms-cache
export MODELSCOPE_CREDENTIALS_PATH=$PWD/work/mineru4/ms-credentials
export HF_HOME=$PWD/work/mineru4/hf-home
work/mineru4/venv/bin/mineru-models-download --tier basic --small-backend onnx -s modelscope

# 3) 新旧对比（旧版走系统 python + 仓库 preproc，新版走 venv）
python3 tests/algorithm/preproc/test_mineru_version_compare.py --pages 7 --tier basic
python3 tests/algorithm/preproc/test_mineru_version_compare.py --pages 14-16 --tier basic
```

报告与产物：`work/mineru4/compare/report_*.json`、`work/mineru4/compare/{old,new}_*.md`。

## 10. 尚未覆盖

* `standard` 档已下载并本地跑通（见第 4 节），但只抽测了第 1、7 页，未做全量对比；`advanced` 档未测；
* 未跑 19 页全文对比（旧版全文约 33 分钟，新版预计 1–2 分钟）；
* 4.0 的 DOCX/PPTX/XLSX/EPUB/OFD 等多格式入口未评估，若要用它替代现有 `docling`/`pandas` 路线需单独对比。
