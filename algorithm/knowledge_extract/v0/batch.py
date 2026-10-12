from __future__ import annotations

from copy import deepcopy
from typing import Any

from .graph_utils import (
    coerce_non_empty_name,
    entity_uid,
    filter_relationships_with_known_nodes,
    merge_node_properties,
)


def _merge_relationship_provenance(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Retain supplied provenance only; never infer a source from item presence."""
    evidences: list[Any] = []
    for relationship in (existing, incoming):
        values = relationship.get("evidences")
        values = list(values) if isinstance(values, list) else []
        singular = relationship.get("evidence")
        if isinstance(singular, dict):
            values.append(singular)
        for value in values:
            if value not in evidences:
                evidences.append(deepcopy(value))
    if evidences:
        existing["evidences"] = evidences
    incoming_indices = incoming.get("chunk_indices")
    if isinstance(incoming_indices, list):
        existing_indices = existing.get("chunk_indices")
        indices = list(existing_indices) if isinstance(existing_indices, list) else []
        for index in incoming_indices:
            if index not in indices:
                indices.append(index)
        existing["chunk_indices"] = indices
    incoming_status = incoming.get("provenance_status")
    if incoming_status and (
        not existing.get("provenance_status") or incoming_status == "evidence"
    ):
        existing["provenance_status"] = incoming_status


def deduplicate_chunk_graphs(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Merge graph provenance first, then recover valid raw-only relationships."""
    nodes_by_uid: dict[str, dict[str, Any]] = {}
    relationships: list[dict[str, Any]] = []
    relationships_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

    for position, item in enumerate(items):
        graph = item.get("graph") or {}
        chunk_index = item.get("index", position)
        for node in graph.get("nodes", []):
            uid = str(node.get("uid") or "")
            if not uid:
                continue
            # 投影节点自带证据回溯的 chunk_indices（跨 chunk 闭包拷贝也保留来源），
            # 合并时必须沿用；仅当节点未携带来源信息时才回退到所在 item 的下标。
            projected_indices = node.get("chunk_indices")
            source_indices = (
                list(dict.fromkeys(projected_indices))
                if isinstance(projected_indices, list)
                else [chunk_index]
            )
            if uid not in nodes_by_uid:
                nodes_by_uid[uid] = {
                    **node,
                    "properties": dict(node.get("properties") or {}),
                    "chunk_indices": source_indices,
                }
            else:
                nodes_by_uid[uid]["properties"] = merge_node_properties(
                    nodes_by_uid[uid].setdefault("properties", {}),
                    node.get("properties") or {},
                )
                chunk_indices = nodes_by_uid[uid].setdefault("chunk_indices", [])
                for source_index in source_indices:
                    if source_index not in chunk_indices:
                        chunk_indices.append(source_index)

    valid_uids = set(nodes_by_uid)

    relationship_candidates: list[dict[str, Any]] = []
    for item in items:
        graph = item.get("graph") or {}
        relationship_candidates.extend(graph.get("relationships", []))
    # Each item may repeat document-level raw. Inspect every projected graph
    # before raw fallback so a bare copy cannot mask a later graph's metadata.
    for item in items:
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
            relationship_candidates.append(
                {
                    "from_uid": entity_uid(from_type, from_name, None),
                    "to_uid": entity_uid(to_type, to_name, None),
                    "type": relation_type,
                    **{
                        field: deepcopy(relation[field])
                        for field in ("evidence", "evidences", "chunk_indices", "provenance_status")
                        if field in relation
                    },
                }
            )

    for relationship in filter_relationships_with_known_nodes(
        relationship_candidates,
        valid_uids,
    ):
        key = (
            str(relationship.get("from_uid") or ""),
            str(relationship.get("to_uid") or ""),
            str(relationship.get("type") or ""),
        )
        if key in relationships_by_key:
            _merge_relationship_provenance(relationships_by_key[key], relationship)
            continue
        merged_relationship = deepcopy(relationship)
        relationships_by_key[key] = merged_relationship
        relationships.append(merged_relationship)

    return {"nodes": list(nodes_by_uid.values()), "relationships": relationships}
