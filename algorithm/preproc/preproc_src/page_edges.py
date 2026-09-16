"""页眉/页脚（页边带）保留工具。

MinerU 与 Docling 都会把页面上下边缘的文字当作页眉/页脚并从 Markdown 中丢弃：

* MinerU：版面模型把它们判成 ``abandon``，进入 ``discarded_blocks``；而
  ``MakeMode.MM_MD`` 只渲染 ``para_blocks``，``discarded_blocks`` 仅在
  ``MakeMode.CONTENT_LIST`` 下输出。
* Docling：这类内容被放进 ``FURNITURE`` 内容层，``export_to_markdown()`` 默认只导出
  ``BODY``。

对标准文件来说，封面左上角的 ``ICS 01.120`` / ``CCS A 00``、底部的发布与实施日期
恰恰是重要元数据，丢了很可惜。本模块用 PDF 文本层按带宽把这几行取回来，
**只补回引擎输出里缺失的行**（已出现在正文中的行会被跳过，避免重复），
再按页拼回 Markdown。

扫描件没有文本层，此时取不到页边带内容；MinerU 路线会退回到该页的
``discarded_blocks``，其余情况保持不变。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Sequence

# 页面上/下边缘的带宽比例：顶部 12% 覆盖 ICS/CCS 与页眉，底部 15% 覆盖脚注与页码区。
DEFAULT_TOP_BAND = 0.12
DEFAULT_BOTTOM_BAND = 0.15
# 短于该长度的行按噪声（水印、标志残片）丢弃，例如封面右上角的 "GB"。
DEFAULT_MIN_CHARS = 3

KEEP_MODES = ("off", "first_page", "all")

# 只做"是否已存在"比较用的归一化：去掉空白与常见标点，避免排版差异造成误判。
_STRIP_PATTERN = re.compile(r"[\s\u3000,.;:，。；：、()（）\[\]【】/\\|\-—_~·\"'“”‘’]+")


def normalize_keep_page_edges(value: Any) -> str | set[int] | None:
    """把配置值归一化为 None（关闭）、"all"、"first_page" 或 1 基页码集合。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return "first_page" if value else None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "off", "none", "false", "0"):
            return None
        if text in ("all", "true"):
            return "all"
        if text in ("first_page", "first", "first-page", "cover"):
            return "first_page"
        raise ValueError(
            "keep_page_edges 仅支持 off、first_page、all 或 1 基页码数组，收到: " + repr(value)
        )
    if isinstance(value, (list, tuple, set)):
        pages = set()
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError("keep_page_edges 页码数组只能包含整数: " + repr(value))
            if item < 1:
                raise ValueError("keep_page_edges 页码从 1 开始: " + repr(value))
            pages.add(item)
        return pages or None
    raise ValueError(
        "keep_page_edges 仅支持 off、first_page、all 或 1 基页码数组，收到: " + repr(value)
    )


def page_is_kept(selection: str | set[int] | None, page_number: int, first_page_number: int) -> bool:
    """判断 1 基页码 ``page_number`` 是否要保留页边内容。

    ``"first_page"`` 指本次解析范围内的第一页（分片解析时即该分片首页）。
    """
    if selection is None:
        return False
    if selection == "all":
        return True
    if selection == "first_page":
        return page_number == first_page_number
    return page_number in selection


def _split_lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").replace("\r", "\n").split("\n") if line.strip()]


def normalize_for_match(text: str) -> str:
    """去掉空白与标点，用于判断某行是否已经出现在引擎输出中。"""
    return _STRIP_PATTERN.sub("", str(text or "")).lower()


def extract_edge_lines(
    pdf_source: str | Path | bytes,
    page_number: int,
    *,
    top_band: float = DEFAULT_TOP_BAND,
    bottom_band: float = DEFAULT_BOTTOM_BAND,
    min_chars: int = DEFAULT_MIN_CHARS,
) -> dict[str, list[str]]:
    """从 PDF 文本层取指定页（1 基）上/下边缘带的文本行。

    返回 ``{"top": [...], "bottom": [...]}``；没有文本层（扫描件）或依赖缺失时返回空列表。
    """
    empty = {"top": [], "bottom": []}
    try:
        import pypdfium2 as pdfium
    except ModuleNotFoundError:
        return empty

    pdf = None
    try:
        if isinstance(pdf_source, (bytes, bytearray)):
            pdf = pdfium.PdfDocument(bytes(pdf_source))
        else:
            path = Path(pdf_source)
            if not path.is_file():
                return empty
            pdf = pdfium.PdfDocument(str(path))
        if page_number < 1 or page_number > len(pdf):
            return empty
        page = pdf[page_number - 1]
        width, height = page.get_size()
        textpage = page.get_textpage()
        top_text = textpage.get_text_bounded(left=0, bottom=height * (1 - top_band), right=width, top=height)
        bottom_text = textpage.get_text_bounded(left=0, bottom=0, right=width, top=height * bottom_band)
    except Exception:
        return empty
    finally:
        if pdf is not None:
            try:
                pdf.close()
            except Exception:
                pass

    def _clean(text: str) -> list[str]:
        return [line for line in _split_lines(text) if len(line) >= min_chars]

    return {"top": _clean(top_text), "bottom": _clean(bottom_text)}


def lines_from_blocks(blocks: Iterable[dict], min_chars: int = DEFAULT_MIN_CHARS) -> list[str]:
    """从 MinerU ``discarded_blocks`` 一类的块里取文本行（扫描件回退路径）。"""
    lines: list[str] = []
    for block in blocks or []:
        for line in block.get("lines", []) or []:
            text = "".join(span.get("content", "") for span in line.get("spans", []) or []).strip()
            if len(text) >= min_chars:
                lines.append(text)
    return lines


def missing_lines(lines: Sequence[str], rendered_markdown: str) -> list[str]:
    """只保留没有出现在 ``rendered_markdown`` 中的行，避免与正文重复。"""
    haystack = normalize_for_match(rendered_markdown)
    kept: list[str] = []
    for line in lines or []:
        needle = normalize_for_match(line)
        if not needle or needle in haystack:
            continue
        if needle in (normalize_for_match(k) for k in kept):
            continue
        kept.append(line)
    return kept


def merge_page_edges(page_markdown: str, top_lines: Sequence[str], bottom_lines: Sequence[str]) -> str:
    """把补回的页眉行放在该页 Markdown 之前、页脚行之后。"""
    parts: list[str] = []
    if top_lines:
        parts.append("\n".join(top_lines))
    body = (page_markdown or "").strip()
    if body:
        parts.append(body)
    if bottom_lines:
        parts.append("\n".join(bottom_lines))
    return "\n\n".join(parts)
