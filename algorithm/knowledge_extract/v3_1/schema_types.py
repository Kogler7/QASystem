"""JSON shape helpers; datatype syntax never supplies a business fact."""
from __future__ import annotations

import math
import re
from typing import Any


def map_value_type(datatype: str) -> str | None:
    """Return a mapping's value declaration, '' for untyped, None for non-map.

    Map<T>/Dict<T> use T as the value type; Map<K,V>/Dict<K,V> use V.
    JSON object keys must be strings independently of the key declaration.
    Nested generic commas are not top-level parameter separators.
    """
    normalized = re.sub(r"\s+", "", datatype).lower().rstrip("?")
    if normalized in {"map", "dict", "object"}:
        return ""
    match = re.fullmatch(r"(?:map|dict)<(.+)>", normalized)
    if match is None:
        return None
    body = match.group(1)
    depth = 0
    begin = 0
    parts: list[str] = []
    for index, character in enumerate(body):
        if character == "<":
            depth += 1
        elif character == ">":
            depth -= 1
            if depth < 0:
                return None
        elif character == "," and depth == 0:
            parts.append(body[begin:index])
            begin = index + 1
    parts.append(body[begin:])
    if depth != 0 or len(parts) not in {1, 2} or not all(parts):
        return None
    return parts[-1]


def is_json_value(value: Any, _active: set[int] | None = None) -> bool:
    """Reject non-JSON Python values, non-string keys, cycles and nonfinite numbers."""
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if not isinstance(value, (list, dict)):
        return False
    active = set() if _active is None else _active
    identifier = id(value)
    if identifier in active:
        return False
    active.add(identifier)
    try:
        if isinstance(value, list):
            return all(is_json_value(item, active) for item in value)
        return all(isinstance(key, str) and is_json_value(item, active) for key, item in value.items())
    finally:
        active.remove(identifier)
