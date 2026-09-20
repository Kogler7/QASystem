# MinerU 4.0 —— 待办事项（单环境合并版）

> 已完成改动见 `docs/MinerU4.0-README-已完成.md`；完整评估见 `docs/MinerU4.0评估与部署说明.md`。
> 更新于 2026-09-20。标注「需要谁做」：**构建机** = 有 Docker/GPU 的那台；**我** = 本会话可继续；**你们** = 需决策。

## A. 必须做（上线路径）

### A1. 重建环境镜像 `qasystem-env-v1`　【构建机】

* **为什么**：4.0 与 docling 2.129 依赖 `transformers>=5.10.1`，必须重建镜像才能单环境生效；同时 2.x 退役也要落到镜像。
* **仓库里已改好**：
  * `backend/requirements.txt`：`docling==2.129.0`、`sentence-transformers==6.1.0`、`transformers==5.17.0`、
    `numpy==2.2.6`，并**移除** `mineru==2.7.6`；
  * `backend/requirements.mineru-extras.txt`：`mineru[torch]==4.0.4`（**不要**改成 `mineru[full]`：它要 vllm>=0.19.1，与镜像自带 vllm 0.11.2 冲突）；
  * `Dockerfile`：单环境安装 + 最终断言（mineru 4.x / transformers 5.x / `from mineru import parse`）。
* **怎么做**：构建机执行 `build-image.ps1`。
* **验收标准**：
  1. `pip check` 干净（构建脚本会跑）；
  2. `python -c "from importlib.metadata import version as v; assert v('mineru').startswith('4.'); assert v('transformers').startswith('5.'); assert v('docling').startswith('2.129')"`；
  3. `python -c "import docling, mineru, pandas, scipy"` 均可在同一解释器导入。
* **风险与注意**：
  * docling 2.129 是第一版引入 `docling-slim` 拆分的版本，若构建期解析出不同子版本，需按 `pip check` 结果微调；
  * 我在本地用 `--system-site-packages` venv 模拟时遇到过 pandas/numexpr/bottleneck 的 numpy ABI 报错——
    那是**本地 venv 混用系统包**造成的，干净镜像里 pandas/numpy/scipy 一起解析安装不会有该问题；
  * 建议构建后跑一次 `backend/tests` 的解析相关用例（见 A3）。

### A2. 同步模型（已由我完成，构建机只需核对）　【构建机】

* `/mnt/data3/qasys/models/mineru4/`（4.2GB）：`MinerU-4_models_onnx` + `MinerU2.5-Pro-2605-1.2B-GGUF`（CPU VLM）
  + `MinerU2.5-Pro-2605-1.2B`（GPU vLLM 权重）+ 可写 `.locks`；
* `/mnt/data3/qasys/models/docling-models/RapidOcr/` 增加了 docling 2.129 必需的
  `PP-OCRv6_det_small.onnx`、`PP-OCRv6_rec_small.onnx`、`ch_ppocr_mobile_v2.0_cls_mobile.onnx`；
* 2.x 的 `/models/mineru/{pipeline,vlm}` 在 4.0 下不再被读取，确认无其它引用后可以下线（省 2.x 模型占用）。

### A3. 重建 Backend 容器并冒烟　【构建机 + 我】

```bash
# 容器内逐项冒烟（顺序与本地验收一致）
python - <<'PY'
import sys; sys.path.insert(0,'/app')
from algorithm.preproc.preproc_src.main_processor import DocumentProcessor
p = DocumentProcessor(output_dir='/tmp/verify')
pdf = '<挂载的测试 PDF>'
for eng in ('docling', 'mineru', 'vlm', 'mineru4'):
    r = p.process_file(pdf, model_name=eng, force=True, start_page_id=0, end_page_id=0)
    print(eng, r['success'], r['metadata']['model_used'], r['metadata'].get('mineru4_options'), 'ICS' in r['content'])
for path in ('<csv>', '<xlsx>', '<docx>'):
    r = p.process_file(path, model_name='mineru4', force=True)
    print(path, r['success'], (r['metadata'].get('tables_json') or {}).get('count'))
PY
```

* **验收**：8 项全部 `success=True`；`engine=mineru`/`vlm` 的 `mineru4_options.mode` 为 `in-process`；
  非 PDF 的 `tier` 为 `flash`；首页含 `ICS01.120`/`CCS A00`；`server_url` 为空（未走远程）。

### A4. 灰度回归　【我 / 你们】

* 用 `test_artifacts/parser/test_mineru_version_compare.py`（保留 2.x 对照时）或直接同批样本对比新旧 md；
* **回归口径**：条款号完整（`4.1`/`4.2.1`/`6.10`）、交叉引用（`表A.x`/`第N章`）、中文标点（`、。`）、
  表格转 JSON（数量 + `json.loads` 通过率）；
* 建议：默认 `auto` 不变；扫描件/疑难版面显式 `engine=mineru4`；确认稳定后再考虑调整默认。

## B. GPU 提速　【你们决策 + A40 验证】

* **已就绪**：vLLM 权重已下载（`/models/mineru4/MinerU2.5-Pro-2605-1.2B`）；镜像自带 vLLM 0.11.2 满足
  4.0 的 `vllm>=0.10.1` 要求（`compute_capability` 检查只对 <8.0 报警，A40 是 8.6）。
* **两种启用方式**（选一，均需在 A40 上验证）：
  1. **进程内 vLLM**：`MINERU_MODEL_VLM_ENGINE=vllm`，4.0 直接在本进程加载权重；
  2. **外部服务**：用 `mineru-openai-server`（4.0 自带）或现有 vLLM 池起服务，再设
     `MINERU_MODEL_VLM_SERVER_URL=<url>` 让 4.0 走 HTTP。
* **待办**：
  1. 在 A40 上确认显存占用与并发（现有 GPU 队列参数：`EXECUTION_GPU_MEMORY_REQUEST_MB_GPU_VLM` 等）；
  2. 若走外部服务，需要在 supervisor/compose 里为新 VLM 加一个服务项，并确认与现有 vLLM 池不冲突；
  3. 量化 GPU 下 basic/standard/advanced 的耗时与质量，决定线上默认档位（CPU 上 advanced 约 48–60s/页）。
* **本机无法验证 GPU**（无 NVIDIA 驱动），上述仅为配置与文档准备。

## C. 建议做（质量与维护）

| 编号 | 事项 | 说明 |
|---|---|---|
| C1 | 分块保护 JSON 表格 | `algorithm/knowledge_pipeline/chunking.py` 无代码围栏保护，长文档会按 `\n` 切开大表；建议让 `json` 块不可切分（超长按记录边界切） |
| C2 | 抽取侧识别 JSON 表格 | `algorithm/knowledge_extract_v2/candidate_selector.py:28` 的 `_TABLE_ROW` 只认 Markdown 管道表格行，建议扩展到 ```json 块 |
| C3 | `md_to_text` 保留围栏内容 | `algorithm/knowledge_pipeline/presentation.py:10` 会删掉所有围栏块（含 JSON 表格）；当前沉淀链路未调用，但风险仍在 |
| C4 | 页边保留改用 4.0 `structured_content()` | 4.0 已把页眉/页脚标为 `header`/`footer` 并带 bbox，扫描件同样可用；建议做成开关（默认仍走文本层）以免改变现有输出 |
| C5 | 用 4.0 统一更多格式入口 | 目前 HTML/EPUB/TSV 等仍走原链路；可按同一对比方法评估是否收敛到 4.0 |
| C6 | 收尾整理 | 未提交改动需入库；`work/` 下的 4.2GB 模型副本与评估脚手架按需清理（交付侧已独立存放模型） |

## D. 明确不做

* 不改 `process_file` 的入参/返回、错误码与元数据键名；
* 不改 `auto` 选路；`docling`/`pandas`/`beautifulsoup`/`text` 的默认与行为不变；
* 不把 4.0 的页眉/页脚行为直接替换现有页边规则（除非 C4 以开关方式采纳）；
* 不安装 `mineru[full]`（与镜像 vLLM 版本冲突）；不引入第二个 Python 环境（已合并为单环境）。
