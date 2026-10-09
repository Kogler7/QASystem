"""Select immutable source lines without rewriting or interpreting their text."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from ..v2.chunk_context import ChunkContextManager


def source_spans(chunk_id: str, text: str) -> list[dict[str, str]]:
    """Keep every original character, including whitespace and line endings.

    Line numbers are one-based. A trailing newline belongs to its preceding
    line; no synthetic extra line is appended. Empty/blank lines remain in the
    rendered source, although blank lines alone cannot support a fact.
    """
    return [{"span_id": f"{chunk_id}:L{number}", "text": line}
            for number, line in enumerate(text.splitlines(keepends=True), start=1)]


def render_sources(sources: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Replace each raw text field with lossless source-line choices once.

    This changes only the input citation interface. It does not select facts,
    classify lines, join separate lines, or truncate long source text. Callers
    continue to keep the original ChunkContextManager as the authority.
    """
    rendered = []
    for source in sources:
        chunk_id, text = source.get("chunk_id"), source.get("text")
        if not isinstance(chunk_id, str) or not chunk_id or not isinstance(text, str):
            raise ValueError("Sources require a nonempty chunk_id and original text")
        item = {key: deepcopy(value) for key, value in source.items() if key not in {"text", "spans"}}
        item["spans"] = source_spans(chunk_id, text)
        rendered.append(item)
    return rendered


def span_anchor(span_id: Any, context: ChunkContextManager) -> dict[str, Any] | None:
    """Resolve an exact canonical line ID against original source text."""
    if not isinstance(span_id, str):
        return None
    chunk_id, delimiter, number = span_id.rpartition(":L")
    if (not delimiter or not number or not number.isascii() or not number.isdecimal()
            or number.startswith("0") or not context.has_chunk(chunk_id)):
        return None
    try:
        line_number = int(number)
    except ValueError:
        return None
    chunk = context.get_chunk(chunk_id)
    offset = 0
    for index, line in enumerate(chunk.text.splitlines(keepends=True), start=1):
        end = offset + len(line)
        if index == line_number:
            if not line.strip():
                return None
            return {"quote": line, "source_block_ids": [chunk_id], "start_char": offset, "end_char": end}
        offset = end
    return None
