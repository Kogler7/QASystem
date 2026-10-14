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


from qasystem_config.quota_schema import QuotaTier, parse_quota_tiers, select_tier, tighten_tiers


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
