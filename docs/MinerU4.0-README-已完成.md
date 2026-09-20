# MinerU 4.0 —— 已完成改动（单环境合并版）

> 待办见 `docs/MinerU4.0-README-待办.md`；完整评估见 `docs/MinerU4.0评估与部署说明.md`。
> 更新于 2026-09-20。全部本地推理，未调用 MinerU 远程 API。

## 0. 结论

1. **MinerU 4.0.4 已并入主环境**（与 docling 2.129.0 同一个 Python 环境，不再需要独立 venv）；
2. **MinerU 2.x 已退役**：`engine=mineru` / `vlm` 由 4.0 适配层承接，2.x 相关依赖与配置移除；
3. **多格式链路已开**：DOC/DOCX/PPT/PPTX/XLS/XLSX/CSV/OFD 等可走 4.0（自动降 `flash` 档），原有
   `pandas`/`docling`/`text`/`beautifulsoup` 链路与默认选择不变，OFD 转换失败时自动回退 4.0 原生解析；
4. **GPU 提速已就绪**：vLLM 用的 1.2B 权重已下载并落到交付目录，配置开关与文档已准备（本机无 GPU，待 A40 验证）；
5. **解析步骤的输入输出未改**：`process_file` 入参/返回、错误码、markdown 中表格的 JSON 形态、
   页眉/页脚补回规则、元数据键名全部保持；`auto` 选路不变。

## 1. 代码改动

### 1.1 解析层能力（前置，已提交 `5a1537e`）

| 文件 | 作用 |
|---|---|
| `algorithm/preproc/preproc_src/page_edges.py` | 页眉/页脚保留（文本层取上下边缘带，只补引擎丢掉的行；默认首页） |
| `algorithm/preproc/preproc_src/text_layer.py` | Docling 文本层对账（合并被拆开的标题、逐行校正、收拢编号空格） |
| `algorithm/preproc/preproc_src/table_json.py` | 表格统一转 JSON：`{"columns": [...], "rows": [{...}]}`，兼容 HTML 合并单元格与分组表头 |
| `algorithm/preproc/preproc_src/processors/{docx,ocr,excel}_processor.py` | Docling/MinerU/Excel 三条路线接入上述能力 |
| `backend/app/schemas.py` | `keep_page_edges`、`page_edge_min_chars` |

### 1.2 MinerU 4.0 适配与 2.x 退役（工作区，未提交）

| 文件 | 状态 | 改动 |
|---|---|---|
| `processors/mineru4_processor.py` | 新增 | 4.x 适配：进程内/子进程双模式；`tier` 档位；非 PDF 自动降 `flash`；`page_range` 仅对 PDF；页边补回仅对 PDF；元数据 `mineru4_options` |
| `processors/mineru4_worker.py` | 新增 | 子进程模式的 worker（保留能力：主环境为 2.x 时可用 `MINERU4_PYTHON` 走独立解释器） |
| `main_processor.py` | 改 | ①`ENGINE_MATRIX` 为 `.xlsx/.xls/.csv` 增加 `mineru4`（默认仍是 `pandas`）② 装了 4.x 时 `engine=mineru`/`vlm` 复用 4.x 适配层（元数据仍如实回报请求的引擎名）③ OFD 转 PDF 失败时回退 4.x 原生 OFD 解析 ④ **mineru4 不再强制走 LibreOffice 转 PDF**（4.0 原生支持 Office/图片） |
| `backend/app/schemas.py` | 改 | `ParserConfig.engine` 增加 `"mineru4"`（原有取值不变） |
| `backend/tests/test_mineru4_processor.py` | 新增 | 36 例：契约、参数换算、双模式、非 PDF flash 档、JSON 表格、页边补回、2.x 退役映射、OFD 回退、引擎注册 |

### 1.3 依赖与镜像（工作区，未提交）

| 文件 | 改动 |
|---|---|
| `backend/requirements.txt` | `docling 2.76.0 → 2.129.0`、`sentence-transformers 5.1.2 → 6.1.0`、新增 `transformers==5.17.0`、`numpy==2.0.2 → 2.2.6`、**移除 `mineru==2.7.6`** |
| `backend/requirements.mineru-extras.txt` | 改为 `mineru[torch]==4.0.4`；移除 ultralytics 与 2.x 相关说明；标注"不要装 `mineru[full]`"（它要 vllm>=0.19.1，与镜像的 vllm 0.11.2 冲突） |
| `Dockerfile` | 保持单环境：去掉独立 venv 层与 `python3-venv`；最终校验断言 `mineru` 为 4.x、`transformers` 为 5.x、`from mineru import parse` 可用 |
| `.env.example` | 移除 2.x 变量（`MINERU_CONFIG_PATH`/`MINERU_MODEL_PIPELINE_PATH`/`MINERU_MODEL_VLM_PATH`）；保留并注释 4.0 变量与 GPU 选项（`MINERU_MODEL_VLM_ENGINE`、`MINERU4_TIER`、`MINERU4_LOCK_DIR`） |
| `docker-compose.yml` | `/models` 仍只读，单独把 `mineru4/.locks` 叠成可写（4.0 固定在该目录加锁） |

## 2. 模型（已落到交付目录 `/mnt/data3/qasys/models`）

| 路径 | 体积 | 用途 |
|---|---|---|
| `mineru4/MinerU-4_models_onnx` | 819MB | basic 档小模型（Layout/OCR/公式/表格） |
| `mineru4/MinerU2.5-Pro-2605-1.2B-GGUF` | 1.2GB | CPU VLM（llama.cpp，standard/advanced 档） |
| `mineru4/MinerU2.5-Pro-2605-1.2B` | 2.2GB | **GPU VLM（vLLM 提速用，新增）** |
| `mineru4/.locks` | 空目录 | 4.0 固定写入的锁目录（需可写） |
| `docling-models/RapidOcr/{PP-OCRv6_det_small,PP-OCRv6_rec_small,ch_ppocr_mobile_v2.0_cls_mobile}.onnx` | 31MB | **docling 2.129 新增必需模型（新增）** |

部署栈 `/mnt/data3/qasys/.env` 已追加 4.0 变量（备份 `.env.bak-mineru4`）。仓库 `docker-compose.yml`
的 `.locks` 可写挂载已加；部署栈的 `/models` 本身可写，无需改动。

## 3. 验证：合并环境验收（单解释器：docling 2.129 + MinerU 4.0.4 + transformers 5.17）

| 用例 | 结果 |
|---|---|
| PDF 第 1 页 → docling | `success=True`，228 字符，含 `ICS`/标准号 |
| PDF 附录 14–16 → docling | `success=True`，11900 字符，**10 张表全转 JSON**，`<table>`=0 |
| PDF 第 1 页 → mineru4（进程内） | `success=True`，222 字符，含 `ICS`；`mode=in-process` |
| PDF 附录 14–16 → mineru4 | `success=True`，11109 字符，**9 张表全转 JSON** |
| `engine=mineru`（2.x 已退役） | `success=True`，222 字符，`engine=mineru`、`mode=in-process`（由 4.0 承接） |
| `engine=vlm` | 同上，`engine=vlm` |
| CSV → mineru4 | `success=True`，213 字符，1 张 JSON 表，`tier=flash` |
| XLSX → mineru4 | `success=True`，4434 字符，1 张 JSON 表，`tier=flash` |
| DOCX → mineru4 | `success=True`，3581 字符，**2 张 JSON 表**（未走 LibreOffice，4.0 原生解析） |
| CSV → pandas（默认） | 与原行为一致（每 sheet 一节 + JSON 块） |
| XLSX → pandas（默认） | 与原行为一致 |
| DOCX → docling（默认） | 与原行为一致，2 张 JSON 表 |

其他已验证项：`keep_page_edges=off` 时适配层输出与 4.0 原生 markdown（经表格转 JSON）**逐字节一致**；
同进程连续多次解析稳定；本地性（`MINERU_MODEL_SOURCE=local` + 断网）通过；`advanced` 档本地可用（CPU 上约 48–60s/页）。

## 4. 测试

* `backend/tests` 全量 **234 passed**；8 个失败为既有问题（`test_error_contract`×2、`test_execution_runtime`×2、
  `test_extractor_retry`×2、`test_knowledge_protocol`×2，均为 LLM/向量库/MCP 桩相关），已用基线对比确认与本次改动无关；
* 注意 `backend/tests/conftest.py` 有 session 级桩会替换 `main_processor`，涉及引擎注册的测试按文件路径加载真实模块。

## 5. 自查命令

```bash
# 1) 单环境里两个引擎都在
python -c "from importlib.metadata import version as v; print('mineru', v('mineru'), '| docling', v('docling'), '| transformers', v('transformers'))"

# 2) 引擎名映射与多格式（本地）
MINERU_HOME=$PWD/work/mineru4/home MINERU_MODEL_BASE_DIR=$PWD/work/mineru4/home/models \
MINERU_MODEL_SOURCE=local MINERU_MODEL_SMALL_BACKEND=onnx MINERU4_TIER=basic \
DOCLING_MODELS_PATH=$PWD/work/mergecheck/docling-models python3 -c "
import sys; sys.path.insert(0,'.')
from algorithm.preproc.preproc_src.main_processor import DocumentProcessor
p = DocumentProcessor(output_dir='/tmp/v')
for eng in ('docling','mineru','vlm','mineru4'):
    r = p.process_file('test_artifacts/parser/解析测试数据/GBT+48000.4-2026 标准数字化 第4部分：协同制定要求.pdf',
                       model_name=eng, force=True, start_page_id=0, end_page_id=0)
    print(eng, r['success'], r['metadata']['model_used'], (r['metadata'].get('mineru4_options') or {}).get('mode'))"

# 3) 单测
python3 -m pytest backend/tests/test_mineru4_processor.py backend/tests/test_table_json.py \
  backend/tests/test_page_edges.py backend/tests/test_text_layer_reconcile.py \
  backend/tests/test_docling_page_edges.py -q
```
