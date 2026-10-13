"""Bounds for every V3.1 model request, independent of upstream chunk size."""
from __future__ import annotations
from docker.configuration import getenv as config_getenv

import json
import math
import os
import time
from dataclasses import replace


def input_limit(llm=None):
    # UTF-8 bytes are a conservative token upper bound; no provider tokenizer
    # dependency is needed. Honour the request-specific client limit as well.
    limits = [int(config_getenv("KG_EXTRACT_V31_MAX_INPUT_BYTES", "48000"))]
    for owner in (llm, getattr(llm, "client", None)):
        value = getattr(owner, "max_input_tokens", None)
        if isinstance(value, (int, float)) and value > 0:
            limits.append(int(value))
    value = config_getenv("KG_EXTRACT_LLM_MAX_INPUT_TOKENS")
    if value:
        limits.append(int(value))
    return max(1, min(limits))


def message_bytes(messages):
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


def fits(messages, llm=None, *, reserve=2048):
    return message_bytes(messages) + reserve <= input_limit(llm)


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
    return bool(attempts and (attempts[-1].get("type") in {"incomplete", "input_budget"}
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
