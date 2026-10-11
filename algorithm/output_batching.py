"""Bounded output-driven splitting, independent of model/provider clients.

The caller rebuilds both prompt and schema for each subset. All invocations
still use the caller's scheduler, deadline and evidence validation pipeline.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


def generate_batched(
    items: list[Any],
    build_request: Callable,
    generate: Callable,
    llm: Any,
    *,
    batch_size: int,
    max_depth: int = 6,
    max_calls: int = 63,
    stats: dict[str, int] | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Split truncated batches immediately; retain successful siblings.

    A failed leaf remains terminal. A split parent is superseded only by its
    children, never by a recovered JSON prefix. Limits bound generation calls
    (each may additionally perform bounded transport retries).
    """
    attempts = kwargs['attempts']
    root = kwargs['chunk_id']
    size = max(1, batch_size)
    safe_size = size
    pending = [(items[i:i + size], 0, str(i // size)) for i in range(0, len(items), size)]
    result: dict[str, Any] = {}
    succeeded = False
    calls = 0
    deadline = getattr(getattr(llm, 'client', None), 'deadline', None)
    while pending:
        batch, depth, tag = pending.pop(0)
        chunk_id = f'{root}.part-{tag}'
        # Learn only inside this invocation: no provider/model/run can change
        # another request's batching. Do not repeat a known oversized sibling.
        if len(batch) > safe_size and depth < max_depth:
            pending[0:0] = [(batch[i:i + safe_size], depth + 1, f'{tag}s{i // safe_size}')
                            for i in range(0, len(batch), safe_size)]
            continue
        expired = isinstance(deadline, (int, float)) and time.monotonic() >= deadline
        if calls >= max_calls or expired:
            attempts.append({
                'attempt': 1, 'type': 'timeout' if expired else 'incomplete', 'pass': kwargs['pass_name'],
                'chunk': chunk_id, 'terminal': True, 'retryable': False,
                'message': 'Document deadline exhausted' if expired else 'Output split call budget exhausted',
                'pending_items': len(batch) + sum(len(row[0]) for row in pending),
            })
            break
        messages, schema = build_request(batch)
        start = len(attempts)
        parsed = generate(llm, messages, schema, **{
            **kwargs, 'chunk_id': chunk_id,
        })
        calls += 1
        if stats is not None:
            stats['calls'] = stats.get('calls', 0) + 1
        failures = attempts[start:]
        truncated = any(row.get('type') in {'incomplete', 'input_limit'} for row in failures)
        if parsed is None and truncated and len(batch) > 1 and depth < max_depth:
            for row in failures:
                row['terminal'] = False
                row['recovered_by'] = 'output_batch_split'
            middle = len(batch) // 2
            safe_size = min(safe_size, middle)
            pending[0:0] = [(batch[:middle], depth + 1, tag + 'a'),
                            (batch[middle:], depth + 1, tag + 'b')]
            logger.warning('Token budget split: pass=%s chunk=%s items=%d split=%d+%d depth=%d',
                           kwargs['pass_name'], chunk_id, len(batch), middle,
                           len(batch) - middle, depth + 1)
            continue
        if parsed is None:
            continue
        succeeded = True
        for key, value in parsed.items():
            if isinstance(value, list):
                result.setdefault(key, []).extend(value)
            elif isinstance(value, dict):
                result.setdefault(key, {}).update(value)
            else:
                raise ValueError(f'Batch output must contain collections: {key}')
    return result if succeeded else None


def output_batch_size(llm: Any, *, tokens_per_item: int, ceiling: int) -> int:
    """Reserve 35% of the configured output budget for variance and framing."""
    limit = getattr(llm, 'max_output_tokens', None)
    if not isinstance(limit, int) or limit <= 0:
        limit = getattr(llm, 'max_tokens', None)
    limit = limit if isinstance(limit, int) and limit > 0 else 16384
    return max(1, min(ceiling, int(limit * 0.65) // tokens_per_item))
