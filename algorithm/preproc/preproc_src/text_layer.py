"""用 PDF 文本层校正解析器输出的排版伪影。

Docling 在拼接文本单元时会按字形间距补空格，而本类标准 PDF 使用宽字距的西文字体，
于是 ``GB/T1.1`` 被导出成 ``GB / T1 . 1``、``4.1`` 变成 ``4 . 1``、中文标点两侧也会多出空格。
PDF 文本层没有这些伪影，因此这里做一次"逐行对账"：

* 只把 Markdown 行的正文部分与文本层行做**去掉空白后完全一致**的比较；
* 仅当文本层里存在**唯一**匹配行时才替换；
* 命中不了就原样保留，因此不会引入新内容，也不会破坏表格、图片与 Markdown 结构。

扫描件没有文本层，此时本模块是空操作。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

_WHITESPACE = re.compile(r"[\s\u3000]+")
# 连字符/破折号变体在导出时常被归一化（— 变成 -），比对时统一折叠。
_DASHES = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-", "\uff0d": "-", "\u30fc": "-",
})
# Markdown 行首结构：标题、引用、列表符号、有序列表
_LINE_PREFIX = re.compile(r"^(#{1,6}\s+|[-*+>]\s+|\d+[.)]\s+)")


def normalize_for_compare(text: str) -> str:
    """去掉所有空白并折叠破折号变体，用于与文本层行比对。"""
    return _WHITESPACE.sub("", str(text or "")).translate(_DASHES)


def split_markdown_line(line: str) -> tuple[str, str]:
    """把一行 Markdown 拆成"结构前缀 + 正文"，便于只替换正文。"""
    indent = line[: len(line) - len(line.lstrip())]
    body = line.strip()
    match = _LINE_PREFIX.match(body)
    if match:
        return indent + match.group(0), body[match.end():]
    return indent, body


def page_text_lines(
    pdf_source: str | Path | bytes,
    page_number: int,
) -> list[str]:
    """取指定页（1 基）文本层的全部文本行；缺依赖、越界或读取失败时返回空列表。"""
    try:
        import pypdfium2 as pdfium
    except ModuleNotFoundError:
        return []

    pdf = None
    try:
        if isinstance(pdf_source, (bytes, bytearray)):
            pdf = pdfium.PdfDocument(bytes(pdf_source))
        else:
            path = Path(pdf_source)
            if not path.is_file():
                return []
            pdf = pdfium.PdfDocument(str(path))
        if page_number < 1 or page_number > len(pdf):
            return []
        text = pdf[page_number - 1].get_textpage().get_text_bounded()
    except Exception:
        return []
    finally:
        if pdf is not None:
            try:
                pdf.close()
            except Exception:
                pass

    return [line.strip() for line in str(text or "").replace("\r", "\n").split("\n") if line.strip()]


def build_line_index(lines: Sequence[str]) -> dict[str, str]:
    """建立"去空白文本 -> 文本层原行"的索引，出现歧义时标记为不可用。"""
    index: dict[str, str] = {}
    for line in lines or []:
        key = normalize_for_compare(line)
        if not key:
            continue
        if key in index:
            index[key] = ""  # 多个候选，放弃替换
        else:
            index[key] = line.strip()
    return index


def reconcile_markdown(markdown: str, lines: Sequence[str]) -> tuple[str, int]:
    """用文本层行替换 Markdown 中排版走样但内容相同的行，返回 (新文本, 替换行数)。"""
    index = build_line_index(lines)
    if not index:
        return markdown, 0

    replaced = 0
    output: list[str] = []
    for raw_line in str(markdown or "").split("\n"):
        prefix, body = split_markdown_line(raw_line)
        replacement = index.get(normalize_for_compare(body)) if body else None
        if replacement and replacement != body:
            output.append(prefix + replacement)
            replaced += 1
        else:
            output.append(raw_line)
    return "\n".join(output), replaced


# 导出器在数字与分隔符之间插入的空格：4 . 5 -> 4.5、2 0 2 6 -> 2026、A. 6 -> A.6。
_NUMBER_SPACING = (
    (re.compile(r"(?<=\d)[ \t]*\.[ \t]*(?=\d)"), "."),
    (re.compile(r"(?<=\d)[ \t]+(?=\d)"), ""),
    (re.compile(r"(?<=[A-Za-z0-9])\.[ \t]+(?=\d)"), "."),
    (re.compile(r"(?<=[A-Za-z0-9])[ \t]*/[ \t]*(?=[A-Za-z0-9])"), "/"),
)


def tighten_number_artifacts(markdown: str) -> str:
    """收拢数字/编号内部被插入的空格。

    只处理 `数字.数字`、`数字 数字`、`字母. 数字`、`X / Y` 这几类形态，
    `Part 4`、`第 4 部分` 这类正常空格不受影响。
    """
    text = str(markdown or "")
    for pattern, replacement in _NUMBER_SPACING:
        text = pattern.sub(replacement, text)
    return text


def merge_split_headings(markdown: str, lines: Sequence[str]) -> tuple[str, int]:
    """合并被导出器拆成两行的条款标题，例如 ``## 6`` + ``## . 1 0 废止阶段``。

    只有"拼接结果去掉空白后确实存在于该页文本层"时才合并，
    因此不会把两个无关标题粘在一起。
    """
    known = {key for key in build_line_index(lines) if key}
    if not known:
        return markdown, 0

    rows = str(markdown or "").split("\n")
    output: list[str] = []
    merged = 0
    index = 0
    while index < len(rows):
        prefix, body = split_markdown_line(rows[index])
        if prefix.startswith("#") and body and re.fullmatch(r"\d+(\.\d+)*", body):
            # 向后找第一个非空行，尝试把标题编号与紧随其后的标题拼回去
            nxt = index + 1
            while nxt < len(rows) and not rows[nxt].strip():
                nxt += 1
            if nxt < len(rows):
                next_prefix, next_body = split_markdown_line(rows[nxt])
                candidate = body + next_body
                if (
                    next_prefix.startswith("#")
                    and next_body
                    and normalize_for_compare(candidate) in known
                ):
                    output.append(prefix + tighten_number_artifacts(candidate))
                    index = nxt + 1
                    merged += 1
                    continue
        output.append(rows[index])
        index += 1
    return "\n".join(output), merged
