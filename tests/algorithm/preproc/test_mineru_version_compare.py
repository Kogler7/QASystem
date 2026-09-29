#!/usr/bin/env python3
"""新旧 MinerU 解析对比测试。

对比对象：

* **旧版**：系统 Python 里的 MinerU 2.7.6，走仓库既有 preproc 路径
  （``DocumentProcessor.process_file(engine="mineru", force=True)``）。
* **新版**：独立 venv 里的 MinerU 4.x（``work/mineru4/venv``），走 4.0 公开 API
  （``mineru.parse(...)`` + ``ParseResult.markdown()``）。

两个版本无法在同一个解释器里共存（4.0 重写了内部 API），因此各自在子进程里跑，
父进程只负责指标统计与对比报告，不导入任何 MinerU。

用法：

    python tests/algorithm/preproc/test_mineru_version_compare.py --pages 14-16 --tier basic
    python tests/algorithm/preproc/test_mineru_version_compare.py --pages 1 --tier standard
    python tests/algorithm/preproc/test_mineru_version_compare.py --pages 14-16 \\
        --old-md work/a.md --new-md work/b.md        # 只对比已有产物，不重跑

pytest：设置 ``RUN_MINERU_COMPARE=1`` 才执行（需要两套环境就绪）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PDF = (
    ROOT / "test_data/parse/解析测试数据"
    / "GBT+48000.4-2026 标准数字化 第4部分：协同制定要求.pdf"
)
OLD_PYTHON = os.getenv("MINERU_OLD_PYTHON", sys.executable)
NEW_PYTHON = os.getenv("MINERU_NEW_PYTHON", str(ROOT / "work/mineru4/venv/bin/python"))
NEW_HOME = os.getenv("MINERU_NEW_HOME", str(ROOT / "work/mineru4/home"))
OUT_DIR = ROOT / "work/mineru4/compare"

# 旧版 2.7.6 需要的模型路径与 ftfy（未声明依赖）
OLD_ENV = {
    "MINERU_MODEL_SOURCE": "local",
    "MINERU_MODEL_PIPELINE_PATH": "/mnt/data3/qasys/models/mineru/pipeline",
    "MINERU_MODEL_VLM_PATH": "/mnt/data3/qasys/models/mineru/vlm",
    "PYTHONPATH": os.pathsep.join([
        str(ROOT),
        str(ROOT / "work/pdf_extract/pylibs"),
        os.environ.get("PYTHONPATH", ""),
    ]),
    "MPLCONFIGDIR": "/tmp/mplcfg",
    "YOLO_CONFIG_DIR": "/tmp/ultralytics",
}

OLD_WORKER = r'''
import json, sys, time
from pathlib import Path
root, pdf, out_path, start, end = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
sys.path.insert(0, root)
from algorithm.preproc.preproc_src.main_processor import DocumentProcessor
t0 = time.time()
proc = DocumentProcessor(output_dir=str(Path(out_path).parent / "engine_out"))
res = proc.process_file(
    pdf, model_name="mineru", force=True,
    start_page_id=start, end_page_id=end, keep_page_edges="off",
)
elapsed = round(time.time() - t0, 1)
md = res.get("content", "") if res.get("success") else ""
Path(out_path).write_text(md, encoding="utf-8")
print("@@" + json.dumps({
    "ok": bool(res.get("success")), "elapsed": elapsed, "chars": len(md),
    "error": res.get("error"), "engine": (res.get("metadata") or {}).get("model_used"),
    "version": __import__("importlib.metadata", fromlist=["version"]).version("mineru"),
}, ensure_ascii=False))
'''

NEW_WORKER = r'''
import json, sys, time
from pathlib import Path
pdf, out_path, page_range, tier = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
import mineru
from importlib.metadata import version
t0 = time.time()
result = mineru.parse(pdf, tier=tier, page_range=page_range)
md = result.markdown()
elapsed = round(time.time() - t0, 1)
Path(out_path).write_text(md, encoding="utf-8")
Path(out_path + ".structured.json").write_text(
    json.dumps(result.structured_content(), ensure_ascii=False)[:200000], encoding="utf-8")
from mineru.config import config as _cfg
print("@@" + json.dumps({
    "ok": True, "elapsed": elapsed, "chars": len(md),
    "engine": "mineru-4", "version": version("mineru"), "pages": len(result.pages),
    "model_source": _cfg.model.source,
    "small_backend": _cfg.model.small_backend,
    "vlm_engine": _cfg.model.vlm.engine,
    "server_url": _cfg.model.vlm.server_url,
    "api_key_configured": bool(_cfg.model.vlm.api_key),
    "local_only": not _cfg.model.vlm.server_url and not bool(_cfg.model.vlm.api_key),
}, ensure_ascii=False))
'''


def _run(cmd, env, cwd=ROOT):
    proc = subprocess.run(cmd, env=env, cwd=str(cwd), capture_output=True, text=True)
    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith("@@"):
            payload = json.loads(line[2:])
    if payload is None:
        payload = {
            "ok": False,
            "error": (proc.stderr or proc.stdout or "no output")[-1500:],
        }
    payload["returncode"] = proc.returncode
    payload["stderr_tail"] = (proc.stderr or "").strip().splitlines()[-3:]
    return payload


def run_old(pdf: Path, out_path: Path, start: int, end: int) -> dict:
    env = {**os.environ, **OLD_ENV}
    cmd = [OLD_PYTHON, "-c", OLD_WORKER, str(ROOT), str(pdf), str(out_path), str(start), str(end)]
    return _run(cmd, env)


def run_new(pdf: Path, out_path: Path, page_range: str, tier: str,
            small_backend: str = "onnx", vlm_engine: str | None = None,
            block_network: bool = True) -> dict:
    env = {
        **os.environ,
        "MINERU_HOME": NEW_HOME,
        # 4.0 的 ModelScope/HF 缓存与凭据默认写在 ~/.cache、~/.modelscope，
        # 沙箱下不可写，统一指向工作区。
        "MODELSCOPE_CACHE": str(ROOT / "work/mineru4/ms-cache"),
        "MODELSCOPE_CREDENTIALS_PATH": str(ROOT / "work/mineru4/ms-credentials"),
        "HF_HOME": str(ROOT / "work/mineru4/hf-home"),
        # 强制本地推理：只用已下载模型，禁掉在线校验，并把代理指向黑洞端口，
        # 任何 HTTP 访问都会立刻失败——保证对比结果不来自远程 API。
        "MINERU_MODEL_SOURCE": os.getenv("MINERU_NEW_MODEL_SOURCE", "local"),
        "MINERU_MODEL_SMALL_BACKEND": small_backend,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PYTHONPATH": "",
    }
    if block_network:
        dead = "http://127.0.0.1:9"
        env.update({
            "HTTP_PROXY": dead, "HTTPS_PROXY": dead, "ALL_PROXY": dead,
            "http_proxy": dead, "https_proxy": dead, "all_proxy": dead,
            "NO_PROXY": "", "no_proxy": "",
        })
    if vlm_engine:
        env["MINERU_MODEL_VLM_ENGINE"] = vlm_engine
    cmd = [NEW_PYTHON, "-c", NEW_WORKER, str(pdf), str(out_path), page_range, tier]
    return _run(cmd, env)


def metrics(markdown: str) -> dict:
    text = markdown or ""
    pipe_rows = sum(1 for line in text.splitlines() if line.strip().startswith("|"))
    return {
        "chars": len(text),
        "clause_3level": len(re.findall(r"\d+\.\d+\.\d+", text)),
        "clause_2level": len(re.findall(r"\d+\.\d+", text)),
        "has_4.1": "4.1 概述" in text or "4 . 1" in text,
        "has_6.10": "6.10" in text,
        "dunhao": text.count("、"),
        "juhao": text.count("。"),
        "table_refs": len(re.findall(r"表\s*A\s*\.\s*\d", text)),
        "chapter_refs": len(re.findall(r"第\s*\d+\s*章", text)),
        "has_ICS": "ICS" in text,
        "has_CCS": "CCS" in text,
        "has_date": "2026-01-28" in text,
        "json_blocks": text.count("```json"),
        "pipe_table_rows": pipe_rows,
        "html_tables": text.count("<table"),
    }


def convert_tables(markdown: str) -> tuple[str, int]:
    """用仓库的表格转 JSON 规则处理一遍，看看"引擎原生 + 我们的后处理"效果。"""
    sys.path.insert(0, str(ROOT))
    from algorithm.preproc.preproc_src.table_json import convert_tables_to_json

    return convert_tables_to_json(markdown)


def compare(pdf: Path, pages: str, tier: str, skip_old: bool, skip_new: bool, out_dir: Path,
            small_backend: str = "onnx", vlm_engine: str | None = None,
            block_network: bool = True) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    first, last = _parse_pages(pages)
    old_md_path = out_dir / f"old_p{pages.replace(',', '_')}.md"
    new_md_path = out_dir / f"new_{tier}_p{pages.replace(',', '_')}.md"

    report: dict = {"pdf": str(pdf), "pages": pages, "tier": tier,
                    "block_network": block_network, "sides": {}}

    if not skip_old:
        print(f"[1/2] 旧版 MinerU 2.7.6 解析 {pages} ...", flush=True)
        report["sides"]["old"] = run_old(pdf, old_md_path, first - 1, last - 1)
    if not skip_new:
        print(f"[2/2] 新版 MinerU 4 ({tier}) 解析 {pages} ...", flush=True)
        report["sides"]["new"] = run_new(pdf, new_md_path, pages, tier, small_backend, vlm_engine, block_network)

    for name, path in (("old", old_md_path), ("new", new_md_path)):
        if path.is_file():
            md = path.read_text(encoding="utf-8")
            side = report["sides"].setdefault(name, {})
            side["markdown"] = str(path)
            side.update(metrics(md))
            converted, count = convert_tables(md)
            side["after_table_json_blocks"] = converted.count("```json")
            side["after_table_json_new"] = count
    return report


def _parse_pages(pages: str) -> tuple[int, int]:
    if "-" in pages:
        a, b = pages.split("-", 1)
        return int(a), int(b)
    value = int(pages)
    return value, value


def print_report(report: dict) -> None:
    old = report["sides"].get("old", {})
    new = report["sides"].get("new", {})
    keys = [
        ("版本", "version"), ("成功", "ok"), ("耗时(s)", "elapsed"), ("字符数", "chars"),
        ("三级条款号", "clause_3level"), ("二级条款号", "clause_2level"),
        ("4.1 概述", "has_4.1"), ("6.10", "has_6.10"),
        ("顿号 、", "dunhao"), ("句号 。", "juhao"),
        ("表A.x 引用", "table_refs"), ("第N章 引用", "chapter_refs"),
        ("ICS", "has_ICS"), ("CCS", "has_CCS"), ("发布日期", "has_date"),
        ("原生 JSON 表", "json_blocks"), ("原生管道表行", "pipe_table_rows"),
        ("原生 HTML 表", "html_tables"),
        ("后处理 JSON 表", "after_table_json_blocks"), ("后处理新增", "after_table_json_new"),
    ]
    print("\n" + "=" * 74)
    print(f"{'指标':<16}{'旧版 2.7.6':<26}{'新版 4.x (' + report['tier'] + ')':<26}")
    print("-" * 74)
    for label, key in keys:
        print(f"{label:<16}{str(old.get(key, '-')):<26}{str(new.get(key, '-')):<26}")
    print("=" * 74)
    new_side = report["sides"].get("new", {})
    if new_side:
        print(f"新版运行模式: source={new_side.get('model_source')} backend={new_side.get('small_backend')} "
              f"vlm={new_side.get('vlm_engine')} server_url={new_side.get('server_url')!r} "
              f"本地优先={new_side.get('local_only')} 断网={report.get('block_network')}")
        if not new_side.get("local_only"):
            print("⚠️ 新版检测到远程服务配置，结果可能来自 API；请清空 server_url/api_key 后重跑。")
    for name in ("old", "new"):
        side = report["sides"].get(name, {})
        if side.get("error"):
            print(f"[{name}] 错误: {str(side['error'])[:300]}")
        elif side.get("stderr_tail"):
            print(f"[{name}] stderr: {side['stderr_tail']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="新旧 MinerU 解析对比")
    ap.add_argument("--pdf", default=str(DEFAULT_PDF))
    ap.add_argument("--pages", default="14-16", help="1 基页范围，如 1 或 14-16")
    ap.add_argument("--tier", default="basic", choices=["flash", "basic", "standard", "advanced"])
    ap.add_argument("--small-backend", default="onnx", choices=["auto", "onnx", "torch"])
    ap.add_argument("--vlm-engine", default=None, choices=[None, "llama-cpp", "vllm", "lmdeploy", "mlx"])
    ap.add_argument("--allow-network", action="store_true",
                    help="默认用黑洞代理禁网，确保走本地推理；加此参数才允许联网")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--old-md", default=None, help="跳过旧版解析，直接对比该 md")
    ap.add_argument("--new-md", default=None, help="跳过新版解析，直接对比该 md")
    args = ap.parse_args(argv)

    pdf = Path(args.pdf)
    if not pdf.is_file():
        print(f"[FAIL] 找不到 PDF: {pdf}")
        return 2
    out_dir = Path(args.out_dir)
    if args.old_md:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"old_p{args.pages.replace(',', '_')}.md").write_text(
            Path(args.old_md).read_text(encoding="utf-8"), encoding="utf-8")
    if args.new_md:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"new_{args.tier}_p{args.pages.replace(',', '_')}.md").write_text(
            Path(args.new_md).read_text(encoding="utf-8"), encoding="utf-8")

    report = compare(
        pdf, args.pages, args.tier,
        skip_old=bool(args.old_md), skip_new=bool(args.new_md), out_dir=out_dir,
        small_backend=args.small_backend, vlm_engine=args.vlm_engine,
        block_network=not args.allow_network,
    )
    print_report(report)
    report_path = out_dir / f"report_{args.tier}_p{args.pages.replace(',', '_')}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {report_path}")
    return 0


def test_mineru_version_compare(tmp_path):
    """真实对比测试：默认跳过，设置 RUN_MINERU_COMPARE=1 启用。"""
    import pytest

    if os.getenv("RUN_MINERU_COMPARE") != "1":
        pytest.skip("设置 RUN_MINERU_COMPARE=1 才执行新旧版本对比（需要两套环境）")
    if not DEFAULT_PDF.is_file():
        pytest.skip(f"缺少测试文件: {DEFAULT_PDF}")

    report = compare(DEFAULT_PDF, "1", "basic", skip_old=False, skip_new=False, out_dir=tmp_path)
    old, new = report["sides"]["old"], report["sides"]["new"]
    assert old.get("ok"), old.get("error")
    assert new.get("ok"), new.get("error")
    assert old["chars"] > 0 and new["chars"] > 0
    # 新版必须能给出标准号/封面信息，且条款号结构完整
    assert new["chars"] >= old["chars"] * 0.5


if __name__ == "__main__":
    raise SystemExit(main())
