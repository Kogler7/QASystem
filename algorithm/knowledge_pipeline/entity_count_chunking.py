"""Refine source chunks once using predicted entity-count bands."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
import re
from typing import Callable
from algorithm.table_jsonl import row_boundaries

_LOGGER = logging.getLogger(__name__)
_PARTS = {'le20': 1, '21to40': 2, 'gt40': 3}
_FENCE = re.compile(r'(?m)^\s*(?:`{3,}|~{3,})')
_TABLE_SEPARATOR = re.compile(r'^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$')


def _audit(record: dict) -> None:
    """One append per decision, including fallbacks and protected structures."""
    default = Path(__file__).resolve().parents[2] / 'backend/tmp/chunk_entity_decisions.jsonl'
    path = Path(os.getenv('CHUNK_ENTITY_LOG_PATH', str(default)))
    entry = {'timestamp': datetime.now(timezone.utc).isoformat(), 'pid': os.getpid(), **record}
    data = (json.dumps(entry, ensure_ascii=False) + '\n').encode('utf8')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            if os.write(descriptor, data) != len(data):raise OSError('incomplete audit write')
        finally:os.close(descriptor)
    except OSError:
        raise RuntimeError('cannot persist chunk entity decision audit') from None


def _structured(text: str) -> bool:
    # Conservatively preserve incomplete fences as well: no new corruption.
    if _FENCE.search(text):
        return True
    lines = text.splitlines()
    if any('|' in before and _TABLE_SEPARATOR.fullmatch(after)
           for before, after in zip(lines, lines[1:])):
        return True
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    if isinstance(whole, (dict, list)):
        return True
    decoder = json.JSONDecoder()
    for match in re.finditer(r'[\[{]', text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        # Embedded scalar arrays such as [1] are commonly citation markers,
        # matching the existing splitter's protection contract.
        if isinstance(value, dict) or (isinstance(value, list) and any(isinstance(v, dict) for v in value)):
            return True
    return False


def _bucket(prediction: dict) -> str:
    if not isinstance(prediction, dict):
        raise ValueError('prediction must be a dictionary')
    bucket = prediction.get('bucket')
    if bucket in _PARTS:
        return bucket
    count = prediction.get('estimated_count')
    if bucket is None and type(count) is int and count >= 0:
        return 'le20' if count <= 20 else '21to40' if count <= 40 else 'gt40'
    raise ValueError('invalid entity-count band')


def refine_chunks(
    content: str,
    items: list[dict],
    estimator: Callable[[str], dict],
    overlap: int = 50,
    logger: logging.Logger | None = None,
) -> list[dict]:
    """Estimate each original item once and split into at most three chunks.

    Ranges remain global half-open source offsets. Boundary expansion shares
    exactly effective_overlap characters and cannot erase progress on short
    chunks. Protected structures remain original atomic source chunks.
    """
    if type(overlap) is not int or overlap < 0:
        raise ValueError('overlap must be a non-negative integer')
    log = logger if logger is not None else _LOGGER
    refined: list[dict] = []
    for index, item in enumerate(items):
        text = item.get('text')
        source_range = item.get('range')
        if (not isinstance(text, str) or not isinstance(source_range, (list, tuple))
                or len(source_range) != 2 or any(type(v) is not int for v in source_range)
                or not 0 <= source_range[0] <= source_range[1] <= len(content)
                or content[source_range[0]:source_range[1]] != text):
            raise ValueError(f'item {index} does not match its source range')
        start, end = source_range
        record = {'index': index, 'source_range': [start, end], 'bucket': None,
                  'planned_parts': None, 'actual_parts': 1, 'original_length': len(text),
                  'child_sizes': [len(text)], 'ranges': [[start, end]],
                  'requested_overlap': overlap, 'effective_overlap': 0}
        ranges = [[start, end]]
        try:
            prediction = estimator(text)
            bucket = _bucket(prediction)
        except Exception as exc:
            # Exception messages and arbitrary model output can contain source
            # text or credentials. Record the exception type only.
            record.update(status='estimator_fallback', reason=type(exc).__name__)
        else:
            planned = _PARTS[bucket]
            record.update(bucket=bucket, planned_parts=planned)
            if type(prediction.get('estimated_count')) is int:
                record['estimated_count'] = prediction['estimated_count']
            confidence = prediction.get('confidence')
            if type(confidence) in (int, float) and 0 <= confidence <= 1:
                record['confidence'] = confidence
            if planned == 1:
                record['status'] = 'kept'
            elif (bounds := row_boundaries(text)) is not None and len(bounds) > 2:
                parts = min(planned, len(bounds) - 1)
                cuts = [bounds[(len(bounds) - 1) * i // parts] for i in range(parts + 1)]
                ranges = []
                for i, (a, b) in enumerate(zip(cuts, cuts[1:])):
                    # Only repeat whole rows within the configured overlap budget.
                    if i:
                        preceding = [value for value in bounds if cuts[i-1] < value < a and a-value <= overlap]
                        a = min(preceding, default=a)
                    ranges.append([start+a, start+b])
                actual = [max(0, left[1]-right[0]) for left, right in zip(ranges, ranges[1:])]
                record.update(status='refined_jsonl', actual_parts=parts,
                              effective_overlap=max(actual, default=0), effective_overlaps=actual,
                              ranges=ranges, child_sizes=[b-a for a,b in ranges])
            elif _structured(text):
                record['status'] = 'preserved_structured'
            elif len(text) < 2:
                record['status'] = 'preserved_short'
            else:
                parts = min(planned, len(text))
                cuts = [len(text) * i // parts for i in range(parts + 1)]
                effective = min(overlap, min(b-a for a,b in zip(cuts,cuts[1:])) - 1)
                left = effective // 2
                right = effective - left
                ranges = [[start + max(0, a - (left if i else 0)),
                           start + min(len(text), b + (right if i < parts-1 else 0))]
                          for i,(a,b) in enumerate(zip(cuts,cuts[1:]))]
                record.update(status='refined' if parts == planned else 'refined_short',
                              actual_parts=parts, effective_overlap=effective,
                              ranges=ranges, child_sizes=[b-a for a,b in ranges])
        for a,b in ranges:
            child = {**item, 'text': content[a:b], 'range': [a,b]}
            if isinstance(item.get('metadata'), dict):
                child['metadata'] = dict(item['metadata'])
                bounds = row_boundaries(text) if record.get('status') == 'refined_jsonl' else None
                if bounds is not None and item['metadata'].get('table_format') == 'jsonl':
                    row_offset = int(item['metadata'].get('table_data_row_start', 1))
                    child['metadata']['table_data_row_start'] = row_offset + bounds.index(a-start)
                    child['metadata']['table_data_row_end'] = row_offset + bounds.index(b-start) - 1
            refined.append(child)
        _audit(record)
        log.info('entity_count_chunking %s', json.dumps(record, ensure_ascii=False))
    return refined
