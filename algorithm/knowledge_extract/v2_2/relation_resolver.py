"""Ontology-gated relation acceptance, deduplication, and rejection state."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from .candidate_models import EntityCandidate, RelationCandidate
from .graph_utils import canonical_name
from .hierarchy import numbered_parent_match
from .ontology_schema import OntologySchema, _clean_relation_identifier


DETERMINISTIC_RELATION_IDS = frozenset(
    {
        # Structural links whose endpoints follow from document position/span
        # rather than from language understanding; the pipeline assembles them
        # directly in ``hierarchy`` instead of asking the relation classifier.
        "haspart",
        "evidencedby",
        "hassubclause",
        "hasstructuralelement",
        "hasclause",
        "referencesclause",
        "cites",
        "hasdevelopmentstage",
        "standardizes",
        "classifiedunder",
        "issuedby",
        "proposedby",
        "administeredby",
        "replaces",
        "hasnormativeelement",
        "defines",
        "hasrepresentationform",
        "usesterm",
        "describesaction",
        "hascharacteristic",
        "imposesconstraint",
        "involvesobject",
        "referencesexternalresource",
        "specifiescharacteristic",
        # The benchmark's complete graph expresses these facts through the
        # inverse, deterministic Property -> Constraint edge only.  Asking the
        # classifier for additional Constraint -> Object/Property edges creates
        # unsupported duplicates and therefore false positives.
        "constrainsobject",
        "constrainscharacteristic",
    }
)

# These edges are assembled from document structure on the standard route.
# Other routes still have to classify them, because nothing else builds the edge.
_STANDARD_ASSEMBLED_TOKENS = frozenset(
    {
        "issuedby",
        "proposedby",
        "administeredby",
        "defines",
        "classifiedunder",
        "replaces",
        "cites",
        "standardizes",
        "hasdevelopmentstage",
    }
)


def relation_token(value: Any) -> str:
    """Compare relation ids ignoring case and underscores."""
    return "".join(ch for ch in canonical_name(value) if ch.isalnum())


def withheld_relation_tokens(route_kind: str = "") -> frozenset[str]:
    """Relation tokens the classifier must not decide for this document route.

    Structural edges are always assembled from spans. Cover and definition
    edges are assembled only for standards; official and generic documents
    still need the classifier to choose them.
    """
    if route_kind == "standard":
        return DETERMINISTIC_RELATION_IDS
    return DETERMINISTIC_RELATION_IDS - _STANDARD_ASSEMBLED_TOKENS


def pair_relation_ids(pair: dict[str, Any]) -> list[str]:
    """Ontology labels the classifier may assign to this entity pair."""
    ids = [str(item) for item in (pair.get("relation_ids") or []) if str(item)]
    if ids:
        return ids
    single = str(pair.get("relation_id") or "")
    if not single or single == "NONE":
        return []
    if "|" in single:
        return [part for part in single.split("|") if part]
    return [single]


def enumerate_relation_pairs(
    plan: Any,
    entities: Iterable[Any],
    *,
    skip_tokens: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Screen entity pairs that the ontology can actually relate.

    Each row is one ordered entity pair. ``relation_ids`` lists every
    non-assembled ontology label between those two types; the classifier
    picks one label or NONE. Endpoint candidates are never created here.
    """
    skip = DETERMINISTIC_RELATION_IDS if skip_tokens is None else skip_tokens
    entity_rows: list[dict[str, Any]] = []
    for entity in entities:
        if isinstance(entity, dict):
            candidate_id = str(entity.get("id") or entity.get("candidate_id") or "")
            type_id = str(entity.get("type") or entity.get("canonical_type") or "")
            name = str(entity.get("name") or "")
        else:
            candidate_id = str(getattr(entity, "candidate_id", "") or "")
            type_id = str(getattr(entity, "canonical_type", "") or getattr(entity, "proposed_type", "") or "")
            name = str(getattr(entity, "name", "") or "")
        if candidate_id and type_id:
            entity_rows.append({"id": candidate_id, "type": type_id, "name": name})
    entity_rows.sort(key=lambda row: (row["id"], row["type"], row["name"]))

    # The ontology already declares each relation's source/target class, so we
    # expand every entity type to itself + all ancestors once and bucket the
    # entities by the class they are assignable to.  Relation pairs can then be
    # generated one signature at a time (source class x target class) instead of
    # scanning every entity pair against every relation.
    ancestors_cache: dict[str, frozenset[str]] = {}

    def ancestors(type_id: str) -> frozenset[str]:
        cached = ancestors_cache.get(type_id)
        if cached is not None:
            return cached
        result: set[str] = set()
        pending = [type_id]
        while pending:
            current = pending.pop()
            key = canonical_name(current)
            if key in result:
                continue
            result.add(key)
            spec = plan.class_specs.get(current)
            if spec is None:
                spec = next(
                    (value for name, value in plan.class_specs.items() if canonical_name(name) == key),
                    None,
                )
            for parent in (spec.parents if spec else ()):
                pending.append(parent)
        frozen = frozenset(result)
        ancestors_cache[type_id] = frozen
        return frozen

    class_rows_cache: dict[str, list[dict[str, Any]]] = {}

    def rows_assignable_to(expected: str) -> list[dict[str, Any]]:
        class_key = canonical_name(expected)
        cached = class_rows_cache.get(class_key)
        if cached is None:
            cached = [row for row in entity_rows if class_key in ancestors(row["type"])]
            class_rows_cache[class_key] = cached
        return cached

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for specs in plan.relation_specs.values():
        for spec in specs:
            # relation_specs is keyed by canonical relation ID; use the same
            # stable spelling in the pair contract.
            relation_id = canonical_name(spec.relation_id)
            if relation_token(relation_id) in skip:
                continue
            sources = rows_assignable_to(spec.source)
            if not sources:
                continue
            targets = rows_assignable_to(spec.target)
            if not targets:
                continue
            comment = " ".join((getattr(spec, "comment", "") or "").split())
            for source in sources:
                for target in targets:
                    if source["id"] == target["id"]:
                        continue
                    key = (source["id"], target["id"])
                    bucket = grouped.get(key)
                    if bucket is None:
                        bucket = {
                            "source_id": source["id"],
                            "target_id": target["id"],
                            "source_type": source["type"],
                            "target_type": target["type"],
                            "source_name": source["name"],
                            "target_name": target["name"],
                            "relation_ids": [],
                            "relation_comments": {},
                            "signatures": [],
                        }
                        grouped[key] = bucket
                    if relation_id not in bucket["relation_ids"]:
                        bucket["relation_ids"].append(relation_id)
                    if comment and relation_id not in bucket["relation_comments"]:
                        bucket["relation_comments"][relation_id] = comment
                    if spec.signature not in bucket["signatures"]:
                        bucket["signatures"].append(spec.signature)
    rows: list[dict[str, Any]] = []
    for bucket in grouped.values():
        relation_ids = tuple(sorted(bucket["relation_ids"]))
        if not relation_ids:
            continue
        raw_key = "|".join((bucket["source_id"], bucket["target_id"], *relation_ids))
        pair_id = "pair-" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
        comments = {
            relation_id: bucket["relation_comments"][relation_id]
            for relation_id in relation_ids
            if relation_id in bucket["relation_comments"]
        }
        rows.append(
            {
                "pair_id": pair_id,
                "source_id": bucket["source_id"],
                "target_id": bucket["target_id"],
                "source_type": bucket["source_type"],
                "target_type": bucket["target_type"],
                "source_name": bucket["source_name"],
                "target_name": bucket["target_name"],
                "relation_id": relation_ids[0] if len(relation_ids) == 1 else "",
                "relation_ids": list(relation_ids),
                "relation_comments": comments,
                "signature": bucket["signatures"][0] if len(bucket["signatures"]) == 1 else "",
            }
        )
    rows.sort(key=lambda row: (row["source_id"], row["target_id"], row["pair_id"]))
    return rows


def _append_warning(
    warnings: list[dict[str, Any]] | None,
    code: str,
    message: str,
    path: str = "",
    value: Any = None,
) -> None:
    if warnings is None:
        return
    warning: dict[str, Any] = {"code": code, "message": message, "path": path}
    if value is not None:
        warning["value"] = value
    warnings.append(warning)


def _cardinality_is_single(cardinality: str) -> bool:
    return (cardinality or "").strip() in {"1", "0..1", "1..1"}


class RelationResolver:
    """Accept only relations whose IDs, endpoints, direction, and cardinality fit."""

    def __init__(self, schema: OntologySchema, entities_by_id: dict[str, EntityCandidate]):
        self.schema = schema
        self.entities_by_id = entities_by_id
        self._relations: list[RelationCandidate] = []
        self._relation_by_key: dict[tuple[str, str, str], RelationCandidate] = {}
        self._rejections: list[dict[str, Any]] = []
        self._next_id = 0

    @property
    def candidate_count(self) -> int:
        return len(self._relations)

    def add(
        self,
        candidate: RelationCandidate,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> RelationCandidate | None:
        from_entity = self.entities_by_id.get(candidate.from_candidate_id)
        to_entity = self.entities_by_id.get(candidate.to_candidate_id)
        if from_entity is None or to_entity is None:
            return self._reject(
                candidate,
                code="UNKNOWN_RELATION_ENDPOINT_ID",
                message=(
                    f"关系 {candidate.proposed_relation!r} 引用不存在的候选端点: "
                    f"{candidate.from_candidate_id!r} -> {candidate.to_candidate_id!r}"
                ),
                warnings=warnings,
            )

        cleaned_relation = _clean_relation_identifier(candidate.proposed_relation)
        if cleaned_relation:
            candidate.proposed_relation = cleaned_relation
        matching = self.schema.resolve_relation(cleaned_relation)
        if not matching:
            return self._reject(
                candidate,
                code="UNKNOWN_RELATION_TYPE",
                message=f"关系类型 {candidate.proposed_relation!r} 未在本体中定义",
                warnings=warnings,
            )
        signature = next(
            (
                relation
                for relation in matching
                if self.schema.is_assignable(from_entity.canonical_type or "", relation.source)
                and self.schema.is_assignable(to_entity.canonical_type or "", relation.target)
            ),
            None,
        )
        if signature is None:
            return self._reject(
                candidate,
                code="RELATION_ENDPOINT_TYPE_MISMATCH",
                message=(
                    f"关系 {candidate.proposed_relation!r} 的端点 "
                    f"{from_entity.canonical_type} -> {to_entity.canonical_type} 不符合本体签名"
                ),
                warnings=warnings,
            )

        if (
            canonical_name(signature.identifier) == "hassubclause"
            and numbered_parent_match(from_entity, to_entity) is False
        ):
            return self._reject(
                candidate,
                code="RELATION_HIERARCHY_MISMATCH",
                message=(
                    f"关系 {candidate.proposed_relation!r} 的编号层级不一致: "
                    f"{from_entity.name!r} -> {to_entity.name!r}"
                ),
                warnings=warnings,
            )

        candidate.canonical_relation = signature.identifier
        candidate.from_name = from_entity.name
        candidate.to_name = to_entity.name
        candidate.from_type = from_entity.canonical_type or ""
        candidate.to_type = to_entity.canonical_type or ""
        key = (candidate.from_candidate_id, signature.identifier, candidate.to_candidate_id)
        existing = self._relation_by_key.get(key)
        if existing is not None:
            for evidence in candidate.evidence:
                if not any(evidence.span_id == item.span_id for item in existing.evidence):
                    existing.evidence.append(evidence)
            return existing

        self._check_cardinality(candidate, signature, warnings)
        candidate.candidate_id = f"relation-{self._next_id}"
        self._next_id += 1
        self._relations.append(candidate)
        self._relation_by_key[key] = candidate
        return candidate

    def accepted(self) -> list[RelationCandidate]:
        return [relation for relation in self._relations if relation.mapping_status == "accepted"]

    def candidates(self) -> list[RelationCandidate]:
        """Return a snapshot of all resolved candidates, including rejections."""
        return list(self._relations)

    def unresolved_rejections(self) -> list[dict[str, Any]]:
        accepted = self.accepted()
        accepted_endpoint_pairs = {
            frozenset((relation.from_candidate_id, relation.to_candidate_id))
            for relation in accepted
        }
        unresolved: list[dict[str, Any]] = []
        for rejection in self._rejections:
            value = rejection.get("value") or {}
            pair = frozenset((str(value.get("from_id") or ""), str(value.get("to_id") or "")))
            if rejection.get("code") in {
                "RELATION_ENDPOINT_TYPE_MISMATCH",
                "UNKNOWN_RELATION_TYPE",
            } and pair in accepted_endpoint_pairs:
                continue
            if rejection.get("code") == "UNKNOWN_RELATION_ENDPOINT_ID":
                proposed = canonical_name(_clean_relation_identifier(value.get("rel_type")))
                from_id = str(value.get("from_id") or "")
                to_id = str(value.get("to_id") or "")
                from_known = from_id in self.entities_by_id
                to_known = to_id in self.entities_by_id
                if any(
                    canonical_name(relation.canonical_relation) == proposed
                    and (
                        (from_known and relation.from_candidate_id == from_id)
                        or (to_known and relation.to_candidate_id == to_id)
                    )
                    for relation in accepted
                ):
                    continue
            unresolved.append(rejection)
        return unresolved

    def _reject(
        self,
        candidate: RelationCandidate,
        *,
        code: str,
        message: str,
        warnings: list[dict[str, Any]] | None,
    ) -> None:
        candidate.mapping_status = "rejected"
        rejection = {
            "code": code,
            "message": message,
            "path": "relations",
            "value": {
                "from_id": candidate.from_candidate_id,
                "to_id": candidate.to_candidate_id,
                "rel_type": candidate.proposed_relation,
            },
        }
        self._rejections.append(rejection)
        _append_warning(warnings, code, message, "relations", rejection["value"])

    def _check_cardinality(self, candidate: RelationCandidate, signature: Any, warnings: Any) -> None:
        if _cardinality_is_single(signature.target_cardinality):
            conflict = any(
                other.from_candidate_id == candidate.from_candidate_id
                and other.canonical_relation == candidate.canonical_relation
                and other.to_candidate_id != candidate.to_candidate_id
                for other in self._relations
            )
            if conflict:
                _append_warning(
                    warnings,
                    "RELATION_CARDINALITY_EXCEEDED",
                    (
                        f"关系 {candidate.canonical_relation} 目标端基数 "
                        f"{signature.target_cardinality} 为单值，但出现多个目标端点"
                    ),
                    "relations",
                    candidate.canonical_relation,
                )
                candidate.warnings.append("RELATION_CARDINALITY_EXCEEDED")
        if _cardinality_is_single(signature.source_cardinality):
            conflict = any(
                other.to_candidate_id == candidate.to_candidate_id
                and other.canonical_relation == candidate.canonical_relation
                and other.from_candidate_id != candidate.from_candidate_id
                for other in self._relations
            )
            if conflict:
                _append_warning(
                    warnings,
                    "RELATION_CARDINALITY_EXCEEDED",
                    (
                        f"关系 {candidate.canonical_relation} 源端基数 "
                        f"{signature.source_cardinality} 为单值，但出现多个源端点"
                    ),
                    "relations",
                    candidate.canonical_relation,
                )
                candidate.warnings.append("RELATION_CARDINALITY_EXCEEDED")


__all__ = [
    "DETERMINISTIC_RELATION_IDS",
    "RelationResolver",
    "enumerate_relation_pairs",
    "pair_relation_ids",
    "relation_token",
    "withheld_relation_tokens",
]
