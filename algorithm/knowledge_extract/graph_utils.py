from __future__ import annotations

from typing import Any, Iterable


def coerce_non_empty_name(value: Any) -> str:
    """Return a normalized non-empty display name, or an empty string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


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
            merged[key] = value
            continue
        incoming_name = coerce_non_empty_name(value)
        existing_name = coerce_non_empty_name(merged.get("name"))
        if incoming_name or not existing_name:
            merged["name"] = incoming_name
    if "name" not in merged:
        merged["name"] = ""
    return merged


def iter_persistable_properties(properties: dict[str, Any] | None) -> list[tuple[str, Any]]:
    """Return node properties that are safe to persist without empty-name overwrite."""
    persisted: list[tuple[str, Any]] = []
    for key, value in (properties or {}).items():
        if value is None:
            continue
        if key == "name" and not coerce_non_empty_name(value):
            continue
        persisted.append((key, value))
    return persisted


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
