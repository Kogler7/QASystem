"""Mechanical original-text anchoring. No name, keyword or domain filters."""
from __future__ import annotations

import json
import math
import re
from typing import Any

from ..v2.chunk_context import ChunkContextManager
from .citations import span_anchor
from .schema_types import is_json_value, map_value_type


def locate(value: Any, context: ChunkContextManager, default_chunk: str) -> list[dict[str, Any]]:
    """Anchor each cited item independently and keep only the located ones.

    A claim is supported by the citations that really resolve in the original
    text. One invented, malformed or stale citation must not discard its valid
    siblings, and a claim whose citations all fail to resolve still yields
    nothing. Every surviving anchor is verified exactly as before.
    """
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not value:
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if "span_id" in item:
            span = span_anchor(item["span_id"], context)
            if span is None:
                continue
            source = span["source_block_ids"][0]
            # Redundant caller-supplied fields may confirm the selected source,
            # but may never overwrite or contradict the authoritative anchor.
            if any(key in item and (item[key] != span[key] or (
                    key in {"start_char", "end_char"} and type(item[key]) is not int))
                   for key in ("quote", "source_block_ids", "start_char", "end_char")):
                continue
            if any(key in item and item[key] != source for key in ("chunk_id", "source_chunk_id")):
                continue
            chunk = context.get_chunk(source)
            if any(key in item and item[key] != getattr(chunk, key)
                   for key in ("document_id", "section_id", "section_title", "page_no")):
                continue
            item = {**item, **span, "chunk_id": source}
        declared = item.get("source_block_ids")
        chunk_id = item.get("chunk_id") if item.get("chunk_id") is not None else item.get("source_chunk_id")
        if chunk_id is None:
            # Accepted internal anchors carry their original source in this
            # canonical field. Reusing a cross-chunk anchor must not relabel it
            # with the currently repaired chunk.
            chunk_id = declared[0] if isinstance(declared, list) and len(declared) == 1 else default_chunk
        if declared is not None and declared != [chunk_id]:
            continue
        quote = item.get("quote")
        if not isinstance(chunk_id, str) or not context.has_chunk(chunk_id) or not isinstance(quote, str) or not quote.strip():
            continue
        chunk = context.get_chunk(chunk_id)
        start, end = item.get("start_char"), item.get("end_char")
        if start is None and end is None:
            start = chunk.text.find(quote)
            end = start + len(quote)
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(chunk.text) or chunk.text[start:end] != quote:
            continue
        anchored = {"quote": quote, "source_block_ids": [chunk_id], "start_char": start, "end_char": end}
        for key in ("document_id", "section_id", "section_title", "page_no"):
            if getattr(chunk, key) is not None:
                anchored[key] = getattr(chunk, key)
        result.append(anchored)
    return unique(result)


def unique(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list({json.dumps(value, ensure_ascii=False, sort_keys=True): value for value in values}.values())


def source_ids(evidence: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(source for item in evidence for source in item.get("source_block_ids", [])))


def typed(value: Any, datatype: str) -> bool:
    """Only JSON type checks, without extracting numbers or interpreting words."""
    if value is None:
        return True
    datatype = re.sub(r"\s+", "", datatype).lower().rstrip("?")
    if datatype.endswith("[]"):
        return isinstance(value, list) and all(typed(item, datatype[:-2]) for item in value)
    if datatype.startswith("list<") and datatype.endswith(">"):
        return isinstance(value, list) and all(typed(item, datatype[5:-1]) for item in value)
    mapping_value_type = map_value_type(datatype)
    if mapping_value_type is not None:
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            return False
        if mapping_value_type:
            return all(typed(item, mapping_value_type) for item in value.values())
        return is_json_value(value)
    if datatype in {"int", "integer", "long"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if datatype in {"float", "double", "decimal", "number"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if datatype in {"bool", "boolean"}:
        return isinstance(value, bool)
    if datatype in {"str", "string", "date", "datetime", "time"}:
        return isinstance(value, str)
    # Unknown scalar/enumeration meaning is judged semantically from the ontology.
    return isinstance(value, (str, int, float, bool)) and not (isinstance(value, float) and not math.isfinite(value))
