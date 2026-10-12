"""Offline adaptive chunk planning; deliberately not connected to runtime.

Classifier calls see baseline-sized, non-overlapping probes, never the adaptive
chunks produced from their decisions. Sizes are character budgets, not tokens.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from typing import Any

from algorithm.knowledge_pipeline.chunking import chunk_document_items

CONFIDENCE_THRESHOLD = 0.6


def _pipe_table_spans(content: str) -> list[tuple[int, int]]:
    """Protect Markdown header/delimiter/body runs, with an adjacent heading.

    A pipe alone is insufficient: require a header followed by a valid Markdown
    separator row. Leave fenced code to the production splitter's protection.
    """
    lines = content.splitlines(keepends=True)
    starts: list[int] = []
    position = 0
    fenced: list[bool] = []
    fence: str | None = None
    for line in lines:
        starts.append(position)
        position += len(line)
        stripped = line.lstrip()
        marks = re.match(r"(`{3,}|~{3,})", stripped)
        fenced.append(fence is not None or marks is not None)
        if marks:
            mark = marks.group(1)
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence):
                fence = None

    def cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    spans: list[tuple[int, int]] = []
    index = 0
    while index + 1 < len(lines):
        delimiter = cells(lines[index + 1])
        if (fenced[index] or fenced[index + 1] or "|" not in lines[index]
                or "|" not in lines[index + 1]
                or len(cells(lines[index])) != len(delimiter)
                or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in delimiter)):
            index += 1
            continue
        first = index
        heading = index - 1
        while heading >= 0 and not lines[heading].strip():
            heading -= 1
        if heading >= 0 and not fenced[heading] and re.match(r"\s*#{1,6}\s", lines[heading]):
            first = heading
        stop = index + 2
        while stop < len(lines) and not fenced[stop] and lines[stop].strip() and "|" in lines[stop]:
            stop += 1
        spans.append((starts[first], starts[stop] if stop < len(lines) else len(content)))
        index = stop
    return spans


def _protected(text: str) -> bool:
    # Upstream already isolates fences (including their immediately preceding
    # heading) and JSON. Classify neither as prose nor split them to meet size.
    if any(line.lstrip().startswith(("```", "~~~")) for line in text.splitlines()):
        return True
    try:
        return isinstance(json.loads(text), (dict, list))
    except (ValueError, TypeError):
        return False


def _split(content: str, size: int, overlap: int = 0) -> list[dict]:
    """Reuse source-aware splitter and recover its omitted whitespace gaps."""
    parts: list[dict] = []
    cursor = 0
    spans = _pipe_table_spans(content)
    for start, end in spans + [(len(content), len(content))]:
        if cursor < start:
            for part in chunk_document_items(content[cursor:start], chunk_type=1, chunk_size=size,
                                             chunk_overlap=min(overlap, size - 1)):
                left, right = part["range"]
                parts.append({"text": part["text"], "range": [cursor + left, cursor + right]})
        if start < end:
            parts.append({"text": content[start:end], "range": [start, end],
                          "protected_kind": "pipe_table"})
        cursor = end
    restored: list[dict] = []
    cursor = 0
    def recover_gap(start: int, end: int) -> None:
        # Upstream intentionally drops whitespace-only spans. Keep even a large
        # blank document bounded without sending an oversized classifier probe.
        if content[start:end].strip():
            raise ValueError("splitter omitted non-whitespace source text")
        for left in range(start, end, size):
            right = min(left + size, end)
            restored.append({"text": content[left:right], "range": [left, right]})
    for part in parts:
        start, end = part["range"]
        if content[start:end] != part["text"]:
            raise ValueError("splitter returned an invalid source range")
        if start > cursor:
            recover_gap(cursor, start)
        restored.append(part)
        cursor = max(cursor, end)
    if cursor < len(content):
        recover_gap(cursor, len(content))
    return restored


def _decision(classify: Callable, text: str) -> tuple[str, str | None]:
    try:
        answer = classify(text)
        if isinstance(answer, str):
            label = answer
        elif isinstance(answer, dict):
            label = answer.get("label", answer.get("density", answer.get("class")))
            confidence = float(answer.get("confidence", 0))
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                return "medium", "invalid_confidence"
            if confidence < CONFIDENCE_THRESHOLD:
                return "medium", "low_confidence"
        else:
            return "medium", "invalid_response"
        if isinstance(label, str) and label.lower().strip() in {"low", "medium", "high"}:
            return label.lower().strip(), None
        return "medium", "invalid_label"
    except Exception as exc:
        # Only exception type is retained: classifier error messages may contain
        # request contents or credentials supplied by an external caller.
        return "medium", "classifier_error:" + type(exc).__name__


def plan_adaptive(
    content: str,
    classify: Callable[[str], str | dict[str, Any]],
    base_size: int = 1200,
    overlap: int = 50,
) -> list[dict[str, Any]]:
    """Return lossless source windows with low/medium/high size annotations.

    The classifier receives each baseline prose probe once. A returned label is
    accepted directly; dict results require confidence >= 0.6. Invalid/uncertain
    responses fall back to the base budget. Adjacent probes of the same accepted
    density are grouped before splitting at 2x/1x/0.5x baseline. Protected blocks
    bypass classification and remain whole, including oversized table JSON.

    Overlap is applied within each continuous same-density prose group, never
    across protected blocks or policy boundaries. Source ranges use character
    offsets [start, end). Reconstruct by source coverage, not naive concatenation
    of overlapping chunks. No model calls, filtering, files, or runtime mutation
    occur unless supplied by the caller's classify function.
    """
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    if not isinstance(base_size, int) or isinstance(base_size, bool) or base_size < 2:
        raise ValueError("base_size must be an integer >= 2")
    if not isinstance(overlap, int) or not 0 <= overlap < base_size:
        raise ValueError("overlap must satisfy 0 <= overlap < base_size")
    if not callable(classify):
        raise TypeError("classify must be callable")
    if not content:
        return []

    budgets = {"low": base_size * 2, "medium": base_size, "high": max(1, base_size // 2)}
    probes = _split(content, base_size)
    groups: list[dict] = []
    for probe in probes:
        protected = bool(probe.get("protected_kind")) or _protected(probe["text"])
        label, fallback = ("protected", None) if protected else _decision(classify, probe["text"])
        # A failed classification remains a baseline block; do not merge it into
        # a neighbouring successfully classified region and hide the fallback.
        if (groups and not protected and not fallback and not groups[-1]["protected"]
                and not groups[-1]["fallback_reason"] and groups[-1]["density"] == label
                and groups[-1]["range"][1] == probe["range"][0]):
            groups[-1]["range"][1] = probe["range"][1]
            groups[-1]["probe_ranges"].append(probe["range"])
        else:
            groups.append({"range": list(probe["range"]), "density": label,
                           "fallback_reason": fallback, "protected": protected,
                           "protected_kind": probe.get("protected_kind"),
                           "probe_ranges": [probe["range"]]})

    result: list[dict] = []
    for group in groups:
        left, right = group["range"]
        target = base_size if group["protected"] else budgets[group["density"]]
        children = ([{"text": content[left:right], "range": [0, right - left]}]
                    if group["protected"] else _split(content[left:right], target, overlap))
        for child in children:
            start, end = (left + value for value in child["range"])
            result.append({"text": content[start:end], "range": [start, end],
                           "density": group["density"], "target_size": target,
                           "probe_ranges": [list(r) for r in group["probe_ranges"]
                                            if r[0] < end and r[1] > start],
                           "fallback_reason": group["fallback_reason"],
                           "protected": group["protected"],
                           "protected_kind": group["protected_kind"],
                           "downstream_policy": ("preserve_pipe_table" if group["protected_kind"] == "pipe_table"
                                                 else "structured_row_segments" if group["protected"] else None)})
    return result
