"""Bounds for every V3.1 model request, independent of upstream chunk size."""
from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv

import json
import math
import time
import threading
from collections import deque
from dataclasses import replace
from uuid import uuid4


class RecoveryBudget:
    """One finite attempt allowance shared by every descendant of a failed task."""

    def __init__(self, attempts, branches=1):
        self.limit = max(1, 2 * int(attempts) - 1) + 2 * max(0, int(branches) - 1)
        self.used = 0
        self.task_id = uuid4().hex[:16]
        self.lock = threading.Lock()

    def claim(self):
        return self.claim_trace() is not None

    def claim_trace(self):
        with self.lock:
            if self.used >= self.limit:
                return None
            self.used += 1
            return self.trace()

    def trace(self):
        return {'logical_task_id': self.task_id, 'logical_attempt': self.used,
                'logical_attempt_limit': self.limit}


def fair_source_windows(chunks, limit):
    """Give every original source a turn before one large table fills the queue."""
    pending = deque(iter(iter_windows(chunk, limit)) for chunk in chunks)
    while pending:
        iterator = pending.popleft()
        try:
            yield next(iterator)
        except StopIteration:
            continue
        pending.append(iterator)


def input_limit(llm=None):
    # Byte limits protect serialized requests; they are not model token counts.
    limits = [int(config_getenv("KG_EXTRACT_V31_MAX_INPUT_BYTES", "48000"))]
    for owner in (llm, getattr(llm, "client", None)):
        value = getattr(owner, "max_input_bytes", None)
        if isinstance(value, (int, float)) and value > 0:
            limits.append(int(value))
    return max(1, min(limits))


def message_bytes(messages):
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


def fits(messages, llm=None, *, reserve=2048):
    if message_bytes(messages) + reserve > input_limit(llm):
        return False
    owners = (llm, getattr(llm, "client", None))
    limits = [int(value) for owner in owners
              if isinstance(value := getattr(owner, 'max_input_tokens', None), (int, float)) and value > 0]
    if not limits:
        return True
    measure = next((getattr(owner, 'measure_input') for owner in owners
                    if callable(getattr(owner, 'measure_input', None))), None)
    if measure is not None:
        tokens = measure({'messages': messages}).tokens
    else:
        from algorithm.token_budget import estimate_input_tokens
        tokens = estimate_input_tokens({'messages': messages})
    return tokens <= min(limits)


def window_chars(llm=None):
    value = max(128, int(config_getenv("KG_EXTRACT_V31_WINDOW_CHARS", "2000")))
    for owner in (llm, getattr(llm, "client", None)):
        cap = getattr(owner, "max_tokens", None)
        if isinstance(cap, (int, float)) and cap > 0:
            value = min(value, max(128, int(cap) // 2))
    return value


def windows(chunk, limit, *, overlap=100):
    """Lossless source windows. Private offsets never enter a model prompt."""
    return list(iter_windows(chunk, limit, overlap=overlap))


def iter_windows(chunk, limit, *, overlap=100):
    """Yield source windows without copying the whole document into a work queue."""
    text = chunk.text or ""
    if len(text) <= limit:
        yield chunk
        return
    from .source_windows import table_windows
    structured = table_windows(chunk, limit)
    if structured is not None:
        yield from structured
        return
    start = 0
    overlap = min(max(0, overlap), max(0, limit // 4))
    base = int(chunk.metadata.get("_v31_window_start", 0))
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            boundary = max(text.rfind(c, start + limit // 2, end) for c in "\n。；")
            if boundary >= 0:
                end = boundary + 1
        yield replace(chunk, text=text[start:end], metadata={
            **chunk.metadata, "_v31_window_start": base + start,
        })
        if end == len(text):
            break
        start = end - overlap


def split_request_failure(attempts):
    return bool(attempts and (attempts[-1].get('adaptive_split') or attempts[-1].get("type") in {"incomplete", "input_budget"}
        or (attempts[-1].get("type") == "timeout" and attempts[-1].get("retryable", True))
        or (attempts[-1].get("type") == "request" and any(s in
            attempts[-1].get("message", "").lower() for s in
            ("context length", "context_length", "maximum context", "token limit", "too many tokens")))))


def document_deadline(llm):
    deadlines = []
    for owner in (llm, getattr(llm, 'client', None)):
        deadline = getattr(owner, 'deadline', None)
        if type(deadline) in (int, float) and math.isfinite(deadline):
            deadlines.append(deadline)
    return min(deadlines) if deadlines else None


def document_expired(llm):
    deadline = document_deadline(llm)
    return deadline is not None and time.monotonic() >= deadline


def recover_split_attempts(attempts):
    for attempt in attempts:
        if attempt.get("terminal"):
            attempt.update(terminal=False, recovery="split_request")
