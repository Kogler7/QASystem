"""测试脚本：解析指定 PDF 的指定页，并返回解析后的 Markdown 文件。

用法：
    python algorithm/preproc/test_parse_pdf.py \
        --pdf "KGQA/test_docs/GBT+48000.4-2026 标准数字化 第4部分：协同制定要求.pdf" \
        --page 1 \
        --out-dir /tmp/parse_out

说明：
    - 复用 algorithm/preproc/preproc_src/main_processor.DocumentProcessor。
    - --page 为 1-based 页码，内部换算为 start_page_id / end_page_id 传入 process_file。
    - --engine 可选，默认走 auto（会依据版面自动选择 vlm/mineru）。
    - 成功时在输出目录生成 *.md，并打印文件路径与解析元信息。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 使项目根目录下的 algorithm 包可被导入
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="解析 PDF 指定页并输出 Markdown")
    parser.add_argument("pdf", type=str, help="待解析 PDF 文件路径")
    parser.add_argument("--page", type=int, default=1, help="要解析的页码（1-based，默认第 1 页）")
    parser.add_argument("--out-dir", type=str, default=None, help="输出目录（含 .md 与中间产物）")
    parser.add_argument("--engine", type=str, default=None,
                        help="指定解析引擎：vlm / mineru / docling，默认 auto")
    parser.add_argument("--ocr-mode", type=str, default="auto", choices=["auto", "on", "off"])
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"[错误] PDF 文件不存在：{pdf_path}")
        return 2

    page_id = args.page - 1  # 内部 0-based 页码
    if page_id < 0:
        print("[错误] --page 必须 >= 1")
        return 2

    from algorithm.preproc.preproc_src.main_processor import DocumentProcessor

    processor = DocumentProcessor(output_dir=args.out_dir)
    result = processor.process_file(
        file_path=str(pdf_path),
        model_name=args.engine,        # auto 时传 None
        lang="ch",
        ocr_mode=args.ocr_mode,
        start_page_id=page_id,
        end_page_id=page_id,           # 只解析该单页
        allow_empty_content=True,
    )

    if not result.get("success"):
        print("[解析失败]")
        print("  error_code:", result.get("error_code"))
        print("  error:     ", result.get("error"))
        print("  metadata:  ", result.get("metadata"))
        return 1

    content = result.get("content") or ""
    if not content.strip():
        print("[提示] 该页未生成有效内容（可能是空白页，engine 元信息已记录）")

    # 直接写入最终 md 文件
    md_path = Path(args.out_dir or PROJECT_ROOT / "tmp" / "processed")
    md_path = md_path / f"{pdf_path.stem}_p{args.page}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(content, encoding="utf-8")

    print("[解析成功]")
    print(f"  Markdown 文件: {md_path}")
    print(f"  字符数:        {len(content)}")
    metadata = result.get("metadata", {})
    print(f"  解析引擎:      {metadata.get('engine_selection')} | model_used={metadata.get('model_used')}")
    print(f"  输出目录:      {metadata.get('output_dir')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())