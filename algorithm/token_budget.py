"""Separate byte safety checks from explicitly labelled fallback token estimates."""
from __future__ import annotations

import json


class InputTokenLimitError(ValueError):
    error_type = 'input_limit'


def input_bytes(payload: dict) -> int:
    # Serialize only input-bearing fields, not credentials or output budgets.
    fields = {key: payload[key] for key in (
        'messages', 'input', 'instructions', 'response_format', 'text', 'tools',
        'tool_choice', 'reasoning_effort', 'extra_body',
    ) if key in payload}
    return len(json.dumps(fields, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8'))


def check_input_bytes(payload: dict, limit: int) -> int:
    size = input_bytes(payload)
    if size > limit:
        raise InputTokenLimitError(f'Input bytes {size} exceed max_input_bytes {limit}')
    return size


def estimate_input_tokens(payload: dict) -> int:
    # Compatibility API and conservative fallback only. Real quota admission
    # uses the model-specific TokenCounter when its local assets are available.
    return input_bytes(payload) + 256


def check_input_budget(payload: dict, limit: int) -> int:
    estimate = estimate_input_tokens(payload)
    if estimate > limit:
        raise InputTokenLimitError(
            f'Estimated input tokens {estimate} exceed max_input_tokens {limit} (utf8_bytes estimate)'
        )
    return estimate
