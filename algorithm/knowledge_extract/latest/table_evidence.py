"""Provenance gate for rule candidates produced from structured tables.

The table classifier decides *which* rows are rule-like.  This module decides
whether a candidate's already-normalized evidence actually belongs to one of
those rows.  It deliberately parses the source text in place; serializing the
decoded JSON would destroy the offsets needed for this check.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .structured_text import _is_record_payload


@dataclass
class _Node:
    start: int
    end: int
    kind: str
    value: Any = None
    members: list[tuple["_Node", "_Node"]] = field(default_factory=list)
    items: list["_Node"] = field(default_factory=list)


def _skip(text: str, pos: int, end: int) -> int:
    while pos < end and text[pos].isspace():
        pos += 1
    return pos


def _parse_node(text: str, pos: int, limit: int) -> _Node | None:
    pos = _skip(text, pos, limit)
    if pos >= limit:
        return None
    decoder = json.JSONDecoder()
    if text[pos] == "{":
        node = _Node(pos, pos + 1, "object", members=[])
        cursor = _skip(text, pos + 1, limit)
        if cursor < limit and text[cursor] == "}":
            node.end = cursor + 1
            return node
        while cursor < limit:
            cursor = _skip(text, cursor, limit)
            if cursor >= limit or text[cursor] != '"':
                return None
            try:
                key, key_end = decoder.raw_decode(text, cursor)
            except (ValueError, json.JSONDecodeError):
                return None
            cursor = _skip(text, key_end, limit)
            if cursor >= limit or text[cursor] != ":":
                return None
            value = _parse_node(text, cursor + 1, limit)
            if value is None:
                return None
            # Key ranges are retained for the structural node, although the
            # gate only admits value ranges and therefore excludes headers.
            key_node = _Node(pos, key_end, "key", key)
            node.members.append((key_node, value))
            cursor = _skip(text, value.end, limit)
            if cursor < limit and text[cursor] == "}":
                node.end = cursor + 1
                return node
            if cursor >= limit or text[cursor] != ",":
                return None
            cursor += 1
        return None
    if text[pos] == "[":
        node = _Node(pos, pos + 1, "array", items=[])
        cursor = _skip(text, pos + 1, limit)
        if cursor < limit and text[cursor] == "]":
            node.end = cursor + 1
            return node
        while cursor < limit:
            item = _parse_node(text, cursor, limit)
            if item is None:
                return None
            node.items.append(item)
            cursor = _skip(text, item.end, limit)
            if cursor < limit and text[cursor] == "]":
                node.end = cursor + 1
                return node
            if cursor >= limit or text[cursor] != ",":
                return None
            cursor += 1
        return None
    try:
        value, end = decoder.raw_decode(text, pos)
    except (ValueError, json.JSONDecodeError):
        return None
    return _Node(pos, end, "scalar", value)


def _fenced_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    lines = list(re.finditer(r"(?m)^\s*```\s*(?P<lang>[A-Za-z0-9_+-]*)\s*$", text))
    index = 0
    while index + 1 < len(lines):
        opening, closing = lines[index], lines[index + 1]
        lang = opening.group("lang").lower()
        if lang in {"", "json"}:
            ranges.append((opening.end(), closing.start()))
        index += 2
    if len(lines) % 2:
        opening = lines[-1]
        if opening.group("lang").lower() in {"", "json"}:
            ranges.append((opening.end(), len(text)))
    return ranges


def _source_roots(text: str) -> list[_Node]:
    roots: list[_Node] = []
    ranges = _fenced_ranges(text)
    if ranges:
        for start, end in ranges:
            node = _parse_node(text, start, end)
            if node is not None and not text[node.end:end].strip() and _is_record_payload(json.loads(text[node.start:node.end])):
                roots.append(node)
        if roots:
            return roots
    # Match structured_text's batch heading tolerance while preserving source
    # offsets. A historical splitter may leave a lone trailing fence marker.
    stripped_start = 0
    while stripped_start < len(text):
        line_end = text.find("\n", stripped_start)
        line_end = len(text) if line_end < 0 else line_end + 1
        line = text[stripped_start:line_end]
        if line.strip() and not re.match(r"^\s*#{1,6}\s*文件\s*[:：].*$", line):
            break
        stripped_start = line_end
    end = len(text)
    trailing = re.search(r"\n\s*```\s*$", text)
    if trailing:
        end = trailing.start()
    node = _parse_node(text, stripped_start, end)
    if node is not None and text[node.end:end].strip() == "":
        roots.append(node)
    return roots


def _row_nodes(root: _Node) -> list[_Node] | None:
    if root.kind == "array":
        return root.items
    if root.kind != "object":
        return None
    for key, value in root.members:
        if key.value == "rows" and value.kind == "array":
            return value.items
    return [root]


def _value_ranges(row: _Node) -> list[tuple[int, int]]:
    if row.kind == "object":
        return [(value.start, value.end) for _, value in row.members]
    if row.kind == "array":
        return [(item.start, item.end) for item in row.items]
    return [(row.start, row.end)]


def _get(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _evidence_items(entity: Any) -> list[Any]:
    raw = _get(entity, "evidence")
    if raw is None:
        raw = _get(entity, "evidences")
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [raw] if raw is not None else []


def _first_evidence(entity: Any) -> Any:
    items = _evidence_items(entity)
    return items[0] if items else None


def _metadata(chunk: Any) -> dict[str, Any]:
    value = _get(chunk, "metadata", {})
    return value if isinstance(value, dict) else {}


def _row_for_span(root: _Node, start: int, end: int) -> tuple[int, _Node] | None:
    for index, row in enumerate(_row_nodes(root) or (), start=1):
        if not (row.start <= start and end <= row.end):
            continue
        if any(start < value_end and end > value_start for value_start, value_end in _value_ranges(row)):
            return index, row
    return None


def _occurrence_span(text: str, row: _Node, evidence: Any, entity: Any) -> tuple[int, int] | None:
    quote = str(_get(evidence, "quote", "") or "")
    if not quote:
        return None
    mention = str(_get(entity, "mention", "") or _get(entity, "name", "") or "")
    row_text = text[row.start:row.end]
    candidates: list[tuple[int, int]] = []
    cursor = 0
    while True:
        found = row_text.find(quote, cursor)
        if found < 0:
            break
        candidates.append((row.start + found, row.start + found + len(quote)))
        cursor = found + 1
    if not candidates:
        return None
    start = _get(evidence, "start_char")
    end = _get(evidence, "end_char")
    anchored = [item for item in candidates if isinstance(start, int) and isinstance(end, int) and item[0] <= start and end <= item[1]]
    if anchored:
        return anchored[0]
    if mention:
        for candidate_start, candidate_end in candidates:
            if mention in text[candidate_start:candidate_end]:
                return candidate_start, candidate_end
    return candidates[0]


def table_source_span(source_chunk: Any, original_chunk: Any, entity: Any) -> tuple[int, int] | None:
    """Map normalized table evidence back to an original row span.

    Mapping is offset-first. For reconstructed row windows, metadata supplies
    the original block and row-window start; each candidate row is then
    validated by decoded JSON equality before its original offsets are used.
    """
    source_text = str(_get(source_chunk, "text", "") or "")
    original_text = str(_get(original_chunk, "text", "") or "")
    evidence = _first_evidence(entity)
    start = _get(evidence, "start_char") if evidence is not None else None
    end = _get(evidence, "end_char") if evidence is not None else None
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
        return None
    source_roots = _source_roots(source_text)
    if not source_roots:
        return None
    source_row: tuple[int, _Node] | None = None
    source_block = None
    for index, root in enumerate(source_roots, start=1):
        found = _row_for_span(root, start, end)
        if found is not None:
            source_block, source_row = index, found
            break
    if source_row is None or source_block is None:
        return None
    local_row_index, source_node = source_row
    if source_text == original_text:
        occurrence = _occurrence_span(source_text, source_node, evidence, entity)
        return occurrence or (source_node.start, source_node.end)

    metadata = _metadata(source_chunk)
    block_index = metadata.get("table_source_block_index", source_block)
    row_start = metadata.get("table_data_row_start", 1)
    if not isinstance(block_index, int) or not isinstance(row_start, int) or block_index < 1 or row_start < 1:
        return None
    original_roots = _source_roots(original_text)
    if block_index > len(original_roots):
        return None
    original_rows = _row_nodes(original_roots[block_index - 1]) or ()
    original_index = row_start + local_row_index - 1
    if original_index > len(original_rows):
        return None
    original_node = original_rows[original_index - 1]
    try:
        source_value = json.loads(source_text[source_node.start:source_node.end])
        original_value = json.loads(original_text[original_node.start:original_node.end])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if source_value != original_value:
        return None
    return original_node.start, original_node.end


def rule_candidate_in_scope(chunk: Any, decision: Any, entity: Any) -> bool:
    """Return whether a rule candidate's evidence is permitted by table rows.

    Evidence outside every parsed JSON table is valid prose evidence. Evidence
    intersecting a table must be wholly inside a selected row and touch a value, which
    excludes column names and the table header while remaining safe for
    duplicate cell values.
    """
    rule_types = tuple(getattr(decision, "rule_types", ()) or ())
    candidate_type = _get(entity, "canonical_type") or _get(entity, "proposed_type") or _get(entity, "type")
    if candidate_type not in rule_types:
        return True
    text = str(_get(chunk, "text", "") or "")
    if "source.not_table" in tuple(getattr(decision, "reasons", ()) or ()):
        return True
    roots = _source_roots(text)
    if not roots:
        # A table decision without parser support is unsafe for rule output.
        return False
    selected = {tuple(pair) for pair in (getattr(decision, "rule_rows", ()) or ())}
    rows: list[tuple[int, int, _Node]] = []
    table_ranges: list[tuple[int, int]] = []
    for table_no, root in enumerate(roots, start=1):
        table_ranges.append((root.start, root.end))
        for row_no, row in enumerate(_row_nodes(root) or (), start=1):
            if (table_no, row_no) in selected:
                rows.append((table_no, row_no, row))
    if not table_ranges:
        return False
    evidences = _evidence_items(entity)
    if not evidences:
        return False
    for evidence in evidences:
        start = _get(evidence, "start_char")
        end = _get(evidence, "end_char")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start or end > len(text):
            return False
        overlaps_table = any(start < right and end > left for left, right in table_ranges)
        if not overlaps_table:
            continue
        if not any(
            row.start <= start and end <= row.end
            # A model may quote a complete JSON record. Permit that span when
            # it intersects a real cell, while a pure key/header span remains
            # excluded.
            and any(start < value_end and end > value_start for value_start, value_end in _value_ranges(row))
            for _, _, row in rows
        ):
            return False
    return True


__all__ = ["rule_candidate_in_scope", "table_source_span"]
