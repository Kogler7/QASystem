"""Provider-neutral structured-output schemas and validation.

The extractor uses the same schemas whether the LLM provider supports native
Structured Outputs or only returns JSON text.  Validation intentionally covers
the small JSON-Schema subset emitted by :mod:`ontology_compiler`, avoiding a
new runtime dependency.
"""
from __future__ import annotations

import re
from typing import Any


def validate_json_schema(instance: Any, schema: dict[str, Any], *, path: str = "$") -> list[str]:
    """Return deterministic validation errors for the emitted schema subset."""
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(instance, expected_type):
        return [f"{path}: expected {expected_type}, got {type(instance).__name__}"]

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: value {instance!r} is not in enum")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected constant {schema['const']!r}")

    if isinstance(instance, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in instance:
                errors.append(f"{path}: missing required property {name!r}")
        if schema.get("additionalProperties") is False:
            for name in instance:
                if name not in properties:
                    errors.append(f"{path}: unexpected property {name!r}")
        for name, child in properties.items():
            if name in instance:
                errors.extend(validate_json_schema(instance[name], child, path=f"{path}.{name}"))

    if isinstance(instance, list):
        if len(instance) < int(schema.get("minItems") or 0):
            errors.append(f"{path}: expected at least {schema['minItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                errors.extend(validate_json_schema(item, item_schema, path=f"{path}[{index}]"))

    if isinstance(instance, str):
        if len(instance) < int(schema.get("minLength") or 0):
            errors.append(f"{path}: string is shorter than {schema['minLength']}")
        pattern = schema.get("pattern")
        if pattern and re.search(pattern, instance) is None:
            errors.append(f"{path}: value does not match {pattern!r}")
    return errors


def _matches_type(instance: Any, expected: str | list[str]) -> bool:
    expected_types = {expected} if isinstance(expected, str) else set(expected)
    checks = {
        "object": isinstance(instance, dict),
        "array": isinstance(instance, list),
        "string": isinstance(instance, str),
        "number": isinstance(instance, (int, float)) and not isinstance(instance, bool),
        "integer": isinstance(instance, int) and not isinstance(instance, bool),
        "boolean": isinstance(instance, bool),
        "null": instance is None,
    }
    return any(checks.get(item, False) for item in expected_types)


__all__ = ["validate_json_schema"]
