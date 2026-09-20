from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable


_CJK_SPACE = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")
_DASHES = str.maketrans({"—": "-", "–": "-", "－": "-", "﹣": "-"})


def coerce_non_empty_name(value: Any) -> str:
    """Return a normalized non-empty display name, or an empty string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def canonical_name(value: Any) -> str:
    """Normalize entity identities without discarding meaningful punctuation.

    NFKC fixes full-width OCR output, CJK-only spacing is collapsed, and Latin
    text is case-folded. Clause numbers such as ``5.1`` remain intact.
    """
    text = coerce_non_empty_name(value)
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).translate(_DASHES)
    text = _CJK_SPACE.sub("", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def canonical_relation_name(value: Any) -> str:
    """Normalize relation IDs across camelCase and separator-based spellings."""
    return re.sub(r"[_\s-]+", "", canonical_name(value))


def entity_uid(entity_type: Any, entity_name: Any, properties: dict[str, Any] | None) -> str:
    """Build a readable, process-stable UID for an extracted entity."""
    normalized_type = coerce_non_empty_name(entity_type) or "Element"
    normalized_name = canonical_name(entity_name)
    if normalized_name:
        return f"{normalized_type}::{normalized_name}"
    payload = json.dumps(
        properties or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{normalized_type}::__anonymous__{digest}"


def build_node_properties(entity_name: Any, properties: dict[str, Any] | None) -> dict[str, Any]:
    """Populate node properties while preventing empty `name` overrides."""
    merged = dict(properties or {})
    resolved_name = coerce_non_empty_name(entity_name) or coerce_non_empty_name(merged.get("name"))
    if resolved_name:
        merged["name"] = resolved_name
    else:
        merged.setdefault("name", "")
    return merged


def merge_node_properties(existing: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any]:
    """Merge node properties without allowing empty `name` to replace a non-empty one."""
    merged = dict(existing or {})
    for key, value in (incoming or {}).items():
        if key != "name":
            current = merged.get(key)
            if isinstance(current, list) and isinstance(value, list):
                merged[key] = list(dict.fromkeys([*current, *value]))
            elif current in (None, "", []):
                merged[key] = value
            elif value not in (None, "", []) and isinstance(current, str) and isinstance(value, str):
                # OCR/chunk boundaries often produce a shorter partial value.
                merged[key] = value if current in value else current
            continue
        incoming_name = coerce_non_empty_name(value)
        existing_name = coerce_non_empty_name(merged.get("name"))
        if incoming_name or not existing_name:
            merged["name"] = incoming_name
    if "name" not in merged:
        merged["name"] = ""
    return merged


def filter_relationships_with_known_nodes(
    relationships: Iterable[dict[str, Any]],
    valid_uids: set[str],
) -> list[dict[str, Any]]:
    """Keep only relationships whose endpoints are present in the node set."""
    filtered: list[dict[str, Any]] = []
    for relationship in relationships:
        from_uid = str(relationship.get("from_uid") or "")
        to_uid = str(relationship.get("to_uid") or "")
        if not from_uid or not to_uid:
            continue
        if from_uid not in valid_uids or to_uid not in valid_uids:
            continue
        filtered.append(relationship)
    return filtered
