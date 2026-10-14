"""Bounded output-driven splitting, independent of model/provider clients.

The caller rebuilds both prompt and schema for each subset. All invocations
still use the caller's scheduler, deadline and evidence validation pipeline.
Independent sibling batches may run concurrently; truncation children replace
only their parent and keep the same depth/call budgets.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Callable

from algorithm.extraction_trace import trace_event

logger = logging.getLogger(__name__)


def _batch_workers(llm: Any) -> int:
    value = getattr(llm, "max_concurrency", None)
    if isinstance(value, int) and value > 0:
        return value
    return 1


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
    (each may additionally perform bounded transport retries). Independent
    pending parts run up to ``llm.max_concurrency`` workers; HTTP in-flight is
    still gated by the caller's scheduler / quota pool.
    """
    attempts = kwargs["attempts"]
    root = kwargs["chunk_id"]
    size = max(1, batch_size)
    safe_size = size
    pending = [(items[i:i + size], 0, str(i // size)) for i in range(0, len(items), size)]
    result: dict[str, Any] = {}
    succeeded = False
    calls = 0
    stop = False
    lock = threading.Lock()
    deadline = getattr(getattr(llm, "client", None), "deadline", None)
    workers = _batch_workers(llm)

    def merge_parsed(parsed: dict[str, Any]) -> None:
        nonlocal succeeded
        succeeded = True
        for key, value in parsed.items():
            if isinstance(value, list):
                result.setdefault(key, []).extend(value)
            elif isinstance(value, dict):
                result.setdefault(key, {}).update(value)
            else:
                raise ValueError(f"Batch output must contain collections: {key}")

    def run_one(batch: list[Any], depth: int, tag: str, local_safe: int) -> dict[str, Any]:
        chunk_id = f"{root}.part-{tag}"
        local_attempts: list[dict[str, Any]] = []
        messages, schema = build_request(batch)
        parsed = generate(llm, messages, schema, **{
            **kwargs,
            "chunk_id": chunk_id,
            "attempts": local_attempts,
        })
        truncated = any(row.get("type") in {"incomplete", "input_limit"} for row in local_attempts)
        if parsed is None and truncated and len(batch) > 1 and depth < max_depth:
            for row in local_attempts:
                row["terminal"] = False
                row["recovered_by"] = "output_batch_split"
            middle = len(batch) // 2
            return {
                "kind": "split",
                "chunk_id": chunk_id,
                "tag": tag,
                "depth": depth,
                "batch": batch,
                "middle": middle,
                "attempts": local_attempts,
                "local_safe": min(local_safe, middle),
            }
        return {
            "kind": "done",
            "chunk_id": chunk_id,
            "parsed": parsed,
            "attempts": local_attempts,
        }

    def take_jobs(limit: int) -> list[tuple[list[Any], int, str, int]]:
        nonlocal calls, stop, safe_size
        jobs: list[tuple[list[Any], int, str, int]] = []
        while pending and len(jobs) < limit and not stop:
            batch, depth, tag = pending.pop(0)
            chunk_id = f"{root}.part-{tag}"
            if len(batch) > safe_size and depth < max_depth:
                pending[0:0] = [
                    (batch[i:i + safe_size], depth + 1, f"{tag}s{i // safe_size}")
                    for i in range(0, len(batch), safe_size)
                ]
                continue
            expired = isinstance(deadline, (int, float)) and time.monotonic() >= deadline
            if calls >= max_calls or expired:
                attempts.append({
                    "attempt": 1,
                    "type": "timeout" if expired else "incomplete",
                    "pass": kwargs["pass_name"],
                    "chunk": chunk_id,
                    "terminal": True,
                    "retryable": False,
                    "message": (
                        "Document deadline exhausted"
                        if expired
                        else "Output split call budget exhausted"
                    ),
                    "pending_items": len(batch) + sum(len(row[0]) for row in pending),
                })
                stop = True
                break
            calls += 1
            if stats is not None:
                stats["calls"] = stats.get("calls", 0) + 1
            jobs.append((batch, depth, tag, safe_size))
        return jobs

    if workers <= 1:
        while pending and not stop:
            jobs = take_jobs(1)
            if not jobs:
                break
            batch, depth, tag, local_safe = jobs[0]
            outcome = run_one(batch, depth, tag, local_safe)
            attempts.extend(outcome["attempts"])
            if outcome["kind"] == "split":
                middle = outcome["middle"]
                tag = outcome["tag"]
                depth = outcome["depth"]
                batch = outcome["batch"]
                safe_size = min(safe_size, outcome["local_safe"])
                trace_event(
                    llm,
                    "batch-split",
                    pass_name=kwargs["pass_name"],
                    parent=outcome["chunk_id"],
                    children=[f"{root}.part-{tag}a", f"{root}.part-{tag}b"],
                    item_counts=[middle, len(batch) - middle],
                    depth=depth + 1,
                    reason=[row.get("type") for row in outcome["attempts"]],
                )
                pending[0:0] = [
                    (batch[:middle], depth + 1, tag + "a"),
                    (batch[middle:], depth + 1, tag + "b"),
                ]
                logger.warning(
                    "Token budget split: pass=%s chunk=%s items=%d split=%d+%d depth=%d",
                    kwargs["pass_name"],
                    outcome["chunk_id"],
                    len(batch),
                    middle,
                    len(batch) - middle,
                    depth + 1,
                )
                continue
            if outcome["parsed"] is not None:
                merge_parsed(outcome["parsed"])
        return result if succeeded else None

    def apply_outcome(outcome: dict[str, Any], *, allow_split: bool) -> None:
        nonlocal safe_size
        attempts.extend(outcome["attempts"])
        if outcome["kind"] == "split" and allow_split:
            middle = outcome["middle"]
            tag = outcome["tag"]
            depth = outcome["depth"]
            batch = outcome["batch"]
            safe_size = min(safe_size, outcome["local_safe"])
            trace_event(
                llm,
                "batch-split",
                pass_name=kwargs["pass_name"],
                parent=outcome["chunk_id"],
                children=[f"{root}.part-{tag}a", f"{root}.part-{tag}b"],
                item_counts=[middle, len(batch) - middle],
                depth=depth + 1,
                reason=[row.get("type") for row in outcome["attempts"]],
            )
            pending[0:0] = [
                (batch[:middle], depth + 1, tag + "a"),
                (batch[middle:], depth + 1, tag + "b"),
            ]
            logger.warning(
                "Token budget split: pass=%s chunk=%s items=%d split=%d+%d depth=%d",
                kwargs["pass_name"],
                outcome["chunk_id"],
                len(batch),
                middle,
                len(batch) - middle,
                depth + 1,
            )
        elif outcome["kind"] == "done" and outcome["parsed"] is not None:
            merge_parsed(outcome["parsed"])

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ke-batch") as executor:
        futures: dict[Any, None] = {}

        def fill() -> None:
            with lock:
                room = workers - len(futures)
                jobs = take_jobs(room) if room > 0 and not stop else []
            for batch, depth, tag, local_safe in jobs:
                futures[executor.submit(run_one, batch, depth, tag, local_safe)] = None

        fill()
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                futures.pop(future)
                outcome = future.result()
                with lock:
                    apply_outcome(outcome, allow_split=not stop)
            if stop:
                if not futures:
                    break
                finished, _ = wait(futures)
                for future in finished:
                    futures.pop(future)
                    outcome = future.result()
                    with lock:
                        apply_outcome(outcome, allow_split=False)
                break
            fill()
    return result if succeeded else None


def output_batch_size(llm: Any, *, tokens_per_item: int, ceiling: int) -> int:
    """Reserve 35% of the configured output budget for variance and framing."""
    limit = getattr(llm, "max_output_tokens", None)
    if not isinstance(limit, int) or limit <= 0:
        limit = getattr(llm, "max_tokens", None)
    limit = limit if isinstance(limit, int) and limit > 0 else 16384
    return max(1, min(ceiling, int(limit * 0.65) // tokens_per_item))
