"""Annotate new nodes against a caller-supplied shortlist. Does not write a graph."""
from __future__ import annotations

import json
from typing import Any, Mapping

from algorithm.jev.blocking import names_overlap, normalize_name
from algorithm.jev.contract import GRAPH_ALIGN
from algorithm.jev.service import Judgment

SHORTLIST_LIMIT = 8


def align_nodes(
    new_nodes: list[Mapping[str, Any]],
    existing_nodes: list[Mapping[str, Any]],
    judgments: Mapping[str, Judgment],
    *,
    routes: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One annotation per shortlisted pair. Missing or non-link routes stay ``new``."""
    annotations: list[dict[str, Any]] = []
    for pair in shortlist(new_nodes, existing_nodes):
        judgment = judgments.get(pair["state_id"])
        action = (routes or {}).get(pair["state_id"], "keep")
        conflicts = conflicting_properties(pair["new_properties"], pair["existing_properties"])
        if judgment is None or judgment.status != "decided" or action != "link":
            annotation = "new"
        elif conflicts:
            annotation = "conflict"
        else:
            annotation = "link"
        annotations.append(
            {
                "state_id": pair["state_id"],
                "task": GRAPH_ALIGN,
                "new_name": pair["new_name"],
                "existing_name": pair["existing_name"],
                "existing_uid": pair["existing_uid"],
                "type": pair["type"],
                "choice": judgment.choice if judgment is not None else "",
                "confidence": judgment.confidence if judgment is not None else 0.0,
                "route": action if judgment is not None else "keep",
                "annotation": annotation,
                "property_conflicts": conflicts,
            }
        )
    return annotations


def shortlist(
    new_nodes: list[Mapping[str, Any]],
    existing_nodes: list[Mapping[str, Any]],
    *,
    limit: int = SHORTLIST_LIMIT,
) -> list[dict[str, Any]]:
    existing = [_view(node) for node in existing_nodes]
    pairs: list[dict[str, Any]] = []
    for node in new_nodes:
        new = _view(node)
        matched = [
            item
            for item in existing
            if item["type"] == new["type"] and names_overlap(new["name"], item["name"])
        ]
        matched.sort(key=lambda item: (len(normalize_name(item["name"])), item["name"], item["uid"]))
        for item in matched[:limit]:
            pairs.append(
                {
                    "state_id": f"{new['type']}\x1f{new['name']}\x1f{item['uid'] or item['name']}",
                    "type": new["type"],
                    "new_name": new["name"],
                    "existing_name": item["name"],
                    "new_properties": new["properties"],
                    "existing_properties": item["properties"],
                    "new_quote": new["quote"],
                    "existing_uid": item["uid"],
                }
            )
    return pairs


def conflicting_properties(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[str]:
    """Non-empty values of the same key that differ after string stripping."""
    conflicts: list[str] = []
    for key in sorted(set(left) & set(right)):
        if key == "name":
            continue
        current = left[key]
        incoming = right[key]
        if not _nonempty(current) or not _nonempty(incoming):
            continue
        if _canonical(current) != _canonical(incoming):
            conflicts.append(str(key))
    return conflicts


def _view(node: Mapping[str, Any]) -> dict[str, Any]:
    properties = node.get("properties") if isinstance(node.get("properties"), Mapping) else {}
    name = str(node.get("name") or properties.get("name") or "")
    return {
        "type": str(node.get("type") or node.get("label") or ""),
        "name": name,
        "uid": str(node.get("uid") or ""),
        "properties": {key: value for key, value in properties.items() if key != "name"},
        "quote": _first_quote(node),
    }


def _first_quote(node: Mapping[str, Any]) -> str:
    for key in ("source_refs", "evidence", "evidences"):
        refs = node.get(key)
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if isinstance(ref, Mapping) and ref.get("quote"):
                return str(ref["quote"])
    return ""


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple)):
        return len(value) > 0
    return True


def _canonical(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
