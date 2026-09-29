"""确定性 chunk 元数据补全（只填空缺字段，不改文本与 chunk 边界）。

上游（文本解析 + 语义切片）经常只提供 chunk 文本；文档路由信号、
``same_section`` 局部上下文与章节归属都依赖 ``section_id`` /
``section_title`` / ``clause_number`` / ``document_id``。本模块从 Markdown
标题、``第X章/节/条`` 与 ``1.2.3`` 编号标题里做确定性派生，并在完全缺失
``document_id`` 时按顺序补默认文档号（偏移回退视为文档边界）。
"""
from __future__ import annotations

import re
from typing import Iterable

from .chunk_context import ExtractionChunk

_MD_HEADING = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*#*\s*$")
_SECTION_LABEL = re.compile(
    r"^第\s*[一二三四五六七八九十百千零〇\d]+\s*[章节部分]\s*[、．.：:]?\s*(?P<title>.*)$"
)
_CLAUSE_LABEL = re.compile(
    r"^(?P<label>第\s*[一二三四五六七八九十百千零〇\d]+\s*条)\s*[、．.：:]?\s*(?P<title>.*)$"
)
_NUMBERED_HEADING = re.compile(
    r"^(?P<number>\d+(?:\.\d+){0,3})\s*[、．.]?\s+(?P<title>\S.*)$"
)


def _number_clause(title: str) -> str:
    match = _NUMBERED_HEADING.match(title.strip())
    return match.group("number") if match else ""


def _clause_label(line: str) -> str:
    match = _CLAUSE_LABEL.match(line)
    if not match:
        return ""
    return re.sub(r"\s+", "", match.group("label"))


def _first_clause_label(lines: list[str]) -> str:
    for line in lines:
        label = _clause_label(line)
        if label:
            return label
    return ""


def _heading_of(text: str) -> tuple[int, str, str] | None:
    """返回 chunk 内首个标题行 ``(level, title, clause_number)``。"""
    lines = [raw.strip() for raw in (text or "").splitlines()]
    lines = [line for line in lines if line]
    for index, line in enumerate(lines):
        markdown = _MD_HEADING.match(line)
        if markdown:
            title = markdown.group("title").strip()
            # 标题行本身没有条号时，继续在正文里找首条条标（如 "## 第一章" 后跟 "第一条 …"）。
            clause = _number_clause(title) or _first_clause_label(lines[index + 1 :])
            return len(markdown.group("level")), title, clause
        clause = _CLAUSE_LABEL.match(line)
        if clause:
            return 3, (clause.group("title") or line).strip() or line, _clause_label(line)
        section = _SECTION_LABEL.match(line)
        if section:
            return 1, (section.group("title") or line).strip() or line, ""
        numbered = _NUMBERED_HEADING.match(line)
        if numbered:
            number = numbered.group("number")
            # 防误判：无点号编号要求短编号（"1 总则"），排除 "2023 年数据" 这类正文行。
            if "." in number or len(number) <= 2:
                return 1 + number.count("."), line, number
    return None


def enrich_chunk_metadata(chunks: Iterable[ExtractionChunk]) -> dict[str, int]:
    """就地补全 chunk 元数据并返回计数（供 profile 展示）。

    ``section_id`` 只在文档确实出现标题结构时派生：无标题的平文文档保持
    ``section_id=None``，避免向 AC-8「带结构元数据必须绑定实体」注入假结构。
    """
    resolved = list(chunks)
    counts = {
        "section_ids_derived": 0,
        "clause_numbers_derived": 0,
        "documents_defaulted": 0,
    }
    headings = [_heading_of(chunk.text) for chunk in resolved]

    # Pass 1：确定每个 chunk 的文档归属（显式 document_id 优先，缺失时补默认号），
    # 同时记录哪些文档真正带标题结构。
    doc_keys: list[str] = []
    doc_has_structure: dict[str, bool] = {}
    current_doc = ""
    default_doc_serial = 0
    previous_start: int | None = None
    for index, chunk in enumerate(resolved):
        document_id = (chunk.document_id or "").strip()
        start_offset = chunk.start_offset
        offset_regression = (
            previous_start is not None
            and start_offset is not None
            and start_offset < previous_start
        )
        if not document_id:
            if not current_doc or offset_regression:
                default_doc_serial += 1
                current_doc = f"document-{default_doc_serial:04d}"
                counts["documents_defaulted"] += 1
            chunk.document_id = current_doc
        elif document_id != current_doc:
            current_doc = document_id
        doc_keys.append(current_doc)
        if headings[index] is not None:
            doc_has_structure[current_doc] = True
        if start_offset is not None:
            previous_start = start_offset

    # Pass 2：按文档维护标题栈，派生 section/clause。
    current_section_id = ""
    current_section_title = ""
    section_stack: list[tuple[int, str]] = []
    section_serial = 0
    active_doc = ""
    for index, chunk in enumerate(resolved):
        doc_key = doc_keys[index]
        if doc_key != active_doc:
            active_doc = doc_key
            current_section_id = ""
            current_section_title = ""
            section_stack = []
            section_serial = 0

        heading = headings[index]
        if heading is not None:
            level, title, clause = heading
            while section_stack and section_stack[-1][0] >= level:
                section_stack.pop()
            section_stack.append((level, title))
            section_serial += 1
            current_section_id = f"{doc_key}::s{section_serial:03d}"
            current_section_title = title
            if clause and not chunk.clause_number:
                chunk.clause_number = clause
                counts["clause_numbers_derived"] += 1

        if doc_has_structure.get(doc_key) and not chunk.section_id:
            # 首个标题之前的导语归属 s000；无标题文档不注入假结构。
            chunk.section_id = current_section_id or f"{doc_key}::s000"
            counts["section_ids_derived"] += 1
        if not chunk.section_title and current_section_title:
            chunk.section_title = current_section_title

    return counts


__all__ = ["enrich_chunk_metadata"]
