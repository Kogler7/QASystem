"""Ontology-gated entities derived from explicitly numbered Markdown headings.

This is deliberately a small structural supplement to the LLM entity pass.  It
only consumes heading lines whose visible text starts with a section number;
ordinary prose, table-of-contents rows, and unnumbered headings are left to
the normal extractor.
"""
from __future__ import annotations

import re
from typing import Any

from .candidate_models import EntityCandidate
from .chunk_context import ChunkContextManager, ExtractionChunk
from .evidence import Evidence

_HEADING = re.compile(r"^(?P<indent> {0,3})(?P<marks>#{1,6})[ \t]+(?P<body>.*?)[ \t]*$")
_NUMBER = re.compile(
    r"^(?P<number>(?:第\s*)?\d+(?:\.\d+)*(?:[.)、：:]?)?)(?P<rest>(?:\s+|$).*)$"
)
_MARKDOWN_EDGE = re.compile(r"(?:\*\*|__|~~|`)+")
_CJK_TRANSLATION = re.compile(
    r"(?P<primary>.*[\u3400-\u9fff])\s+(?P<suffix>[A-Za-z]+(?:[ \t]+[A-Za-z]+)+)$"
)


def _heading_types(schema: Any, allowed_types: set[str]) -> tuple[str, str] | None:
    signatures = tuple(schema.resolve_relation("hasSubClause"))
    if not signatures:
        return None
    sources = {item.source for item in signatures}
    targets = {item.target for item in signatures}
    top = sources - targets
    if len(top) != 1:
        return None
    parent_type = next(iter(top))
    self_targets = {item.target for item in signatures if item.source == item.target}
    nested = self_targets or (targets - top)
    if len(nested) != 1:
        return None
    child_type = next(iter(nested))
    if parent_type not in allowed_types or child_type not in allowed_types:
        return None
    return parent_type, child_type


def _clean_heading_body(body: str) -> str:
    # Remove only Markdown decoration, retaining the numbered title text and
    # all ordinary punctuation/whitespace inside it.
    cleaned = _MARKDOWN_EDGE.sub("", body).strip()
    return cleaned


def _normalize_heading_title(title: str) -> str:
    """Keep a heading's number and primary title, omitting a clear translation.

    A translation is only inferred when the suffix is a whitespace-separated
    phrase made entirely of Latin words and the primary part contains CJK
    text.  This intentionally leaves model numbers, units, acronyms, and
    ambiguous mixed-script titles untouched.
    """
    number = re.match(r"(?P<prefix>(?:第\s*)?\d+(?:\.\d+)*(?:[.)、：:]?)?)(?P<rest>.*)$", title)
    if number is None:
        return title
    rest = number.group("rest").strip()
    match = _CJK_TRANSLATION.fullmatch(rest)
    if match is None:
        return title
    return f"{number.group('prefix')} {match.group('primary').strip()}"


def _numbered_heading(chunk: ExtractionChunk, line_start: int, line: str) -> tuple[str, int, int] | None:
    match = _HEADING.match(line)
    if match is None:
        return None
    body = match.group("body")
    number = _NUMBER.match(body)
    if number is None:
        return None
    title = _clean_heading_body(body)
    if not title:
        return None
    number_text = re.sub(r"\s+", "", number.group("number"))
    number_text = number_text.rstrip(".)、：:")
    if not number_text or not re.fullmatch(r"(?:第)?\d+(?:\.\d+)*", number_text):
        return None
    # Evidence covers the exact source line.  The entity's name starts at the
    # first visible title character after Markdown heading markers.
    content_start = line_start + match.start("body")
    content_end = line_start + len(line.rstrip(" \t"))
    return title, content_start, content_end


def _heading_properties(schema: Any, type_id: str, name: str, *, top_level: bool) -> dict[str, Any]:
    match = re.match(r"(?:第\s*)?(?P<number>\d+(?:\.\d+)*)[.)、：:]?\s+(?P<title>.+)$", name)
    if match is None:
        return {}
    number_symbol = "章编号" if top_level else "条编号"
    title_symbol = "章标题" if top_level else "条标题"
    result: dict[str, Any] = {}
    for symbol, value in (
        (number_symbol, match.group("number")),
        (title_symbol, match.group("title").strip()),
    ):
        prop = schema.resolve_property(type_id, symbol)
        if prop is not None:
            result[prop.identifier] = value
    return result


def add_structural_heading_entities(
    pool: Any,
    manager: ChunkContextManager,
    *,
    allowed_types: set[str] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> int:
    """Add numbered heading entities to ``pool`` after the LLM entity pass.

    Type assignment is abstention-first: it requires a unique top-level and a
    unique nested endpoint implied by the ontology's ``hasSubClause``
    signatures, and both types must be in the extraction scope.
    """
    scope = set(allowed_types or pool.allowed_types)
    resolved_types = _heading_types(pool.schema, scope)
    if resolved_types is None:
        return 0
    parent_type, child_type = resolved_types
    added = 0
    for chunk in manager:
        text = chunk.text or ""
        offset = 0
        for line in text.splitlines(keepends=True):
            source_line = line.rstrip("\r\n")
            found = _numbered_heading(chunk, offset, source_line)
            if found is not None:
                title, start, end = found
                display_title = _normalize_heading_title(title)
                digits = re.match(r"(?:第\s*)?\d+(?:\.\d+)*", title)
                depth = len(re.sub(r"\s+", "", digits.group(0)).removeprefix("第").split(".")) if digits else 0
                type_id = parent_type if depth == 1 else child_type if depth >= 2 else ""
                if type_id:
                    properties = _heading_properties(
                        pool.schema,
                        type_id,
                        display_title,
                        top_level=depth == 1,
                    )
                    candidate = EntityCandidate(
                        candidate_id="",
                        proposed_type=type_id,
                        canonical_type=type_id,
                        name=display_title,
                        properties=properties,
                        evidence=[Evidence(
                            span_id=f"{chunk.chunk_id}:{start}:{end}",
                            source_block_ids=[chunk.chunk_id],
                            quote=source_line,
                            start_char=start,
                            end_char=end,
                            evidence_type="structural",
                        )],
                        source="markdown_structure",
                    )
                    before_count = pool.entity_candidate_count
                    if pool.add_entity(candidate, warnings=warnings) is not None and (
                        pool.entity_candidate_count > before_count
                    ):
                        added += 1
            offset += len(line)
    return added


__all__ = ["add_structural_heading_entities"]
