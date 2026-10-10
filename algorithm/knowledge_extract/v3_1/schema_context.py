"""Compact compiled-schema context for extraction prompts.

This is a syntax/choice contract, never a source of facts. The caller keeps the
original ontology once for semantic explanations; declarations compiled by
OntologySchema determine the legal type/property/relation identifiers.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
import re
from typing import Any

from algorithm.knowledge_extract.v2.ontology_schema import OntologySchema
from .schema_types import map_value_type


def property_value_schema(datatype: str) -> dict[str, Any]:
    """Describe the JSON shape accepted by v3_1.evidence.typed.

    Null means missing, not a factual value; the prompt should prefer omission.
    Unknown scalar/enum declarations retain their original spelling elsewhere
    and allow only JSON scalars, matching the existing mechanical validator.
    No parsing/coercion or interpretation of an enum's meaning is performed.
    """
    normalized = re.sub(r"\s+", "", datatype).lower().rstrip("?")
    if normalized.endswith("[]"):
        return {"type": ["array", "null"], "items": property_value_schema(normalized[:-2])}
    if normalized.startswith("list<") and normalized.endswith(">"):
        return {"type": ["array", "null"], "items": property_value_schema(normalized[5:-1])}
    mapping_value_type = map_value_type(normalized)
    if mapping_value_type is not None:
        return {"type": ["object", "null"], "additionalProperties": (
            property_value_schema(mapping_value_type) if mapping_value_type else {})}
    if normalized in {"int", "integer", "long"}:
        return {"type": ["integer", "null"]}
    if normalized in {"float", "double", "decimal", "number"}:
        return {"type": ["number", "null"]}
    if normalized in {"bool", "boolean"}:
        return {"type": ["boolean", "null"]}
    if normalized in {"str", "string", "date", "datetime", "time"}:
        return {"type": ["string", "null"]}
    return {"type": ["string", "number", "boolean", "null"]}


def build_schema_context(
    schema: OntologySchema,
    *,
    allowed_entity_types: Iterable[str],
    candidates: Iterable[Mapping[str, Any]] = (),
    include_properties: bool = True,
    include_relations: bool = True,
) -> dict[str, Any]:
    """Build a closed identifier contract without enumerating entity pairs.

    ``candidates`` is the accepted catalog's compact ``candidate_id``/``type``
    projection. Candidate endpoint lists mean type compatibility only. Empty
    lists mean no compatible candidate is currently supplied, not that the
    source denies such a fact or that search is complete.

    Property/identity lists include inherited declarations. Relations come only
    from ``schema.relations``; ontology comments do not introduce signatures.
    Several signatures may share a relation identifier, and several relations
    may be valid choices for the same entity pair. None is mutually exclusive.

    A stage that cannot output relations omits them entirely instead of
    receiving an empty signature list, which would read as "no such relation
    is declared". Absence is part of that stage's contract, not a fact.

    Work/output scales with types, signatures and candidate memberships, not
    the Cartesian product of source and target candidates. There is no top-k
    cutoff or domain filter. Inputs and accepted candidates are not mutated.
    """
    allowed = set(allowed_entity_types)
    unknown = allowed - schema.classes_by_id.keys()
    if unknown:
        raise ValueError(f"Unknown allowed entity types: {sorted(unknown)}")

    by_type: dict[str, set[str]] = defaultdict(set)
    candidate_types: dict[str, str] = {}
    for candidate in candidates:
        candidate_id, type_id = candidate.get("candidate_id"), candidate.get("type")
        if not isinstance(candidate_id, str) or not candidate_id or not isinstance(type_id, str):
            raise ValueError("Candidates require a nonempty candidate_id and a declared type")
        if type_id not in schema.classes_by_id:
            raise ValueError(f"Unknown candidate type: {type_id}")
        if candidate_id in candidate_types and candidate_types[candidate_id] != type_id:
            raise ValueError(f"Conflicting types for candidate_id: {candidate_id}")
        candidate_types[candidate_id] = type_id
        if type_id in allowed:
            by_type[type_id].add(candidate_id)

    entity_types: list[dict[str, Any]] = []
    for type_id in sorted(allowed):
        item: dict[str, Any] = {"type": type_id}
        if include_properties:
            item["identity_fallbacks"] = list(schema.identity_properties_for(type_id))
            item["properties"] = [
                {"id": prop.identifier, "declared_type": prop.data_type,
                 "value_schema": property_value_schema(prop.data_type)}
                for prop in schema._properties_for(type_id)
            ]
        entity_types.append(item)

    context: dict[str, Any] = {"allowed_entity_types": sorted(allowed), "entity_types": entity_types}
    if not include_relations:
        return context

    # Cache each endpoint type once; do not materialize source x target pairs.
    endpoint_ids: dict[str, list[str]] = {}

    def compatible_ids(expected_type: str) -> list[str]:
        if expected_type not in endpoint_ids:
            endpoint_ids[expected_type] = sorted({
                candidate_id
                for actual_type, ids in by_type.items()
                if schema.is_assignable(actual_type, expected_type)
                for candidate_id in ids
            })
        return list(endpoint_ids[expected_type])

    context["relation_signatures"] = [
        {"from_type": rel.source, "rel_type": rel.identifier, "to_type": rel.target,
         "source_cardinality": rel.source_cardinality,
         "target_cardinality": rel.target_cardinality,
         "source_candidate_ids": compatible_ids(rel.source),
         "target_candidate_ids": compatible_ids(rel.target)}
        for rel in schema.relations
    ]
    return context
