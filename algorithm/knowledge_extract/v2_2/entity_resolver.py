"""Deterministic global entity resolution primitives."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .candidate_models import EntityCandidate, RelationCandidate
from .graph_utils import canonical_name, coerce_non_empty_name
from .relation_resolver import RelationResolver

_COREFERENCE_NAMES = {
    "该系统",
    "本系统",
    "该平台",
    "本平台",
    "上述方案",
    "该方案",
    "本方案",
    "该对象",
    "上述对象",
    "该机构",
    "该单位",
}
_ALIAS_PROPERTY_MARKERS = ("alias", "synonym", "简称", "别名", "同义")
_SOURCE_PRIORITY = {
    "markdown_structure": 1,
    "document_form": 2,
    "terminology_definition": 2,
    "standard_semantics": 2,
    "technical_semantics": 2,
    "content_element": 2,
    "normative_reference": 3,
    "document_anchor": 4,
}


@dataclass(frozen=True)
class ResolutionDecision:
    merge_into_id: str | None
    method: str
    identity_key: tuple[str, str, str]


def alias_key(value: Any) -> str:
    """Normalize deterministic typography aliases without fuzzy similarity."""
    normalized = canonical_name(value)
    return "".join(
        char
        for char in normalized
        if not unicodedata.category(char).startswith(("P", "Z"))
    )


class EntityResolver:
    def __init__(
        self,
        schema: Any,
        manager: Any | None = None,
        adjudicator: Callable[[dict[str, Any]], str | dict[str, Any]] | None = None,
    ):
        self.schema = schema
        self.manager = manager
        self.adjudicator = adjudicator
        self._exact: dict[tuple[str, str, str], str] = {}
        self._aliases: dict[tuple[str, str], set[str]] = {}
        self._blocking: dict[tuple[str, str], set[str]] = {}
        self._pair_verdicts: dict[tuple[str, str], str] = {}

    def identity_key(self, candidate: Any) -> tuple[str, str, str]:
        type_id, property_id, value = self.identity_value(candidate)
        return type_id, property_id, canonical_name(value)

    def identity_value(self, candidate: Any) -> tuple[str, str, str]:
        """Return the first ontology-declared identity value, falling back to display name."""
        type_id = str(candidate.canonical_type or candidate.proposed_type)
        for property_id in self.schema.identity_properties_for(type_id):
            value = self.property_value(candidate, property_id)
            if canonical_name(value):
                return type_id, property_id, coerce_non_empty_name(value)
        return type_id, "name", coerce_non_empty_name(candidate.name)

    def property_value(self, candidate: Any, property_id: str) -> Any:
        """Read a property by ontology id, accepting display-name keys as well."""
        properties = getattr(candidate, "properties", None) or {}
        if not isinstance(properties, dict):
            return None
        if property_id in properties:
            return properties.get(property_id)
        type_id = str(
            getattr(candidate, "canonical_type", None)
            or getattr(candidate, "proposed_type", None)
            or ""
        )
        for key, value in properties.items():
            if key == "name":
                continue
            prop = self.schema.resolve_property(type_id, key)
            if prop is not None and prop.identifier == property_id:
                return value
            if prop is None and canonical_name(key) == canonical_name(property_id):
                return value
        return None

    def canonicalize_properties(self, candidate: Any) -> dict[str, Any]:
        """Rewrite property keys to ontology identifiers when resolvable."""
        properties = getattr(candidate, "properties", None) or {}
        if not isinstance(properties, dict):
            return {}
        type_id = str(candidate.canonical_type or candidate.proposed_type or "")
        normalized: dict[str, Any] = {}
        for key, value in properties.items():
            if key == "name":
                if coerce_non_empty_name(value):
                    normalized["name"] = value
                continue
            prop = self.schema.resolve_property(type_id, key)
            target = prop.identifier if prop is not None else coerce_non_empty_name(key) or str(key)
            current = normalized.get(target)
            if current in (None, "", []):
                normalized[target] = value
            elif value in (None, "", []) or current == value:
                continue
            elif isinstance(current, list) and isinstance(value, list):
                normalized[target] = list(dict.fromkeys([*current, *value]))
        candidate.properties = normalized
        return normalized

    def decide(
        self,
        candidate: Any,
        entities_by_id: dict[str, Any],
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> ResolutionDecision:
        key = self.identity_key(candidate)
        exact = self._exact.get(key)
        if exact is not None:
            return ResolutionDecision(exact, "exact_identity", key)

        alias_matches: set[str] = set()
        for alias in self.aliases(candidate):
            alias_matches.update(self._aliases.get((key[0], alias), set()))
        alias_matches = {
            candidate_id
            for candidate_id in alias_matches
            if self._identity_compatible(key, self.identity_key(entities_by_id[candidate_id]))
        }
        if len(alias_matches) == 1:
            return ResolutionDecision(next(iter(alias_matches)), "deterministic_alias", key)

        coreference = self._resolve_coreference(candidate, entities_by_id)
        if coreference is not None:
            return ResolutionDecision(coreference, "local_coreference", key)

        adjudicated = self._resolve_ambiguous_pair(
            candidate,
            entities_by_id,
            key,
            warnings=warnings,
        )
        if adjudicated is not None:
            return ResolutionDecision(adjudicated, "pairwise_same_entity", key)
        return ResolutionDecision(None, "new_entity", key)

    def register(self, candidate: Any, *, identity_key: tuple[str, str, str] | None = None) -> None:
        key = identity_key or self.identity_key(candidate)
        self._exact[key] = candidate.candidate_id
        for alias in self.aliases(candidate):
            self._aliases.setdefault((key[0], alias), set()).add(candidate.candidate_id)
        for block in self.blocking_keys(candidate):
            self._blocking.setdefault((key[0], block), set()).add(candidate.candidate_id)

    def register_merged_aliases(self, incoming: Any, existing: Any) -> None:
        type_id = str(existing.canonical_type or existing.proposed_type)
        for alias in self.aliases(incoming):
            self._aliases.setdefault((type_id, alias), set()).add(existing.candidate_id)

    def aliases(self, candidate: Any) -> set[str]:
        aliases = {alias_key(candidate.name)}
        # Ontology ``name`` (or its display-name key) is a stable surface form,
        # independent of whatever mention string Pass 1 used.
        name_property = self.property_value(candidate, "name")
        if name_property not in (None, "", []):
            aliases.add(alias_key(name_property))
        for property_id, value in candidate.properties.items():
            marker = canonical_name(property_id)
            if not any(item in marker for item in _ALIAS_PROPERTY_MARKERS):
                continue
            values = value if isinstance(value, list) else re.split(r"[,，;；/]", str(value))
            aliases.update(alias_key(item) for item in values)
        return {item for item in aliases if item}

    def blocking_keys(self, candidate: Any) -> set[str]:
        """Return broad deterministic blocks used only to select pairwise comparisons."""
        blocks: set[str] = set()
        for alias in self.aliases(candidate):
            if len(alias) >= 4:
                blocks.add(alias[:4])
            elif len(alias) >= 2:
                blocks.add(alias[:2])
        return blocks

    @staticmethod
    def _identity_compatible(
        incoming: tuple[str, str, str],
        existing: tuple[str, str, str],
    ) -> bool:
        if incoming[0] != existing[0]:
            return False
        if incoming[1] == existing[1] != "name" and incoming[2] != existing[2]:
            return False
        return True

    def _resolve_coreference(self, candidate: Any, entities_by_id: dict[str, Any]) -> str | None:
        if coerce_non_empty_name(candidate.name) not in _COREFERENCE_NAMES or self.manager is None:
            return None
        source_chunks = {
            block_id
            for evidence in candidate.evidence
            for block_id in (evidence.source_block_ids or [])
        }
        if len(source_chunks) != 1:
            return None
        source_chunk_id = next(iter(source_chunks))
        if not self.manager.has_chunk(source_chunk_id):
            return None
        anchor = self.manager.get_chunk(source_chunk_id)
        matches: list[str] = []
        for candidate_id, entity in entities_by_id.items():
            if entity.canonical_type != candidate.canonical_type:
                continue
            for evidence in entity.evidence:
                for block_id in evidence.source_block_ids or []:
                    if not self.manager.has_chunk(block_id):
                        continue
                    chunk = self.manager.get_chunk(block_id)
                    if chunk.section_id == anchor.section_id:
                        matches.append(candidate_id)
                        break
                if candidate_id in matches:
                    break
        unique = list(dict.fromkeys(matches))
        return unique[0] if len(unique) == 1 else None

    def _resolve_ambiguous_pair(
        self,
        candidate: Any,
        entities_by_id: dict[str, Any],
        identity_key: tuple[str, str, str],
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> str | None:
        if self.adjudicator is None:
            return None
        candidate_ids: set[str] = set()
        for block in self.blocking_keys(candidate):
            candidate_ids.update(self._blocking.get((identity_key[0], block), set()))

        same_entity: list[str] = []
        for candidate_id in sorted(candidate_ids):
            existing = entities_by_id[candidate_id]
            if not self._identity_compatible(identity_key, self.identity_key(existing)):
                continue
            verdict = self._pair_verdict(existing, candidate, warnings=warnings)
            if verdict == "same_entity":
                same_entity.append(candidate_id)
        return same_entity[0] if len(same_entity) == 1 else None

    def _pair_verdict(
        self,
        existing: Any,
        incoming: Any,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> str:
        fingerprints = sorted((self._fingerprint(existing), self._fingerprint(incoming)))
        pair_key = (fingerprints[0], fingerprints[1])
        if pair_key in self._pair_verdicts:
            return self._pair_verdicts[pair_key]

        payload = {
            "type": str(incoming.canonical_type or incoming.proposed_type),
            "left": self._adjudication_candidate(existing),
            "right": self._adjudication_candidate(incoming),
            "allowed_verdicts": ["same_entity", "different_entity", "uncertain"],
        }
        try:
            raw_verdict = self.adjudicator(payload) if self.adjudicator is not None else "uncertain"
        except Exception as exc:
            _append_warning(
                warnings,
                "ENTITY_ADJUDICATION_FAILED",
                "实体判定器调用失败；候选实体保持独立",
                "entities",
                type(exc).__name__,
            )
            raw_verdict = "uncertain"
        if isinstance(raw_verdict, dict):
            raw_verdict = raw_verdict.get("verdict")
        verdict = str(raw_verdict or "uncertain").strip().casefold()
        if verdict not in {"same_entity", "different_entity", "uncertain"}:
            verdict = "uncertain"
        self._pair_verdicts[pair_key] = verdict
        return verdict

    def _fingerprint(self, candidate: Any) -> str:
        identity = self.identity_key(candidate)
        return "\x1f".join((*identity, *sorted(self.aliases(candidate))))

    @staticmethod
    def _adjudication_candidate(candidate: Any) -> dict[str, Any]:
        return {
            "name": candidate.name,
            "properties": dict(candidate.properties),
            "evidence": [item.to_dict() for item in candidate.evidence],
        }


class CandidatePool:
    """Global authority for entity identity and ontology-valid relations."""

    def __init__(
        self,
        schema: Any,
        *,
        allowed_types: Iterable[str] | None = None,
        manager: Any | None = None,
        adjudicator: Callable[[dict[str, Any]], str | dict[str, Any]] | None = None,
    ):
        self.schema = schema
        self.allowed_types = set(allowed_types or ()) or {
            ontology_class.identifier for ontology_class in schema.classes
        }
        self._entities: list[EntityCandidate] = []
        self._entity_by_id: dict[str, EntityCandidate] = {}
        self._entity_index: dict[tuple[str, str], EntityCandidate] = {}
        self._next_entity_id = 0
        self._resolver = EntityResolver(schema, manager, adjudicator)
        self._relation_resolver = RelationResolver(schema, self._entity_by_id)

    @property
    def entity_candidate_count(self) -> int:
        return len(self._entities)

    @property
    def relation_candidate_count(self) -> int:
        return self._relation_resolver.candidate_count

    def add_entity(
        self,
        candidate: EntityCandidate,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> EntityCandidate | None:
        ontology_class = self.schema.resolve_class(
            candidate.canonical_type or candidate.proposed_type
        )
        if ontology_class is None:
            _append_warning(
                warnings,
                "UNKNOWN_ENTITY_TYPE",
                f"实体类型 {candidate.proposed_type!r} 不在本体中",
                "entities",
                candidate.proposed_type,
            )
            return None
        if ontology_class.identifier not in self.allowed_types:
            _append_warning(
                warnings,
                "ENTITY_OUT_OF_SCOPE",
                f"实体类型 {ontology_class.identifier} 不在目标类型范围内",
                "entities",
                ontology_class.identifier,
            )
            return None

        candidate.canonical_type = ontology_class.identifier
        self._resolver.canonicalize_properties(candidate)
        name_key = (ontology_class.identifier, canonical_name(candidate.name))
        decision = self._resolver.decide(
            candidate,
            self._entity_by_id,
            warnings=warnings,
        )
        existing = (
            self._entity_by_id.get(decision.merge_into_id)
            if decision.merge_into_id is not None
            else None
        )
        if existing is None:
            candidate.candidate_id = f"entity-{self._next_entity_id}"
            _, candidate.identity_property, candidate.identity_value = self._resolver.identity_value(
                candidate
            )
            candidate.resolution_method = decision.method
            candidate.resolution_methods.append(decision.method)
            self._next_entity_id += 1
            self._entities.append(candidate)
            self._entity_by_id[candidate.candidate_id] = candidate
            self._entity_index[name_key] = candidate
            self._resolver.register(candidate, identity_key=decision.identity_key)
            return candidate

        self._absorb_entity(existing, candidate, method=decision.method)
        self._entity_index[name_key] = existing
        return existing

    def reconcile_identities(
        self,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> int:
        """Re-merge entities after properties change using ontology identity only.

        Pass 1 often freezes candidates under mention names before identity
        attributes are filled. Once property enrichment (or repair) supplies the
        ontology-declared identity values, group by exact identity key and absorb
        duplicates so relation enumeration does not fan out across surface forms.
        """
        for entity in self._entities:
            self._resolver.canonicalize_properties(entity)
            _, entity.identity_property, entity.identity_value = self._resolver.identity_value(
                entity
            )

        merged = 0
        groups: dict[tuple[str, str, str], list[EntityCandidate]] = {}
        for entity in self.accepted_entities():
            key = self._resolver.identity_key(entity)
            if key[1] == "name":
                # Mention-only identity is already handled at add time; do not
                # invent fuzzy merges for distinct surface forms.
                continue
            groups.setdefault(key, []).append(entity)

        for members in groups.values():
            if len(members) < 2:
                continue
            members.sort(key=lambda item: item.candidate_id)
            survivor = members[0]
            for other in members[1:]:
                if other.candidate_id not in self._entity_by_id:
                    continue
                self._absorb_entity(survivor, other, method="exact_identity_reconcile")
                self._retire_entity(other)
                merged += 1
                _append_warning(
                    warnings,
                    "ENTITY_IDENTITY_RECONCILED",
                    (
                        f"属性补全后按 identity {survivor.identity_property}="
                        f"{survivor.identity_value!r} 合并表面名 "
                        f"{other.name!r} -> {survivor.name!r}"
                    ),
                    "entities",
                    {
                        "survivor": survivor.candidate_id,
                        "absorbed": other.candidate_id,
                        "identity_property": survivor.identity_property,
                        "identity_value": survivor.identity_value,
                    },
                )

        if merged:
            self._reindex_entities()
        return merged

    def _absorb_entity(self, existing: EntityCandidate, incoming: EntityCandidate, *, method: str) -> None:
        _, incoming_identity_property, incoming_identity_value = self._resolver.identity_value(
            incoming
        )
        if _SOURCE_PRIORITY.get(incoming.source, 0) > _SOURCE_PRIORITY.get(existing.source, 0):
            existing.name = incoming.name
            existing.identity_property = incoming_identity_property
            existing.identity_value = incoming_identity_value
        if existing.identity_property == "name" and incoming_identity_property != "name":
            existing.identity_property = incoming_identity_property
            existing.identity_value = incoming_identity_value
        existing.resolution_method = method
        if method not in existing.resolution_methods:
            existing.resolution_methods.append(method)
        merge_candidate(existing, incoming)
        self._resolver.canonicalize_properties(existing)
        _, existing.identity_property, existing.identity_value = self._resolver.identity_value(
            existing
        )
        self._resolver.register(existing)
        self._resolver.register_merged_aliases(incoming, existing)
        if incoming.candidate_id and incoming.candidate_id != existing.candidate_id:
            self._relation_resolver.rewire_endpoint(incoming.candidate_id, existing.candidate_id)

    def _retire_entity(self, entity: EntityCandidate) -> None:
        entity.mapping_status = "rejected"
        entity.warnings.append("MERGED_BY_IDENTITY_RECONCILE")
        self._entity_by_id.pop(entity.candidate_id, None)
        self._entities = [
            item for item in self._entities if item.candidate_id != entity.candidate_id
        ]

    def _reindex_entities(self) -> None:
        self._entity_index = {}
        self._resolver = EntityResolver(
            self.schema,
            self._resolver.manager,
            self._resolver.adjudicator,
        )
        for entity in self._entities:
            type_id = str(entity.canonical_type or entity.proposed_type or "")
            self._entity_index[(type_id, canonical_name(entity.name))] = entity
            self._resolver.register(entity)

    def find_entity(self, type_id: str, name: str) -> EntityCandidate | None:
        ontology_class = self.schema.resolve_class(type_id)
        if ontology_class is None:
            return None
        return self._entity_index.get((ontology_class.identifier, canonical_name(name)))

    def accepted_entities(self) -> list[EntityCandidate]:
        return [entity for entity in self._entities if entity.mapping_status == "accepted"]

    def entity_candidates(self) -> list[EntityCandidate]:
        """Return a snapshot of every resolved entity candidate."""
        return list(self._entities)

    def entity_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "id": entity.candidate_id,
                "type": entity.canonical_type,
                "name": entity.name,
                "properties": {
                    key: value for key, value in entity.properties.items() if key != "name"
                },
                "source_block_ids": sorted(
                    {
                        block_id
                        for evidence in entity.evidence
                        for block_id in evidence.source_block_ids
                        if block_id
                    }
                ),
            }
            for entity in self.accepted_entities()
        ]

    def add_relation(
        self,
        candidate: RelationCandidate,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> RelationCandidate | None:
        return self._relation_resolver.add(candidate, warnings=warnings)

    def accepted_relations(self) -> list[RelationCandidate]:
        return self._relation_resolver.accepted()

    def relation_candidates(self) -> list[RelationCandidate]:
        """Return all resolved relation candidates, including rejections."""
        return self._relation_resolver.candidates()

    def unresolved_relation_rejections(self) -> list[dict[str, Any]]:
        return self._relation_resolver.unresolved_rejections()


def merge_candidate(existing: Any, incoming: Any) -> None:
    """Merge evidence and non-conflicting properties; retain conflicts internally."""
    if _SOURCE_PRIORITY.get(str(getattr(incoming, "source", "") or ""), 0) > _SOURCE_PRIORITY.get(
        str(getattr(existing, "source", "") or ""), 0
    ):
        existing.source = incoming.source
    for key, value in incoming.properties.items():
        current = existing.properties.get(key)
        if current in (None, "", []):
            existing.properties[key] = value
        elif value in (None, "", []) or current == value:
            continue
        elif isinstance(current, list) and isinstance(value, list):
            existing.properties[key] = list(dict.fromkeys([*current, *value]))
        else:
            conflict = {"property": key, "kept": current, "rejected": value}
            if conflict not in existing.property_conflicts:
                existing.property_conflicts.append(conflict)
    for evidence in incoming.evidence:
        evidence_key = (evidence.span_id, evidence.quote)
        if not any((item.span_id, item.quote) == evidence_key for item in existing.evidence):
            existing.evidence.append(evidence)
    existing.warnings.extend(incoming.warnings)


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


__all__ = [
    "CandidatePool",
    "EntityResolver",
    "ResolutionDecision",
    "alias_key",
    "merge_candidate",
]
