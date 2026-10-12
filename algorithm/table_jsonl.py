"""Lossless row boundaries for parsed record tables (no LLM involved)."""
from __future__ import annotations

import json
import re


JSONL_FENCE = re.compile(r'(?m)^[ \t]*```jsonl[ \t]*\r?\n.*?^[ \t]*```[ \t]*(?:\r?\n|$)', re.DOTALL | re.IGNORECASE)


def decode_records(body: str) -> list[dict] | None:
    """Strict JSONL: each nonblank physical line is a complete object."""
    records = []
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            return None
        if not isinstance(record, dict):
            return None
        records.append(record)
    return records or None


def row_boundaries(text: str) -> list[int] | None:
    """Offsets between records; keep original headings/fence edges in outer rows.

    Accept complete and split JSONL fences. Ordinary multiline JSON is rejected.
    """
    starts = []
    offset = 0
    finished = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            pass
        elif stripped.lower() == '```jsonl' and not starts:
            pass
        elif stripped == '```' and starts:
            finished = True
        elif stripped.startswith('#') and not starts:
            pass
        else:
            if finished or decode_records(line) is None:
                return None
            starts.append(offset)
        offset += len(line)
    return [0, *starts[1:], len(text)] if starts else None


def pack_ranges(text: str, size: int, overlap: int = 0, max_rows: int = 20) -> list[list[int]]:
    """Pack complete records; oversized individual records remain atomic.

    Overlap repeats only complete records, never more than the character budget.
    A small overlap can therefore be zero. Every step includes a new record.
    """
    boundaries = row_boundaries(text)
    if boundaries is None:
        raise ValueError('not a JSONL record table')
    ranges = []
    first = 0
    count = len(boundaries) - 1
    while first < count:
        last = first + 1
        while last < count and last - first < max_rows and boundaries[last + 1] - boundaries[first] <= size:
            last += 1
        ranges.append([boundaries[first], boundaries[last]])
        if last == count:
            break
        next_first = last
        while next_first > first + 1 and boundaries[last] - boundaries[next_first - 1] <= overlap:
            next_first -= 1
        first = next_first
    return ranges
