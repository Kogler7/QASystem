"""HTTP Q&A delivery contract.

The Agent HTTP Worker always attaches this schema. Library callers may still
pass a custom DeliveryConfig or leave it unset.
"""

from __future__ import annotations

import json
from typing import Any

from .models import DeliveryConfig

ANSWER_V1_ID = "answer-v1"

ANSWER_V1_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "conclusion",
        "evidence",
        "assumptions",
        "gaps",
        "confidence",
        "confidence_reason",
        "charts",
        "solution_path",
    ],
    "properties": {
        "conclusion": {
            "type": "string",
            "minLength": 1,
            "description": "Direct answer to the user question.",
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "detail"],
                "properties": {
                    "name": {"type": "string"},
                    "id": {"type": "string"},
                    "detail": {"type": "string"},
                },
            },
        },
        "assumptions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "gaps": {
            "type": "array",
            "items": {"type": "string"},
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
        },
        "confidence_reason": {
            "type": "string",
            "description": "Why this confidence was chosen (evidence-gate checks).",
        },
        "charts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "title": {"type": "string"},
                    "path": {"type": "string"},
                },
            },
        },
        "solution_path": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}

_CONFIDENCE_ALIASES = {
    "high": "high",
    "medium": "medium",
    "low": "low",
    "高": "high",
    "中": "medium",
    "低": "low",
}


def answer_v1_delivery() -> DeliveryConfig:
    return DeliveryConfig(output_schema_id=ANSWER_V1_ID, json_schema=ANSWER_V1_SCHEMA)


def schema_prompt_text(delivery: DeliveryConfig) -> str:
    schema_json = json.dumps(delivery.json_schema, ensure_ascii=False, indent=2)
    return (
        f"Schema id: `{delivery.output_schema_id}`\n\n"
        f"```json\n{schema_json}\n```"
    )


def normalize_answer_payload(payload: Any) -> Any:
    """Map bilingual confidence labels; do not invent missing fields."""
    if not isinstance(payload, dict):
        return payload
    confidence = payload.get("confidence")
    if not isinstance(confidence, str):
        return payload
    key = confidence.strip()
    mapped = _CONFIDENCE_ALIASES.get(key.lower()) or _CONFIDENCE_ALIASES.get(key)
    if mapped is None or mapped == confidence:
        return payload
    normalized = dict(payload)
    normalized["confidence"] = mapped
    return normalized
