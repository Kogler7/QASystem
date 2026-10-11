"""Offline text-input budget checks without downloading a model tokenizer.

UTF-8 bytes are a conservative token estimate for the text-only BPE models used
here, not the provider's exact billing count. Count schema as well as messages.
"""
from __future__ import annotations

import json


class InputTokenLimitError(ValueError):
    error_type = 'input_limit'


def estimate_input_tokens(payload: dict) -> int:
    # Serialize only input-bearing fields, not credentials or output budgets.
    fields = {key: payload[key] for key in (
        'messages', 'input', 'instructions', 'response_format', 'text', 'tools',
    ) if key in payload}
    return len(json.dumps(fields, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8')) + 256


def check_input_budget(payload: dict, limit: int) -> int:
    estimate = estimate_input_tokens(payload)
    if estimate > limit:
        raise InputTokenLimitError(
            f'Estimated input tokens {estimate} exceed max_input_tokens {limit} (utf8_bytes estimate)'
        )
    return estimate
