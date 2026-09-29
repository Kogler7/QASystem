from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from .graph_utils import (
    coerce_non_empty_name,
    entity_uid,
    merge_node_properties,
)
from .mapping_scorer import merge_confidence_payload


def _confidence_payload(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("confidence") is None:
        return {}
    payload = {
        key: item[key]
        for key in (
            "mapping_score",
            "confidence",
            "confidence_status",
            "gate_status",
        )
        if item.get(key) is not None
    }
    if isinstance(item.get("confidence_details"), dict):
        payload["confidence_details"] = dict(item["confidence_details"])
    return payload


def _projection_observation_key(
    item: dict[str, Any],
    fact_key: tuple[str, ...],
    fact: dict[str, Any],
) -> str | None:
    """Identify one aggregate confidence observation projected to a chunk.

    The adapter copies the document-level ``raw`` payload and its fact
    confidence onto every projected chunk. Such a copy is not a new
    observation, regardless of whether the fact had already been aggregated.
    Requiring a non-empty identical raw payload leaves ordinary chunk-local
    observations (including equal scores) independent.
    """
    details = fact.get("confidence_details")
    raw = item.get("raw")
    if not isinstance(details, dict):
        return None
    if not isinstance(raw, dict) or not raw:
        return None
    return json.dumps(
        {
            "fact": fact_key,
            "confidence": _confidence_payload(fact),
            "raw": raw,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


def deduplicate_chunk_graphs(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Merge nodes first, then recover valid cross-chunk raw relationships."""
    nodes_by_uid: dict[str, dict[str, Any]] = {}
    node_observations: dict[str, set[str]] = {}
    relationships: list[dict[str, Any]] = []
    relationships_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

    for position, item in enumerate(items):
        graph = item.get("graph") or {}
        chunk_index = item.get("index", position)
        for node in graph.get("nodes", []):
            uid = str(node.get("uid") or "")
            if not uid:
                continue
            observation_key = _projection_observation_key(item, (uid,), node)
            projected_indices = node.get("chunk_indices")
            source_indices = (
                list(dict.fromkeys(projected_indices))
                if isinstance(projected_indices, list)
                else [chunk_index]
            )
            if uid not in nodes_by_uid:
                nodes_by_uid[uid] = {
                    **deepcopy(node),
                    "properties": deepcopy(node.get("properties") or {}),
                    "chunk_indices": source_indices,
                }
                if observation_key is not None:
                    node_observations[uid] = {observation_key}
            else:
                nodes_by_uid[uid]["properties"] = merge_node_properties(
                    nodes_by_uid[uid].setdefault("properties", {}),
                    node.get("properties") or {},
                )
                chunk_indices = nodes_by_uid[uid].setdefault("chunk_indices", [])
                for source_index in source_indices:
                    if source_index not in chunk_indices:
                        chunk_indices.append(source_index)
                if observation_key is not None:
                    seen = node_observations.setdefault(uid, set())
                    if observation_key in seen:
                        continue
                    seen.add(observation_key)
                nodes_by_uid[uid].update(
                    merge_confidence_payload(
                        _confidence_payload(nodes_by_uid[uid]),
                        _confidence_payload(node),
                    )
                )

    valid_uids = set(nodes_by_uid)

    relationship_candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in items:
        graph = item.get("graph") or {}
        graph_relationships = deepcopy(graph.get("relationships", []))
        relationship_candidates.extend((relation, item) for relation in graph_relationships)
        graph_keys = {
            (
                str(relation.get("from_uid") or ""),
                str(relation.get("to_uid") or ""),
                str(relation.get("type") or ""),
            )
            for relation in graph_relationships
            if isinstance(relation, dict)
        }
        raw = item.get("raw") or {}
        for relation in raw.get("relations", []):
            if not isinstance(relation, dict):
                continue
            from_type = coerce_non_empty_name(relation.get("from_type"))
            from_name = coerce_non_empty_name(relation.get("from_name"))
            to_type = coerce_non_empty_name(relation.get("to_type"))
            to_name = coerce_non_empty_name(relation.get("to_name"))
            relation_type = coerce_non_empty_name(relation.get("rel_type"))
            if not all((from_type, from_name, relation_type, to_type, to_name)):
                continue
            fallback = {
                "from_uid": str(relation.get("from_uid") or entity_uid(from_type, from_name, None)),
                "to_uid": str(relation.get("to_uid") or entity_uid(to_type, to_name, None)),
                "type": relation_type,
                **_confidence_payload(relation),
            }
            fallback_key = (
                fallback["from_uid"],
                fallback["to_uid"],
                fallback["type"],
            )
            if fallback_key not in graph_keys:
                relationship_candidates.append((fallback, item))

    relationship_observations: dict[tuple[str, str, str], set[str]] = {}
    for relationship, source_item in relationship_candidates:
        key = (
            str(relationship.get("from_uid") or ""),
            str(relationship.get("to_uid") or ""),
            str(relationship.get("type") or ""),
        )
        if key[0] not in valid_uids or key[1] not in valid_uids:
            continue
        observation_key = _projection_observation_key(source_item, key, relationship)
        if key in relationships_by_key:
            if observation_key is not None:
                seen = relationship_observations.setdefault(key, set())
                if observation_key in seen:
                    continue
                seen.add(observation_key)
            existing = relationships_by_key[key]
            existing.update(
                merge_confidence_payload(
                    _confidence_payload(existing),
                    _confidence_payload(relationship),
                )
            )
            continue
        owned_relationship = deepcopy(relationship)
        relationships_by_key[key] = owned_relationship
        relationships.append(owned_relationship)
        if observation_key is not None:
            relationship_observations[key] = {observation_key}

    return {"nodes": list(nodes_by_uid.values()), "relationships": relationships}
