"""Pure structured quota schema; independent of callers and deployment."""
import json
from dataclasses import dataclass
from typing import Any

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
