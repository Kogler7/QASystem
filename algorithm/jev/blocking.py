"""Deterministic candidate blocking shared by coreference and graph alignment."""
from __future__ import annotations

from typing import Any, Mapping


def normalize_name(name: str) -> str:
    return "".join(str(name).split()).casefold()


def names_overlap(left: str, right: str) -> bool:
    """True when the names are not identical and share a meaningful span.

    A span counts when the shorter name has at least two characters and is
    contained in the longer one, or both share any contiguous piece of that length.
    Identical names stay on the exact-key merge path and are not JEV candidates.
    """
    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b or a == b:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 2:
        return False
    if short in long:
        return True
    for size in range(len(short), 1, -1):
        for start in range(0, len(short) - size + 1):
            if short[start : start + size] in long:
                return True
    return False


def iter_coref_pairs(entities: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Same-type overlapping names, ordered so the same pair always renders once."""
    rows = [_entity_view(entity) for entity in entities]
    pairs: list[dict[str, Any]] = []
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            if left["type"] != right["type"]:
                continue
            if not names_overlap(left["name"], right["name"]):
                continue
            first, second = sorted((left, right), key=lambda item: (normalize_name(item["name"]), item["name"]))
            pairs.append(
                {
                    "state_id": f"{first['type']}\x1f{first['name']}\x1f{second['name']}",
                    "type": first["type"],
                    "left_name": first["name"],
                    "right_name": second["name"],
                    "left_properties": first["properties"],
                    "right_properties": second["properties"],
                    "left_quote": first["quote"],
                    "right_quote": second["quote"],
                }
            )
    pairs.sort(key=lambda item: item["state_id"])
    return pairs


def _entity_view(entity: Mapping[str, Any]) -> dict[str, Any]:
    properties = entity.get("properties") if isinstance(entity.get("properties"), Mapping) else {}
    name = str(entity.get("name") or properties.get("name") or "")
    return {
        "type": str(entity.get("type") or entity.get("label") or ""),
        "name": name,
        "properties": {key: value for key, value in properties.items() if key != "name"},
        "quote": _first_quote(entity),
    }


def _first_quote(entity: Mapping[str, Any]) -> str:
    for key in ("source_refs", "evidence", "evidences"):
        refs = entity.get(key)
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if isinstance(ref, Mapping):
                quote = ref.get("quote")
                if quote:
                    return str(quote)
    return ""
