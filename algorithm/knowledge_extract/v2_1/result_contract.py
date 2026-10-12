"""Public result boundary for Knowledge Extract V2."""
from __future__ import annotations

from typing import Any

from .run_state import (
    ExtractionError,
    ExtractionRequestError,
    ExtractionResponseError,
    ExtractionTimeoutError,
    ExtractionValidationError,
)


_ERROR_TYPES = {
    "LLM_REQUEST_FAILED": ExtractionRequestError,
    "LLM_TIMEOUT": ExtractionTimeoutError,
    "LLM_JSON_PARSE_FAILED": ExtractionResponseError,
    "LLM_SCHEMA_INVALID": ExtractionValidationError,
}


def to_public_result(result: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return only the stable business contract or raise a typed failure."""
    status = str(result.get("status") or ("failed" if result.get("error_code") else "succeeded"))
    error_code = str(result.get("error_code") or "")
    if status != "succeeded" or error_code:
        error_type = _ERROR_TYPES.get(error_code, ExtractionError)
        raise error_type(
            str(result.get("_error") or result.get("error") or f"extraction ended with status {status}"),
            stage="extraction",
            details={
                "status": status,
                "error_code": error_code or None,
                "run_trace": result.get("run_trace"),
            },
        )

    raw = result.get("raw") or {}
    entities = [
        {
            "type": entity.get("type"),
            "name": entity.get("name"),
            "properties": {
                key: value
                for key, value in dict(entity.get("properties") or {}).items()
                if key != "name"
            },
        }
        for entity in raw.get("entities") or []
    ]
    relations = [
        {
            "from_type": relation.get("from_type"),
            "from_name": relation.get("from_name"),
            "rel_type": relation.get("rel_type"),
            "to_type": relation.get("to_type"),
            "to_name": relation.get("to_name"),
        }
        for relation in raw.get("relations") or []
    ]
    return {"entities": entities, "relations": relations}


__all__ = ["to_public_result"]
