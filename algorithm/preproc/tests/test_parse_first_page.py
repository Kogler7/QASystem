#!/usr/bin/env python3
"""解析测试：用 algorithm/preproc 的 DocumentProcessor 解析 PDF 的第一页。

只解析 PDF 的第 1 页（`start_page_id=0, end_page_id=0`，MinerU 语义为 0 基、闭区间），
把 DocumentProcessor 返回的 Markdown 落盘并打印解析元数据。

直接运行：

    python test_artifacts/parser/test_parse_first_page.py

指定输入输出与引擎：

    python test_artifacts/parser/test_parse_first_page.py \
        --pdf "test_artifacts/parser/解析测试数据/xxx.pdf" \
        --engine mineru \
        --out  /tmp/first_page.md

本机（无 GPU、模型目录在 /mnt/data3/qasys/models）运行示例：

    MINERU_MODEL_SOURCE=local \
    MINERU_MODEL_PIPELINE_PATH=/mnt/data3/qasys/models/mineru/pipeline \
    MINERU_MODEL_VLM_PATH=/mnt/data3/qasys/models/mineru/vlm \
    python test_artifacts/parser/test_parse_first_page.py

pytest 集成：真实解析需要加载模型、耗时较长，因此默认 skip；
设置 `RUN_DOCUMENT_PARSE_TEST=1` 后才执行。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_PDF = (
    ROOT
    / "test_artifacts/parser/解析测试数据"
    / "GBT+48000.4-2026 标准数字化 第4部分：协同制定要求.pdf"
)
FIRST_PAGE = (0, 0)  # start_page_id, end_page_id —— 0 基且闭区间，即仅第 1 页


def _build_processor(output_dir: str):
    from algorithm.preproc.preproc_src.main_processor import DocumentProcessor

    return DocumentProcessor(output_dir=output_dir)


def parse_first_page(
    pdf: str | Path,
    out: str | Path,
    engine: str = "mineru",
    force: bool = True,
    lang: str = "ch",
    ocr_mode: str = "auto",
    table_enable: bool = True,
    formula_enable: bool = True,
    output_dir: str | Path | None = None,
) -> dict:
    """解析 `pdf` 的第 1 页，写出 Markdown，返回结果摘要。"""
    pdf = Path(pdf)
    out = Path(out)
    if not pdf.is_file():
        raise FileNotFoundError(f"待解析文件不存在: {pdf}")

    output_dir = Path(output_dir or (ROOT / "work/parser_output"))
    processor = _build_processor(str(output_dir))
    # 仅用于记录自动引擎选择，实际执行由 process_file 内部再次完成选择。
    selected, selection = processor.select_engine(str(pdf), requested=engine, force=force)

    started = time.time()
    result = processor.process_file(
        str(pdf),
        model_name=engine,
        force=force,
        lang=lang,
        ocr_mode=ocr_mode,
        table_enable=table_enable,
        formula_enable=formula_enable,
        start_page_id=FIRST_PAGE[0],
        end_page_id=FIRST_PAGE[1],
    )
    elapsed = round(time.time() - started, 1)

    summary = {
        "success": bool(result.get("success")),
        "pdf": str(pdf),
        "engine_requested": engine,
        "engine_selected_by_auto": selected,
        "auto_selection": selection,
        "page_range": {"start_page_id": FIRST_PAGE[0], "end_page_id": FIRST_PAGE[1]},
        "elapsed_seconds": elapsed,
        "metadata": result.get("metadata", {}),
    }
    if not result.get("success"):
        summary["error"] = result.get("error")
        return summary

    content = result["content"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    summary.update(
        {
            "markdown_path": str(out),
            "markdown_chars": len(content),
            "markdown_lines": len(content.splitlines()),
            "markdown_head": content.strip().splitlines()[0][:120] if content.strip() else "",
        }
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用 algorithm/preproc 解析 PDF 第 1 页")
    parser.add_argument("--pdf", default=str(DEFAULT_PDF), help="待解析 PDF 路径")
    parser.add_argument("--out", default=None, help="输出 Markdown 路径")
    parser.add_argument("--engine", default="mineru", choices=["mineru", "vlm", "docling"])
    parser.add_argument("--no-force", dest="force", action="store_false", help="关闭 force，走自动选择")
    parser.add_argument("--lang", default="ch")
    parser.add_argument("--ocr-mode", default="auto", choices=["auto", "on", "off"])
    parser.add_argument("--no-table", dest="table_enable", action="store_false")
    parser.add_argument("--no-formula", dest="formula_enable", action="store_false")
    parser.add_argument("--output-dir", default=None, help="DocumentProcessor 中间产物目录")
    parser.add_argument("--pipeline-models", default=None, help="覆盖 MINERU_MODEL_PIPELINE_PATH")
    parser.add_argument("--vlm-models", default=None, help="覆盖 MINERU_MODEL_VLM_PATH")
    args = parser.parse_args(argv)

    if args.pipeline_models:
        os.environ["MINERU_MODEL_PIPELINE_PATH"] = args.pipeline_models
    if args.vlm_models:
        os.environ["MINERU_MODEL_VLM_PATH"] = args.vlm_models

    out = Path(args.out) if args.out else Path(args.pdf).with_name(Path(args.pdf).stem + "_第1页.md")

    try:
        summary = parse_first_page(
            args.pdf,
            out,
            engine=args.engine,
            force=args.force,
            lang=args.lang,
            ocr_mode=args.ocr_mode,
            table_enable=args.table_enable,
            formula_enable=args.formula_enable,
            output_dir=args.output_dir,
        )
    except FileNotFoundError as exc:
        print(f"[FAIL] {exc}")
        return 2

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    if not summary["success"]:
        print(f"[FAIL] 解析失败: {summary.get('error')}")
        return 1
    print(f"[OK] {summary['markdown_path']} ({summary['markdown_chars']} 字符, {summary['elapsed_seconds']}s)")
    return 0


def test_parse_first_page(tmp_path):
    """真实解析第 1 页；默认跳过，设置 RUN_DOCUMENT_PARSE_TEST=1 启用。"""
    import pytest

    if os.getenv("RUN_DOCUMENT_PARSE_TEST") != "1":
        pytest.skip("设置 RUN_DOCUMENT_PARSE_TEST=1 才执行真实文档解析")
    if not DEFAULT_PDF.is_file():
        pytest.skip(f"缺少测试文件: {DEFAULT_PDF}")

    out = tmp_path / "first_page.md"
    summary = parse_first_page(DEFAULT_PDF, out)

    assert summary["success"], summary.get("error")
    assert summary["page_range"] == {"start_page_id": 0, "end_page_id": 0}
    content = out.read_text(encoding="utf-8")
    assert content.strip(), "第 1 页解析结果为空"
    assert "标准数字化" in content, "第 1 页 Markdown 未包含封面标题关键词"


if __name__ == "__main__":
    raise SystemExit(main())
