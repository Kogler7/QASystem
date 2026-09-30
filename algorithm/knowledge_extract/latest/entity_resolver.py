"""Deterministic global entity resolution primitives."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .candidate_models import EntityCandidate, RelationCandidate
from .graph_utils import canonical_name, canonical_relation_name, coerce_non_empty_name
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
        if getattr(candidate, "source", "") == "official_attachment" and getattr(candidate, "source_identity", ""):
            return type_id, "__source_file__", candidate.source_identity
        for property_id in self.schema.identity_properties_for(type_id):
            value = candidate.properties.get(property_id)
            if canonical_name(value):
                return type_id, property_id, coerce_non_empty_name(value)
        return type_id, "name", coerce_non_empty_name(candidate.name)

    def stable_identity_value(self, candidate: Any) -> tuple[str, str, str] | None:
        """Return ontology-declared identity only; never fall back to display name."""
        type_id = str(candidate.canonical_type or candidate.proposed_type)
        if getattr(candidate, "source", "") == "official_attachment" and getattr(candidate, "source_identity", ""):
            return type_id, "__source_file__", candidate.source_identity
        for property_id in self.schema.identity_properties_for(type_id):
            value = candidate.properties.get(property_id)
            if canonical_name(value):
                return type_id, property_id, coerce_non_empty_name(value)
        return None

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
        if getattr(candidate, "source", "") == "official_attachment":
            return ResolutionDecision(None, "source_file_identity", key)

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
        if "__source_file__" in {incoming[1], existing[1]}:
            return incoming == existing
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
        ontology_namespace: str = "ontology",
        identity_admission: str = "quarantine",
        entity_admission: Callable[[EntityCandidate], bool] | None = None,
        relation_admission: Callable[[RelationCandidate], bool] | None = None,
    ):
        self.schema = schema
        self.allowed_types = set(allowed_types or ()) or {
            ontology_class.identifier for ontology_class in schema.classes
        }
        self._entities: list[EntityCandidate] = []
        self._entity_by_id: dict[str, EntityCandidate] = {}
        self._entity_index: dict[tuple[str, str], EntityCandidate] = {}
        self._next_entity_id = 0
        self.ontology_namespace = ontology_namespace or "ontology"
        if identity_admission not in {"observe", "quarantine"}:
            raise ValueError("identity_admission 仅支持 observe 或 quarantine")
        self.identity_admission = identity_admission
        self._identity_finalized = False
        self._entity_admission = entity_admission
        self._relation_admission = relation_admission
        self._id_remap: dict[str, str] = {}
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
        if self._entity_admission is not None and not self._entity_admission(candidate):
            _append_warning(warnings, "OFFICIAL_ENTITY_ROLE_REJECTED", "实体来源不符合公文表单、正文、附件角色约束", "entities", candidate.name)
            return None
        name_key = (ontology_class.identifier, canonical_name(candidate.name))
        stable_identity = self._resolver.stable_identity_value(candidate)
        if stable_identity is not None:
            decision = self._resolver.decide(candidate, self._entity_by_id, warnings=warnings)
        else:
            # Mention-level candidates must remain independent until property
            # enrichment freezes ontology identities.  Otherwise two equal
            # mentions that later receive different IDs can no longer split.
            decision = ResolutionDecision(
                None,
                "provisional_mention",
                self._resolver.identity_key(candidate),
            )
        existing = (
            self._entity_by_id.get(decision.merge_into_id)
            if decision.merge_into_id is not None
            else None
        )
        if existing is None:
            candidate.candidate_id = f"entity-{self._next_entity_id}"
            candidate.lifecycle = "PROVISIONAL"
            candidate.mention_aliases = list(dict.fromkeys([*candidate.mention_aliases, candidate.name]))
            candidate.source_candidate_ids = list(
                dict.fromkeys([*candidate.source_candidate_ids, candidate.candidate_id])
            )
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

        candidate.resolution_method = decision.method
        _, incoming_identity_property, incoming_identity_value = self._resolver.identity_value(
            candidate
        )
        if _SOURCE_PRIORITY.get(candidate.source, 0) > _SOURCE_PRIORITY.get(existing.source, 0):
            existing.name = candidate.name
            existing.identity_property = incoming_identity_property
            existing.identity_value = incoming_identity_value
        if existing.identity_property == "name" and incoming_identity_property != "name":
            existing.identity_property = incoming_identity_property
            existing.identity_value = incoming_identity_value
        existing.resolution_method = decision.method
        if decision.method not in existing.resolution_methods:
            existing.resolution_methods.append(decision.method)
        merge_candidate(existing, candidate)
        self._entity_index[name_key] = existing
        self._resolver.register(existing)
        self._resolver.register_merged_aliases(candidate, existing)
        return existing

    def finalize_identities(
        self,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        """Freeze ontology-declared identities after property enrichment."""
        identified = unresolved = quarantined = 0
        for entity in self._entities:
            stable = self._resolver.stable_identity_value(entity)
            if stable is None:
                declared_identity = self.schema.identity_properties_for(
                    entity.canonical_type or entity.proposed_type
                )
                # A rule mention is still useful extraction output when its
                # optional RuleID was not present in the source. Keep it under
                # its generated candidate ID and make the missing identity
                # explicit; never invent a RuleID from the mention text.
                identity_type = entity.canonical_type or entity.proposed_type
                identity_property = (
                    self.schema.resolve_property(identity_type, declared_identity[0])
                    if len(declared_identity) == 1
                    else None
                )
                identity_symbols = (
                    [declared_identity[0], identity_property.identifier, identity_property.display_name]
                    if identity_property is not None
                    else list(declared_identity)
                )
                rule_id_only = (
                    len(declared_identity) == 1
                    and any(
                        canonical_relation_name(symbol) == "ruleid"
                        for symbol in identity_symbols
                    )
                )
                should_quarantine = (
                    bool(declared_identity)
                    and not rule_id_only
                    and self.identity_admission == "quarantine"
                )
                entity.lifecycle = "QUARANTINED" if should_quarantine else "UNRESOLVED"
                if should_quarantine:
                    entity.mapping_status = "rejected"
                    quarantined += 1
                    if "ENTITY_IDENTITY_QUARANTINED" not in entity.warnings:
                        entity.warnings.append("ENTITY_IDENTITY_QUARANTINED")
                        _append_warning(
                            warnings,
                            "ENTITY_IDENTITY_QUARANTINED",
                            f"实体 {entity.name!r} 缺少本体声明的稳定身份属性，已进入隔离区",
                            f"entities.{entity.candidate_id}",
                            {"identity_properties": list(declared_identity)},
                        )
                elif rule_id_only:
                    if "RULE_ID_MISSING" not in entity.warnings:
                        entity.warnings.append("RULE_ID_MISSING")
                    _append_warning(
                        warnings,
                        "RULE_ID_MISSING",
                        f"规则实体 {entity.name!r} 缺少 RuleID，保留为未解析实体并使用临时候选 ID",
                        f"entities.{entity.candidate_id}",
                        {"identity_properties": list(declared_identity)},
                    )
                entity.resolution_trace.append({
                    "stage": "identity_finalization",
                    "status": "quarantined" if should_quarantine else "unresolved",
                    "reason": "missing_ontology_identity",
                    "identity_properties": list(declared_identity),
                })
                if not should_quarantine:
                    unresolved += 1
                continue
            _, property_id, value = stable
            if "ENTITY_IDENTITY_QUARANTINED" in entity.warnings:
                entity.warnings = [
                    code for code in entity.warnings if code != "ENTITY_IDENTITY_QUARANTINED"
                ]
                if not entity.warnings:
                    entity.mapping_status = "accepted"
                entity.resolution_trace.append({
                    "stage": "identity_finalization",
                    "status": "recovered_from_quarantine",
                    "property": property_id,
                    "value": value,
                })
            entity.identity_property = property_id
            entity.identity_value = value
            entity.lifecycle = "IDENTIFIED"
            entity.resolution_trace.append({
                "stage": "identity_finalization",
                "status": "identified",
                "property": property_id,
                "value": value,
            })
            identified += 1
        self._identity_finalized = True
        return {
            "identified": identified,
            "unresolved": unresolved,
            "quarantined": quarantined,
        }

    def resolve_global(
        self,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        """Merge entities by frozen namespace/type/property/value and rebuild indexes."""
        if not self._identity_finalized:
            self.finalize_identities(warnings=warnings)
        resolved: list[EntityCandidate] = []
        by_stable_key: dict[tuple[str, str, str, str], EntityCandidate] = {}
        by_provisional_key: dict[tuple[str, str], EntityCandidate] = {}
        remap: dict[str, str] = {}
        for entity in self._entities:
            old_id = entity.candidate_id
            stable = self._resolver.stable_identity_value(entity)
            if stable is None:
                provisional_key = (
                    entity.canonical_type or entity.proposed_type,
                    canonical_name(entity.name),
                )
                existing = by_provisional_key.get(provisional_key)
                if existing is None:
                    by_provisional_key[provisional_key] = entity
                    resolved.append(entity)
                    remap[old_id] = old_id
                    continue
                merge_candidate(existing, entity)
                existing.resolution_method = "provisional_same_mention"
                if "provisional_same_mention" not in existing.resolution_methods:
                    existing.resolution_methods.append("provisional_same_mention")
                existing.resolution_trace.append({
                    "stage": "global_resolution",
                    "status": "provisional_merged",
                    "source_candidate_id": old_id,
                    "key": provisional_key,
                })
                remap[old_id] = existing.candidate_id
                continue
            type_id, property_id, value = stable
            key = (self.ontology_namespace, type_id, property_id, canonical_name(value))
            existing = by_stable_key.get(key)
            if existing is None:
                entity.lifecycle = "RESOLVED"
                entity.resolution_method = "stable_identity"
                if "stable_identity" not in entity.resolution_methods:
                    entity.resolution_methods.append("stable_identity")
                entity.resolution_trace.append({"stage": "global_resolution", "status": "resolved", "key": key})
                by_stable_key[key] = entity
                resolved.append(entity)
                remap[old_id] = old_id
                continue
            merge_candidate(existing, entity)
            existing.lifecycle = "RESOLVED"
            existing.resolution_trace.append({
                "stage": "global_resolution",
                "status": "merged",
                "source_candidate_id": old_id,
                "key": key,
            })
            remap[old_id] = existing.candidate_id
            _append_warning(
                warnings,
                "ENTITY_STABLE_IDENTITY_MERGED",
                f"实体 {old_id} 已按 stable identity 合并到 {existing.candidate_id}",
                f"entities.{old_id}",
                {"resolved_id": existing.candidate_id, "key": key},
            )

        self._entities = resolved
        self._entity_by_id.clear()
        self._entity_index.clear()
        self._resolver = EntityResolver(self.schema, self._resolver.manager, self._resolver.adjudicator)
        for entity in self._entities:
            self._entity_by_id[entity.candidate_id] = entity
            aliases = [entity.name, *entity.mention_aliases]
            for alias in aliases:
                self._entity_index[(entity.canonical_type or entity.proposed_type, canonical_name(alias))] = entity
            self._resolver.register(entity)
        self._relation_resolver.remap_endpoints(remap, warnings=warnings)
        self._id_remap = remap
        return dict(remap)

    @property
    def id_remap(self) -> dict[str, str]:
        return dict(self._id_remap)

    def resolve_candidate_id(self, candidate_id: str) -> str:
        """Resolve an ID emitted before the latest global identity pass."""
        current = candidate_id
        seen: set[str] = set()
        while current in self._id_remap and current not in seen:
            seen.add(current)
            resolved = self._id_remap[current]
            if resolved == current:
                break
            current = resolved
        return current

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
            if entity.source != "official_attachment"
        ]

    def add_relation(
        self,
        candidate: RelationCandidate,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> RelationCandidate | None:
        if self._relation_admission is not None and not self._relation_admission(candidate):
            _append_warning(warnings, "OFFICIAL_RELATION_ROLE_REJECTED", "表单和附件内容不用于生成知识关系", "relations", candidate.proposed_relation)
            return None
        return self._relation_resolver.add(candidate, warnings=warnings)

    def accepted_relations(self) -> list[RelationCandidate]:
        return self._relation_resolver.accepted()

    def relation_candidates(self) -> list[RelationCandidate]:
        """Return all resolved relation candidates, including rejections."""
        return self._relation_resolver.candidates()

    def recompute_relation_cardinality(
        self,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> None:
        self._relation_resolver.recompute_cardinality(warnings=warnings)

    def unresolved_relation_rejections(self) -> list[dict[str, Any]]:
        return self._relation_resolver.unresolved_rejections()

    @property
    def relation_endpoint_remap_count(self) -> int:
        return self._relation_resolver.endpoint_remap_count


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
    existing.mention_aliases = list(
        dict.fromkeys([
            *getattr(existing, "mention_aliases", []),
            existing.name,
            *getattr(incoming, "mention_aliases", []),
            incoming.name,
        ])
    )
    existing.source_candidate_ids = list(
        item for item in dict.fromkeys([
            *getattr(existing, "source_candidate_ids", []),
            existing.candidate_id,
            *getattr(incoming, "source_candidate_ids", []),
            incoming.candidate_id,
        ]) if item
    )
    for property_id, evidences in getattr(incoming, "property_evidence", {}).items():
        target = existing.property_evidence.setdefault(property_id, [])
        for evidence in evidences:
            if not any((item.span_id, item.quote) == (evidence.span_id, evidence.quote) for item in target):
                target.append(evidence)
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
