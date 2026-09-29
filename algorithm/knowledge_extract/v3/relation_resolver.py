"""Ontology-gated relation acceptance, deduplication, and rejection state."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from .candidate_models import EntityCandidate, RelationCandidate
from .graph_utils import canonical_name, canonical_relation_name
from .hierarchy import numbered_parent_match
from .ontology_schema import OntologySchema, _clean_relation_identifier


DETERMINISTIC_RELATION_IDS = frozenset(
    {
        # Structural links whose endpoints follow from document position/span
        # rather than from language understanding; the pipeline assembles them
        # directly in ``hierarchy`` instead of asking the relation classifier.
        "haspart",
        "hasnote",
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
    }
)


def enumerate_relation_pairs(plan: Any, entities: Iterable[Any]) -> list[dict[str, Any]]:
    """Enumerate only ontology-assignable, non-hierarchical entity pairs.

    The returned records are stable prompt contracts.  This function never
    creates or merges endpoint candidates; it merely describes legal choices
    for the relation classifier.
    """
    rows: list[dict[str, Any]] = []
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
    seen: set[tuple[str, str, str, str]] = set()

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

    for specs in plan.relation_specs.values():
        for spec in specs:
            # relation_specs is keyed by canonical relation ID; use the same
            # stable spelling in the pair contract.
            relation_id = canonical_name(spec.relation_id)
            if canonical_relation_name(relation_id) in DETERMINISTIC_RELATION_IDS:
                continue
            sources = rows_assignable_to(spec.source)
            if not sources:
                continue
            targets = rows_assignable_to(spec.target)
            if not targets:
                continue
            signature = spec.signature
            for source in sources:
                for target in targets:
                    if source["id"] == target["id"]:
                        continue
                    key = (source["id"], relation_id, target["id"], signature)
                    if key in seen:
                        continue
                    seen.add(key)
                    raw_key = "|".join(key)
                    pair_id = "pair-" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
                    rows.append(
                        {
                            "pair_id": pair_id,
                            "source_id": source["id"],
                            "target_id": target["id"],
                            "source_type": source["type"],
                            "target_type": target["type"],
                            "source_name": source["name"],
                            "target_name": target["name"],
                            "relation_id": relation_id,
                            "relation_label": spec.display_label or relation_id,
                            "signature": signature,
                        }
                    )
    rows.sort(key=lambda row: (row["source_id"], row["relation_id"], row["target_id"], row["pair_id"]))
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


def _cardinality_upper_bound(cardinality: str) -> int | None:
    """Parse a finite UML cardinality upper bound; ``None`` means unbounded."""
    value = (cardinality or "").strip()
    if not value or value == "*":
        return None
    upper = value.rsplit("..", 1)[-1].strip()
    if upper == "*":
        return None
    try:
        parsed = int(upper)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _evidence_key(evidence: Any) -> tuple[Any, ...]:
    return (
        str(getattr(evidence, "span_id", "") or ""),
        tuple(str(item) for item in (getattr(evidence, "source_block_ids", ()) or ())),
        int(getattr(evidence, "start_char", -1) or 0),
        int(getattr(evidence, "end_char", -1) or 0),
        str(getattr(evidence, "quote", "") or ""),
        str(getattr(evidence, "evidence_type", "") or ""),
    )


def _merge_evidence(existing: RelationCandidate, incoming: Iterable[Any]) -> None:
    by_key = {_evidence_key(evidence): evidence for evidence in existing.evidence}
    for evidence in incoming:
        by_key.setdefault(_evidence_key(evidence), evidence)
    existing.evidence = [by_key[key] for key in sorted(by_key)]


class RelationResolver:
    """Accept only relations whose IDs, endpoints, direction, and cardinality fit."""

    def __init__(self, schema: OntologySchema, entities_by_id: dict[str, EntityCandidate]):
        self.schema = schema
        self.entities_by_id = entities_by_id
        self._eligible_relations: list[RelationCandidate] = []
        self._relations: list[RelationCandidate] = []
        self._relation_by_key: dict[tuple[str, str, str], RelationCandidate] = {}
        self._signature_by_id: dict[str, Any] = {}
        self._cardinality_rejected_ids: set[str] = set()
        self._rejections: list[dict[str, Any]] = []
        self._next_id = 0
        self._endpoint_remap_count = 0

    @property
    def candidate_count(self) -> int:
        return len(self._eligible_relations)

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
        if (
            from_entity.mapping_status != "accepted"
            or to_entity.mapping_status != "accepted"
            or from_entity.lifecycle == "QUARANTINED"
            or to_entity.lifecycle == "QUARANTINED"
        ):
            return self._reject(
                candidate,
                code="RELATION_ENDPOINT_QUARANTINED",
                message=(
                    f"关系 {candidate.proposed_relation!r} 的端点未通过身份准入: "
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
            _merge_evidence(existing, candidate.evidence)
            self._recompute_cardinality(warnings=warnings, trigger=existing)
            return existing if existing.mapping_status == "accepted" else None

        candidate.candidate_id = f"relation-{self._next_id}"
        self._next_id += 1
        self._eligible_relations.append(candidate)
        self._relation_by_key[key] = candidate
        self._signature_by_id[candidate.candidate_id] = signature
        self._recompute_cardinality(warnings=warnings, trigger=candidate)
        return candidate if candidate.mapping_status == "accepted" else None

    def accepted(self) -> list[RelationCandidate]:
        return [relation for relation in self._relations if relation.mapping_status == "accepted"]

    def candidates(self) -> list[RelationCandidate]:
        """Return a snapshot of all resolved candidates, including rejections."""
        return list(self._eligible_relations)

    def recompute_cardinality(
        self,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> None:
        """Re-run cardinality after external mapping scores/statuses are assigned."""
        self._recompute_cardinality(warnings=warnings, trigger=None)

    def remap_endpoints(
        self,
        remap: dict[str, str],
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> None:
        """Rewrite old entity IDs and collapse relations that become duplicates."""
        rebuilt: list[RelationCandidate] = []
        by_key: dict[tuple[str, str, str], RelationCandidate] = {}
        rebuilt_signatures: dict[str, Any] = {}
        for relation in self._eligible_relations:
            old_from = relation.from_candidate_id
            old_to = relation.to_candidate_id
            relation.from_candidate_id = remap.get(old_from, old_from)
            relation.to_candidate_id = remap.get(old_to, old_to)
            self._endpoint_remap_count += int(old_from != relation.from_candidate_id)
            self._endpoint_remap_count += int(old_to != relation.to_candidate_id)
            from_entity = self.entities_by_id.get(relation.from_candidate_id)
            to_entity = self.entities_by_id.get(relation.to_candidate_id)
            if from_entity is None or to_entity is None:
                relation.mapping_status = "rejected"
                continue
            relation.from_name = from_entity.name
            relation.from_type = from_entity.canonical_type or ""
            relation.to_name = to_entity.name
            relation.to_type = to_entity.canonical_type or ""
            key = (
                relation.from_candidate_id,
                relation.canonical_relation or relation.proposed_relation,
                relation.to_candidate_id,
            )
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = relation
                rebuilt.append(relation)
                if relation.candidate_id in self._signature_by_id:
                    rebuilt_signatures[relation.candidate_id] = self._signature_by_id[relation.candidate_id]
                continue
            _merge_evidence(existing, relation.evidence)
        self._eligible_relations = rebuilt
        self._relation_by_key = by_key
        self._signature_by_id = rebuilt_signatures
        self._cardinality_rejected_ids.intersection_update(rebuilt_signatures)
        retained_ids = set(rebuilt_signatures)
        self._rejections = [
            rejection
            for rejection in self._rejections
            if not (
                rejection.get("code") == "RELATION_CARDINALITY_EXCEEDED"
                and str((rejection.get("value") or {}).get("candidate_id") or "")
                and str((rejection.get("value") or {}).get("candidate_id") or "")
                not in retained_ids
            )
        ]
        self._recompute_cardinality(warnings=warnings, trigger=None)

    @property
    def endpoint_remap_count(self) -> int:
        return self._endpoint_remap_count

    def unresolved_rejections(self) -> list[dict[str, Any]]:
        accepted = self.accepted()
        accepted_triples = {
            (
                relation.from_candidate_id,
                canonical_name(relation.canonical_relation),
                relation.to_candidate_id,
            )
            for relation in accepted
        }
        unresolved: list[dict[str, Any]] = []
        for rejection in self._rejections:
            value = rejection.get("value") or {}
            proposed = canonical_name(_clean_relation_identifier(value.get("rel_type")))
            triple = (
                str(value.get("from_id") or ""),
                proposed,
                str(value.get("to_id") or ""),
            )
            if proposed and triple in accepted_triples:
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

    def _cardinality_violation(
        self,
        candidate: RelationCandidate,
        signature: Any,
        accepted: Iterable[RelationCandidate],
    ) -> str:
        signature_key = (
            canonical_relation_name(signature.identifier),
            canonical_name(signature.source),
            canonical_name(signature.target),
        )

        def same_signature(other: RelationCandidate) -> bool:
            other_signature = self._signature_by_id.get(other.candidate_id)
            if other_signature is None:
                return False
            return (
                canonical_relation_name(other_signature.identifier),
                canonical_name(other_signature.source),
                canonical_name(other_signature.target),
            ) == signature_key

        target_limit = _cardinality_upper_bound(signature.target_cardinality)
        if target_limit is not None:
            targets = {
                other.to_candidate_id
                for other in accepted
                if other.from_candidate_id == candidate.from_candidate_id
                and same_signature(other)
            }
            if candidate.to_candidate_id not in targets and len(targets) >= target_limit:
                return (
                    f"关系 {candidate.canonical_relation} 目标端基数 "
                    f"{signature.target_cardinality} 超出上限，不能接受更多目标端点"
                )
        source_limit = _cardinality_upper_bound(signature.source_cardinality)
        if source_limit is not None:
            sources = {
                other.from_candidate_id
                for other in accepted
                if other.to_candidate_id == candidate.to_candidate_id
                and same_signature(other)
            }
            if candidate.from_candidate_id not in sources and len(sources) >= source_limit:
                return (
                    f"关系 {candidate.canonical_relation} 源端基数 "
                    f"{signature.source_cardinality} 超出上限，不能接受更多源端点"
                )
        return ""

    @staticmethod
    def _relation_preference(candidate: RelationCandidate) -> tuple[Any, ...]:
        """Return an order-independent quality key for cardinality arbitration.

        Evidence is already source-bound before candidates reach this boundary.
        Prefer a mapping score when the post-scoring arbitration pass has
        assigned one, then quotes that name both endpoints, explicit evidence,
        evidence diversity, and useful quote content.  Before scoring, every
        candidate has the same sentinel score.  The exact triple is the stable
        tie-breaker.
        """
        from_key = canonical_name(candidate.from_name)
        to_key = canonical_name(candidate.to_name)
        evidence_rows = {
            (
                evidence.span_id,
                tuple(evidence.source_block_ids),
                evidence.quote,
                evidence.evidence_type,
            )
            for evidence in candidate.evidence
        }
        endpoint_coverage: list[int] = []
        explicit_count = 0
        quote_chars = 0
        for _, _, quote, evidence_type in evidence_rows:
            quote_key = canonical_name(quote)
            endpoint_coverage.append(
                int(bool(from_key) and from_key in quote_key)
                + int(bool(to_key) and to_key in quote_key)
            )
            explicit_count += int(evidence_type == "explicit")
            quote_chars += len(quote.strip())
        triple = (
            candidate.from_candidate_id,
            candidate.canonical_relation or candidate.proposed_relation,
            candidate.to_candidate_id,
        )
        score = candidate.mapping_score if candidate.mapping_score is not None else -1.0
        return (
            -float(score),
            -max(endpoint_coverage, default=0),
            -sum(coverage == 2 for coverage in endpoint_coverage),
            -explicit_count,
            -len(evidence_rows),
            -quote_chars,
            triple,
        )

    def _recompute_cardinality(
        self,
        *,
        warnings: list[dict[str, Any]] | None,
        trigger: RelationCandidate | None,
    ) -> None:
        """Rebuild the accepted set from every structurally valid candidate."""
        previously_accepted = {
            relation.candidate_id
            for relation in self._relations
            if relation.mapping_status == "accepted"
        }
        managed = previously_accepted | self._cardinality_rejected_ids
        selectable = [
            relation
            for relation in self._eligible_relations
            if (
                relation.mapping_status == "accepted"
                or (
                    relation.candidate_id in managed
                    and not {
                        "MAPPING_REJECTED",
                        "MAPPING_LOW_CONFIDENCE",
                    }.intersection(relation.warnings)
                )
            )
        ]
        selected: list[RelationCandidate] = []
        rejected: dict[str, str] = {}
        for relation in sorted(selectable, key=self._relation_preference):
            signature = self._signature_by_id.get(relation.candidate_id)
            if signature is None:
                continue
            message = self._cardinality_violation(relation, signature, selected)
            if message:
                rejected[relation.candidate_id] = message
                continue
            selected.append(relation)

        selected_ids = {relation.candidate_id for relation in selected}
        revived_ids = self._cardinality_rejected_ids & selected_ids
        for relation in self._eligible_relations:
            relation_id = relation.candidate_id
            if relation_id in selected_ids:
                if relation_id in managed:
                    relation.mapping_status = "accepted"
                    relation.warnings = [
                        code for code in relation.warnings
                        if code not in {"RELATION_CARDINALITY_EXCEEDED", "RELATION_CARDINALITY_SUPERSEDED"}
                    ]
                continue
            if relation_id not in rejected:
                continue
            relation.mapping_status = "rejected"
            if "RELATION_CARDINALITY_EXCEEDED" not in relation.warnings:
                relation.warnings.append("RELATION_CARDINALITY_EXCEEDED")
            newly_rejected = relation_id not in self._cardinality_rejected_ids
            if not newly_rejected:
                continue
            code = (
                "RELATION_CARDINALITY_EXCEEDED"
                if relation is trigger
                else "RELATION_CARDINALITY_SUPERSEDED"
            )
            if code == "RELATION_CARDINALITY_SUPERSEDED":
                relation.warnings.append(code)
            message = rejected[relation_id]
            rejection = {
                "code": "RELATION_CARDINALITY_EXCEEDED",
                "message": message,
                "path": "relations",
                "value": {
                    "candidate_id": relation_id,
                    "from_id": relation.from_candidate_id,
                    "to_id": relation.to_candidate_id,
                    "rel_type": relation.proposed_relation,
                },
            }
            self._rejections.append(rejection)
            _append_warning(warnings, code, message, "relations", rejection["value"])

        if revived_ids:
            self._rejections = [
                rejection
                for rejection in self._rejections
                if not (
                    rejection.get("code") == "RELATION_CARDINALITY_EXCEEDED"
                    and str((rejection.get("value") or {}).get("candidate_id") or "") in revived_ids
                )
            ]
            if warnings is not None:
                warnings[:] = [
                    warning
                    for warning in warnings
                    if not (
                        warning.get("code")
                        in {"RELATION_CARDINALITY_EXCEEDED", "RELATION_CARDINALITY_SUPERSEDED"}
                        and str((warning.get("value") or {}).get("candidate_id") or "")
                        in revived_ids
                    )
                ]
        self._cardinality_rejected_ids = set(rejected)
        self._relations = sorted(selected, key=self._relation_preference)


__all__ = [
    "DETERMINISTIC_RELATION_IDS",
    "RelationResolver",
    "enumerate_relation_pairs",
]
