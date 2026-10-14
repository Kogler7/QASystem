"""Validated input-size quota curves; no deployment defaults live here."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from typing import Any


class QuotaLimitError(ValueError):
    error_type = "quota_limit"


@dataclass(frozen=True)
class QuotaTier:
    max_input_tokens: int
    rpm: int
    tpm: int


def parse_quota_tiers(value: Any) -> tuple[QuotaTier, ...]:
    """An empty curve explicitly disables size-based limits (e.g. local LLMs)."""
    try:
        rows = json.loads(value) if isinstance(value, str) else value
        if not isinstance(rows, (list, tuple)):
            raise ValueError
        tiers = []
        for row in rows:
            if isinstance(row, QuotaTier):
                row = vars(row)
            if not isinstance(row, dict) or set(row) != {"max_input_tokens", "rpm", "tpm"}:
                raise ValueError
            if any(type(v) is not int or v <= 0 for v in row.values()):
                raise ValueError
            tiers.append(QuotaTier(**row))
        if any(a.max_input_tokens >= b.max_input_tokens or a.rpm < b.rpm or a.tpm < b.tpm
               for a, b in zip(tiers, tiers[1:])):
            raise ValueError
        return tuple(tiers)
    except (ValueError, TypeError):
        # Never echo configuration contents in public errors.
        raise ValueError("Invalid extraction quota tiers") from None


def select_tier(tiers: tuple[QuotaTier, ...], input_tokens: int) -> QuotaTier | None:
    if not tiers:
        return None
    return next((tier for tier in tiers if input_tokens <= tier.max_input_tokens), tiers[-1])


def tighten_tiers(old: tuple[QuotaTier, ...], new: tuple[QuotaTier, ...]) -> tuple[QuotaTier, ...]:
    if not old or not new:
        return old or new
    boundaries = sorted({tier.max_input_tokens for tier in (*old, *new)})
    return tuple(QuotaTier(boundary,
                           min(select_tier(old, boundary).rpm, select_tier(new, boundary).rpm),
                           min(select_tier(old, boundary).tpm, select_tier(new, boundary).tpm))
                 for boundary in boundaries)


def response_usage(response: Any) -> tuple[int | None, int] | None:
    def field(obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
    usage = field(response, "usage")
    if usage is None:
        return None
    def count(*keys):
        for key in keys:
            value = field(usage, key)
            if type(value) is int and value >= 0:
                return value
        return None
    prompt = count("prompt_tokens", "input_tokens")
    output = count("completion_tokens", "output_tokens")
    total = count("total_tokens")
    if prompt is not None and output is not None:
        total = max(total or 0, prompt + output)
    if total is None or (prompt is not None and total < prompt):
        return None
    return prompt, total


@dataclass(frozen=True)
class RateLimitSignal:
    kind: str
    retry_after: float | None = None


def rate_limit_signal(error: Exception) -> RateLimitSignal | None:
    """Only an actual HTTP 429 affects adaptation; timeout text never does."""
    response = getattr(error, 'response', None)
    status = getattr(error, 'status_code', None) or getattr(response, 'status_code', None)
    if status != 429:
        return None
    body = getattr(error, 'body', None)
    if not isinstance(body, (dict, str)):
        body = ''
    value = (json.dumps(body, ensure_ascii=True) + ' ' + str(error)).lower()
    if re.search(r'concurr|parallel|simultaneous', value):
        kind = 'concurrency'
    elif re.search(r'\btpm\b|tokens?[_ /-]*(?:per[_ /-]*)?minute|tokens?[_ -]*rate', value):
        kind = 'tpm'
    elif re.search(r'\b(?:rpm|rps)\b|requests?[_ /-]*(?:per[_ /-]*)?(?:minute|second)|requests?[_ -]*rate', value):
        kind = 'rpm'
    else:
        kind = 'unknown'
    headers = getattr(response, 'headers', {}) or {}
    raw = headers.get('retry-after', headers.get('Retry-After'))
    retry_after = None
    if raw is not None:
        try:
            retry_after = max(0.0, float(raw))
            if not math.isfinite(retry_after):
                retry_after = None
        except (TypeError, ValueError):
            try:
                retry_after = max(0.0, (parsedate_to_datetime(str(raw)) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return RateLimitSignal(kind, retry_after)
