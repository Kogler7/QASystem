"""Dependency-free structural checks for benchmark Markdown documents.

The benchmark is consumed as Markdown source, so rendering-critical defects
must fail before extraction: malformed headings, tables, formulas, or images
would otherwise become extraction noise and make scores irreproducible.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlparse


_FENCE = re.compile(r"^\s*([`~]{3,})(?:\s*.*)?$")
_HEADING = re.compile(r"^\s*(#{1,6})(?:[ \t]+)(.*?)(?:[ \t]+#+[ \t]*)?$")
_HEADING_PREFIX = re.compile(r"^\s*#{1,6}\S")
_TABLE_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_IMAGE = re.compile(
    r"!\[(?P<alt>[^\]\n]*)\]\(\s*"
    r"(?P<target><[^>\n]+>|[^)\s]+)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)"
)
_INLINE_CODE = re.compile(r"`+[^`\n]*`+")
_LATEX_ENV = re.compile(r"\\(?P<kind>begin|end)\{(?P<name>[^{}\s]+)\}")
_NUMBERED_HEADING = re.compile(
    r"^(?P<number>(?:\d+(?:\.\d+)*|[A-Z](?:\.\d+)+))(?:\s+(?P<title>\S.*))?$"
)


@dataclass(frozen=True)
class MarkdownIssue:
    code: str
    message: str
    line: int | None = None
    value: Any = None


class _HTMLStructureParser(HTMLParser):
    """Validate table nesting and collect HTML image targets."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.issues: list[MarkdownIssue] = []
        self.images: list[tuple[str, str, int]] = []
        self._stack: list[tuple[str, int]] = []
        self._tables: list[dict[str, int | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        line = self.getpos()[0]
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if tag == "img":
            self.images.append((attributes.get("alt", ""), attributes.get("src", ""), line))
            return
        if tag not in {"table", "tr", "td", "th"}:
            return
        if tag == "table":
            self._tables.append({"rows": 0, "cells": None})
        elif tag == "tr":
            if not self._tables or self._tables[-1]["cells"] is not None:
                self._invalid("HTML <tr> 必须直接位于有效 table 行结构中", line)
            elif self._tables:
                self._tables[-1]["cells"] = 0
        else:
            if not self._tables or self._tables[-1]["cells"] is None:
                self._invalid(f"HTML <{tag}> 必须位于 <tr> 中", line)
            else:
                self._tables[-1]["cells"] = int(self._tables[-1]["cells"] or 0) + 1
        self._stack.append((tag, line))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag not in {"table", "tr", "td", "th"}:
            return
        line = self.getpos()[0]
        if not self._stack or self._stack[-1][0] != tag:
            expected = self._stack[-1][0] if self._stack else "无"
            self._invalid(f"HTML </{tag}> 闭合顺序错误，当前应闭合 {expected}", line)
            matching = next(
                (index for index in range(len(self._stack) - 1, -1, -1) if self._stack[index][0] == tag),
                None,
            )
            if matching is None:
                return
            del self._stack[matching:]
        else:
            self._stack.pop()

        if tag == "tr" and self._tables:
            cells = self._tables[-1]["cells"]
            if not cells:
                self._invalid("HTML table 行至少需要一个 th/td 单元格", line)
            self._tables[-1]["rows"] = int(self._tables[-1]["rows"] or 0) + 1
            self._tables[-1]["cells"] = None
        elif tag == "table" and self._tables:
            table = self._tables.pop()
            if not table["rows"]:
                self._invalid("HTML table 至少需要一行", line)

    def close(self) -> None:
        super().close()
        for tag, line in self._stack:
            self._invalid(f"HTML <{tag}> 未闭合", line)

    def _invalid(self, message: str, line: int) -> None:
        self.issues.append(MarkdownIssue("HTML_TABLE_INVALID", message, line))


def _pipe_cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in stripped:
        if character == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(character)
        escaped = character == "\\" and not escaped
        if character != "\\":
            escaped = False
    cells.append("".join(current).strip())
    return cells


def _validate_image_target(
    target: str,
    *,
    line: int,
    document_path: Path | None,
) -> MarkdownIssue | None:
    cleaned = unquote(target.strip().strip("<>"))
    parsed = urlparse(cleaned)
    if parsed.scheme:
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            return MarkdownIssue(
                "IMAGE_TARGET_INVALID",
                "图片地址只允许有效的 HTTP(S) URL 或存在的本地相对路径",
                line,
                target,
            )
        return None
    if not cleaned or cleaned.startswith(("/", "\\")):
        return MarkdownIssue("IMAGE_TARGET_INVALID", "本地图片必须使用相对路径", line, target)
    if document_path is not None:
        local_path = (document_path.parent / cleaned.split("#", 1)[0].split("?", 1)[0]).resolve()
        if not local_path.is_file():
            return MarkdownIssue("IMAGE_TARGET_MISSING", "本地图片文件不存在", line, target)
    return None


def lint_markdown(text: str, *, document_path: Path | None = None) -> list[MarkdownIssue]:
    """Return rendering-critical Markdown structure issues."""
    issues: list[MarkdownIssue] = []
    lines = text.splitlines()
    active_lines: list[tuple[int, str]] = []
    fence: tuple[str, int, int] | None = None
    previous_heading_level: int | None = None

    for line_number, line in enumerate(lines, 1):
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker), line_number)
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        active_lines.append((line_number, line))

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if not title:
                issues.append(MarkdownIssue("HEADING_EMPTY", "标题内容不能为空", line_number))
            if previous_heading_level is not None and level > previous_heading_level + 1:
                issues.append(
                    MarkdownIssue(
                        "HEADING_LEVEL_JUMP",
                        f"标题层级从 H{previous_heading_level} 跳到 H{level}",
                        line_number,
                    )
                )
            previous_heading_level = level
        elif _HEADING_PREFIX.match(line):
            issues.append(
                MarkdownIssue("HEADING_SYNTAX_INVALID", "ATX 标题的 # 后必须有空格", line_number)
            )

    if fence is not None:
        issues.append(MarkdownIssue("CODE_FENCE_UNCLOSED", "代码围栏未闭合", fence[2]))

    active_by_line = {line_number: line for line_number, line in active_lines}
    _lint_pipe_tables(active_by_line, issues)
    _lint_formulas(active_lines, issues)

    active_text = "\n".join(line for _, line in active_lines)
    parser = _HTMLStructureParser()
    parser.feed(active_text)
    parser.close()
    issues.extend(parser.issues)

    markdown_image_count = 0
    for line_number, line in active_lines:
        for match in _IMAGE.finditer(line):
            markdown_image_count += 1
            if not match.group("alt").strip():
                issues.append(MarkdownIssue("IMAGE_ALT_MISSING", "图片必须提供非空替代文本", line_number))
            target_issue = _validate_image_target(
                match.group("target"),
                line=line_number,
                document_path=document_path,
            )
            if target_issue is not None:
                issues.append(target_issue)
    raw_image_markers = sum(line.count("![") for _, line in active_lines)
    if raw_image_markers > markdown_image_count:
        issues.append(MarkdownIssue("IMAGE_SYNTAX_INVALID", "存在无法解析的 Markdown 图片语法"))
    for alt, target, line_number in parser.images:
        if not alt.strip():
            issues.append(MarkdownIssue("IMAGE_ALT_MISSING", "HTML 图片必须提供非空 alt", line_number))
        target_issue = _validate_image_target(
            target,
            line=line_number,
            document_path=document_path,
        )
        if target_issue is not None:
            issues.append(target_issue)

    for line_number, line in active_lines:
        if any((ord(character) < 32 and character not in "\t") or ord(character) == 127 for character in line):
            issues.append(MarkdownIssue("CONTROL_CHARACTER", "文档包含不可渲染控制字符", line_number))
    return issues


def lint_heading_hierarchy(
    texts: Iterable[str],
    *,
    require_h1: bool = False,
    single_h1: bool = False,
) -> list[MarkdownIssue]:
    """Validate one logical document's headings, including physical parts."""
    issues: list[MarkdownIssue] = []
    headings: list[tuple[int, str, int]] = []
    logical_line = 0
    fence: tuple[str, int] | None = None
    frontmatter = False
    at_document_start = True
    for text in texts:
        for line in text.splitlines():
            logical_line += 1
            stripped = line.strip()
            if at_document_start and stripped == "---":
                frontmatter = True
                at_document_start = False
                continue
            if frontmatter:
                if stripped == "---":
                    frontmatter = False
                continue
            at_document_start = not stripped
            fence_match = _FENCE.match(line)
            if fence_match:
                marker = fence_match.group(1)
                if fence is None:
                    fence = (marker[0], len(marker))
                elif marker[0] == fence[0] and len(marker) >= fence[1]:
                    fence = None
                continue
            if fence is not None:
                continue
            heading = _HEADING.match(line)
            if heading:
                headings.append(
                    (len(heading.group(1)), heading.group(2).strip(), logical_line)
                )

    if require_h1 and (not headings or headings[0][0] != 1):
        issues.append(
            MarkdownIssue(
                "HEADING_H1_MISSING",
                "逻辑文档必须以唯一 H1 作为文档标题",
                headings[0][2] if headings else 1,
            )
        )
    h1_count = sum(level == 1 for level, _, _ in headings)
    if single_h1 and h1_count > 1:
        issues.append(
            MarkdownIssue("HEADING_H1_MULTIPLE", "逻辑文档只能有一个 H1", value=h1_count)
        )

    previous_level: int | None = None
    for level, title, line_number in headings:
        if previous_level is not None and level > previous_level + 1:
            issues.append(
                MarkdownIssue(
                    "HEADING_LEVEL_JUMP",
                    f"标题层级从 H{previous_level} 跳到 H{level}",
                    line_number,
                )
            )
        previous_level = level
        numbered = _NUMBERED_HEADING.fullmatch(title)
        if numbered is None:
            continue
        number = numbered.group("number")
        title_text = numbered.group("title")
        expected_level = len(number.split(".")) + 1
        if not title_text:
            issues.append(
                MarkdownIssue(
                    "HEADING_NUMBER_TITLE_MISSING",
                    "编号标题必须在同一行包含标题文本",
                    line_number,
                    title,
                )
            )
        if level != expected_level:
            issues.append(
                MarkdownIssue(
                    "HEADING_NUMBER_LEVEL_MISMATCH",
                    f"编号 {number} 应使用 H{expected_level}",
                    line_number,
                    title,
                )
            )
    return issues


def _lint_pipe_tables(active_by_line: dict[int, str], issues: list[MarkdownIssue]) -> None:
    if not active_by_line:
        return
    maximum_line = max(active_by_line)
    for line_number in range(2, maximum_line + 1):
        separator = active_by_line.get(line_number)
        header = active_by_line.get(line_number - 1)
        if separator is None or header is None or "|" not in separator:
            continue
        separator_cells = _pipe_cells(separator)
        if not separator_cells or not all(_TABLE_SEPARATOR_CELL.fullmatch(cell) for cell in separator_cells):
            continue
        expected = len(separator_cells)
        if len(_pipe_cells(header)) != expected:
            issues.append(
                MarkdownIssue(
                    "TABLE_COLUMN_MISMATCH",
                    "Markdown 表头与分隔行列数不一致",
                    line_number,
                )
            )
        body_line = line_number + 1
        while body_line <= maximum_line:
            row = active_by_line.get(body_line)
            if row is None or not row.strip() or "|" not in row or row.lstrip().startswith("<"):
                break
            if len(_pipe_cells(row)) != expected:
                issues.append(
                    MarkdownIssue(
                        "TABLE_COLUMN_MISMATCH",
                        f"Markdown 表格数据行应有 {expected} 列",
                        body_line,
                    )
                )
            body_line += 1


def _lint_formulas(
    active_lines: list[tuple[int, str]],
    issues: list[MarkdownIssue],
) -> None:
    block_open_line: int | None = None
    block_has_content = False
    bracket_stack: list[tuple[str, int]] = []
    environment_stack: list[tuple[str, int]] = []

    for line_number, raw_line in active_lines:
        line = _INLINE_CODE.sub("", raw_line)
        cursor = 0
        for match in re.finditer(r"(?<!\\)\$\$", line):
            segment = line[cursor : match.start()]
            if block_open_line is not None and segment.strip():
                block_has_content = True
            if block_open_line is None:
                block_open_line = line_number
                block_has_content = False
            else:
                if not block_has_content:
                    issues.append(MarkdownIssue("FORMULA_EMPTY", "块级公式内容不能为空", block_open_line))
                block_open_line = None
            cursor = match.end()
        if block_open_line is not None and line[cursor:].strip():
            block_has_content = True

        without_blocks = re.sub(r"(?<!\\)\$\$", "", line)
        for match in re.finditer(r"(?<!\\)(\\\[|\\\]|\\\(|\\\))", without_blocks):
            token = match.group(1)
            if token in {r"\[", r"\("}:
                bracket_stack.append((token, line_number))
                continue
            expected = r"\[" if token == r"\]" else r"\("
            if not bracket_stack or bracket_stack[-1][0] != expected:
                issues.append(MarkdownIssue("FORMULA_DELIMITER_INVALID", f"公式结束符 {token} 没有匹配的开始符", line_number))
            else:
                bracket_stack.pop()

        # A lone dollar is commonly a currency sign or a regular-expression
        # anchor.  Only diagnose an odd delimiter count when the line already
        # contains multiple single-dollar markers and therefore clearly tries
        # to use inline math.
        single_dollars = list(re.finditer(r"(?<!\\)(?<!\$)\$(?!\$)", without_blocks))
        if len(single_dollars) > 1 and len(single_dollars) % 2:
            issues.append(MarkdownIssue("FORMULA_UNCLOSED", "行内 $ 公式未闭合", line_number))

        for match in _LATEX_ENV.finditer(line):
            kind = match.group("kind")
            name = match.group("name")
            if kind == "begin":
                environment_stack.append((name, line_number))
            elif not environment_stack or environment_stack[-1][0] != name:
                issues.append(MarkdownIssue("FORMULA_ENV_INVALID", f"LaTeX 环境 {name} 闭合顺序错误", line_number))
            else:
                environment_stack.pop()

    if block_open_line is not None:
        issues.append(MarkdownIssue("FORMULA_UNCLOSED", "$$ 块级公式未闭合", block_open_line))
    for token, line_number in bracket_stack:
        issues.append(MarkdownIssue("FORMULA_UNCLOSED", f"公式开始符 {token} 未闭合", line_number))
    for name, line_number in environment_stack:
        issues.append(MarkdownIssue("FORMULA_ENV_INVALID", f"LaTeX 环境 {name} 未闭合", line_number))


__all__ = ["MarkdownIssue", "lint_heading_hierarchy", "lint_markdown"]
