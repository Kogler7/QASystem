"""Table-aware windows made exclusively of exact source slices.

Headers travel as bounded source context, never as invented row fields. Evidence
coordinates continue to refer to the original upstream chunk.
"""
from __future__ import annotations

from dataclasses import replace
import json
import re

_FENCE = re.compile(r'(?m)^[ \t]*```(?:json|jsonl)?[ \t]*\n(.*?)^[ \t]*```[ \t]*$', re.S)
_WS = re.compile(r'\s*')


def source_parts(chunk):
    """Expand a private table window into independently locatable source slices."""
    context = chunk.metadata.get('_v31_table_context', ())
    if not context:
        return [chunk]
    metadata = {k: v for k, v in chunk.metadata.items() if k != '_v31_table_context'}
    parts = [replace(chunk, metadata=metadata)]
    for offset, text in context:
        parts.append(replace(chunk, text=text, metadata={**metadata,
            '_v31_window_start': offset, '_v31_table_header': True}))
    return sorted(parts, key=lambda p: p.metadata.get('_v31_window_start', 0))


def _json_rows(text, start, end):
    decoder = json.JSONDecoder()
    start = _WS.match(text, start).end()
    try:
        value, parsed_end = decoder.raw_decode(text, start)
        if text[parsed_end:end].strip():
            # JSONL: each entire object is one independently interpretable row.
            spans, pos = [], start
            while pos < end:
                row, stop = decoder.raw_decode(text, pos)
                if not isinstance(row, dict):
                    return None
                spans.append((pos, stop))
                pos = _WS.match(text, stop).end()
            return spans
        if isinstance(value, dict) and isinstance(value.get('rows'), list):
            pos = start + 1
            while pos < parsed_end:
                pos = _WS.match(text, pos).end()
                key, pos = decoder.raw_decode(text, pos)
                pos = _WS.match(text, pos).end()
                if text[pos] != ':':
                    return None
                pos = _WS.match(text, pos + 1).end()
                if key == 'rows':
                    break
                _, pos = decoder.raw_decode(text, pos)
                pos = _WS.match(text, pos).end() + 1
        elif isinstance(value, dict):
            return [(start, parsed_end)]
        elif isinstance(value, list) and all(isinstance(row, dict) for row in value):
            pos = start
        else:
            return None
        if text[pos] != '[':
            return None
        spans = []
        pos += 1
        while pos < parsed_end:
            pos = _WS.match(text, pos).end()
            if text[pos] == ']':
                break
            _, stop = decoder.raw_decode(text, pos)
            spans.append((pos, stop))
            pos = _WS.match(text, stop).end()
            if text[pos] == ',':
                pos += 1
        return spans or None
    except (ValueError, IndexError, RecursionError):
        return None


def table_windows(chunk, limit):
    """Return exact, whole-row windows or None for ordinary text.

    An oversized single row remains atomic. The caller must fail its input
    budget explicitly rather than strip a column or split a cell silently.
    """
    if chunk.metadata.get('_v31_atomic_table'):
        return [chunk]
    text = chunk.text
    base = int(chunk.metadata.get('_v31_window_start', 0))
    saved_rows = chunk.metadata.get('_v31_table_rows')
    if saved_rows:
        rows = [(left - base, right - base) for left, right in saved_rows]
        return _pack_rows(chunk, rows, chunk.metadata.get('_v31_table_context', []), limit)
    fences = list(_FENCE.finditer(text))
    regions = [(m.start(1), m.end(1), m.start(), m.end()) for m in fences] or [(0, len(text), 0, len(text))]
    if not fences:
        # Frozen upstream chunks can split the opening/header from the rows.
        # A lone closing/opening fence is syntax, not a reason to cut JSON cells.
        opening = re.search(r'(?m)^[ \t]*```(?:json|jsonl)[ \t]*\n', text)
        closing = re.search(r'\n[ \t]*```[ \t]*$', text)
        # An upstream chunk may retain a sheet heading plus the opening fence,
        # with the closing fence in a later chunk. Keep the heading separately
        # and recognize only complete rows; never repair source JSON or offsets.
        regions = [(opening.end() if opening else 0, closing.start() if closing else len(text),
                    opening.start() if opening else 0, len(text))]
    tables = []
    for start, end, outer_start, outer_end in regions:
        rows = _json_rows(text, start, end)
        if rows:
            tables.append((outer_start, outer_end, rows))
    if not tables:
        # Markdown tables also repeat their exact header and keep complete rows.
        lines = list(re.finditer(r'.*(?:\n|$)', text))
        i = 1
        while i < len(lines):
            if ('|' in lines[i - 1].group() and '|' in lines[i].group()
                    and re.fullmatch(r'[ \t|:\-]+\n?', lines[i].group())
                    and '-' in lines[i].group()):
                start = lines[i - 1].start()
                rows = []
                i += 1
                while i < len(lines) and '|' in lines[i].group():
                    rows.append((lines[i].start(), lines[i].end()))
                    i += 1
                if rows:
                    tables.append((start, rows[-1][1], rows))
            else:
                i += 1
        if not tables:
            return None
    parts = []
    consumed = 0
    for start, end, rows in tables:
        # Only this table's own header/footer accompany its rows. Ordinary text
        # between tables stays a separate source, avoiding cross-table leakage.
        if text[consumed:start].strip():
            parts.append(replace(chunk, text=text[consumed:start], metadata={
                **chunk.metadata, '_v31_window_start': base + consumed}))
        header = text[start:rows[0][0]]
        footer = text[rows[-1][1]:end]
        context = [(base + start, header), (base + rows[-1][1], footer)]
        context = [(offset, content) for offset, content in context if content.strip()]
        parts.extend(_pack_rows(chunk, rows, context, limit))
        consumed = end
    if text[consumed:].strip():
        parts.append(replace(chunk, text=text[consumed:], metadata={
            **chunk.metadata, '_v31_window_start': base + consumed}))
    return parts


def _pack_rows(chunk, rows, context, limit):
    base = int(chunk.metadata.get('_v31_window_start', 0))
    overhead = sum(len(text) for _, text in context)
    packets, packet = [], []
    for row in rows:
        if packet and row[1] - packet[0][0] + overhead > limit:
            packets.append(packet)
            packet = []
        packet.append(row)
    if packet:
        packets.append(packet)
    return [replace(chunk, text=chunk.text[p[0][0]:p[-1][1]], metadata={**chunk.metadata,
        '_v31_window_start': base + p[0][0], '_v31_table_context': context,
        '_v31_table_rows': [(base + left, base + right) for left, right in p],
        '_v31_atomic_table': len(p) == 1}) for p in packets]
