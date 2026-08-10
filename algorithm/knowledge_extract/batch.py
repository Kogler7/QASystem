from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .config import get_llm_config
from .extractor import extract_v2
from .graph_utils import (
    coerce_non_empty_name,
    entity_uid,
    filter_relationships_with_known_nodes,
    merge_node_properties,
)


def extract_chunks(
    ontology: str,
    chunks: list[str],
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    document_type: str = "auto",
    target_types: list[str] | None = None,
    validation_mode: str = "strict",
    extractor: Callable[..., dict[str, Any]] = extract_v2,
    max_workers: int | None = None,
) -> list[dict[str, Any]]:
    """Extract every chunk concurrently while preserving source order and index."""
    if not chunks:
        return []

    configured_workers = int(get_llm_config().get("max_concurrency", 4))
    worker_count = max(1, min(len(chunks), max_workers or configured_workers))

    def run(item: tuple[int, str]) -> dict[str, Any]:
        index, text = item
        try:
            result = extractor(
                ontology=ontology,
                text=text,
                model=model,
                base_url=base_url,
                api_key=api_key,
                document_type=document_type,
                target_types=target_types,
                validation_mode=validation_mode,
            )
        except Exception as exc:
            result = {
                "raw": {},
                "graph": {"nodes": [], "relationships": []},
                "_error": "知识抽取调用异常",
                "error_code": "EXTRACTION_EXCEPTION",
                "attempts": [{"attempt": 1, "type": "exception", "message": str(exc)}],
            }
        return {
            "index": index,
            "text": text,
            "raw": result.get("raw", {}),
            "graph": result.get("graph") or {"nodes": [], "relationships": []},
            "attempts": result.get("attempts", []),
            "warnings": result.get("warnings", []),
            "profile": result.get("profile", {}),
            "error": result.get("_error") or result.get("error"),
            "error_code": result.get("error_code"),
            "raw_response": result.get("_raw_response", ""),
        }

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="kg-extract") as executor:
        return list(executor.map(run, enumerate(chunks)))


def deduplicate_chunk_graphs(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Merge nodes first, then recover valid cross-chunk raw relationships."""
    nodes_by_uid: dict[str, dict[str, Any]] = {}
    relationships: list[dict[str, Any]] = []
    relationship_keys: set[tuple[str, str, str]] = set()

    for position, item in enumerate(items):
        graph = item.get("graph") or {}
        chunk_index = item.get("index", position)
        for node in graph.get("nodes", []):
            uid = str(node.get("uid") or "")
            if not uid:
                continue
            if uid not in nodes_by_uid:
                nodes_by_uid[uid] = {
                    **node,
                    "properties": dict(node.get("properties") or {}),
                    "chunk_indices": [chunk_index],
                }
            else:
                nodes_by_uid[uid]["properties"] = merge_node_properties(
                    nodes_by_uid[uid].setdefault("properties", {}),
                    node.get("properties") or {},
                )
                chunk_indices = nodes_by_uid[uid].setdefault("chunk_indices", [])
                if chunk_index not in chunk_indices:
                    chunk_indices.append(chunk_index)

    valid_uids = set(nodes_by_uid)

    relationship_candidates: list[dict[str, Any]] = []
    for item in items:
        graph = item.get("graph") or {}
        relationship_candidates.extend(graph.get("relationships", []))
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
        if key in relationship_keys:
            continue
        relationship_keys.add(key)
        relationships.append(relationship)

    return {"nodes": list(nodes_by_uid.values()), "relationships": relationships}
