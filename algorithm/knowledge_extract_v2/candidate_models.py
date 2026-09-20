"""Internal mention-level candidates shared by generation and resolution."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .evidence import Evidence
from .mapping_scorer import mapping_confidence_payload


@dataclass
class EntityCandidate:
    candidate_id: str
    proposed_type: str
    canonical_type: str | None
    name: str
    properties: dict[str, Any]
    evidence: list[Evidence]
    source: str
    extraction_pass: str = "entity"
    mapping_score: float | None = None
    mapping_score_status: str | None = None
    mapping_status: str = "accepted"
    warnings: list[str] = field(default_factory=list)
    identity_property: str = "name"
    identity_value: str = ""
    resolution_method: str = "new_entity"
    resolution_methods: list[str] = field(default_factory=list)
    property_conflicts: list[dict[str, Any]] = field(default_factory=list)
    lifecycle: str = "MENTIONED"
    mention_aliases: list[str] = field(default_factory=list)
    source_candidate_ids: list[str] = field(default_factory=list)
    resolution_trace: list[dict[str, Any]] = field(default_factory=list)
    property_evidence: dict[str, list[Evidence]] = field(default_factory=dict)
    source_identity: str = ""
    source_files: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.canonical_type or self.proposed_type,
            "name": self.name,
            "properties": dict(self.properties),
            "candidate_id": self.candidate_id,
            **mapping_confidence_payload(
                self.mapping_score,
                self.mapping_score_status or self.mapping_status,
                evidences=self.evidence,
                canonical_mapping_valid=bool(self.canonical_type),
                warnings=self.warnings,
            ),
        }
        if self.evidence:
            payload["evidence"] = self.evidence[0].to_dict()
            payload["evidences"] = [item.to_dict() for item in self.evidence]
        if self.source_files:
            payload["source_files"] = list(self.source_files)
        return payload

    def object_model(self, features: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": self.candidate_id,
            "type": self.canonical_type or self.proposed_type,
            "identity": self.identity_value or self.name,
            "display_name": self.name,
            **({"source_files": list(self.source_files)} if self.source_files else {}),
            "properties": dict(self.properties),
            "features": features,
            "evidence": [evidence.to_dict() for evidence in self.evidence],
            "mapping": {
                "score": self.mapping_score,
                "status": self.mapping_status,
                "admission_status": self.mapping_status,
                "score_status": self.mapping_score_status or self.mapping_status,
            },
            **mapping_confidence_payload(
                self.mapping_score,
                self.mapping_score_status or self.mapping_status,
                evidences=self.evidence,
                canonical_mapping_valid=bool(self.canonical_type),
                warnings=self.warnings,
            ),
            "resolution": {
                "method": self.resolution_method,
                "methods": list(self.resolution_methods),
                "property_conflicts": list(self.property_conflicts),
                "lifecycle": self.lifecycle,
                "mention_aliases": list(self.mention_aliases),
                "source_candidate_ids": list(self.source_candidate_ids),
                "trace": list(self.resolution_trace),
                "property_evidence": {
                    key: [item.to_dict() for item in values]
                    for key, values in self.property_evidence.items()
                },
            },
        }


@dataclass
class RelationCandidate:
    candidate_id: str
    from_candidate_id: str
    to_candidate_id: str
    proposed_relation: str
    canonical_relation: str | None
    evidence: list[Evidence]
    from_name: str = ""
    to_name: str = ""
    from_type: str = ""
    to_type: str = ""
    mapping_score: float | None = None
    mapping_score_status: str | None = None
    mapping_status: str = "accepted"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "from_id": self.from_candidate_id,
            "from_type": self.from_type,
            "from_name": self.from_name,
            "rel_type": self.canonical_relation or self.proposed_relation,
            "to_type": self.to_type,
            "to_name": self.to_name,
            "to_id": self.to_candidate_id,
            **mapping_confidence_payload(
                self.mapping_score,
                self.mapping_score_status or self.mapping_status,
                evidences=self.evidence,
                canonical_mapping_valid=bool(self.canonical_relation),
                aggregate_evidence=False,
                aggregate_canonical_mapping=False,
                warnings=self.warnings,
            ),
        }
        if self.evidence:
            payload["evidence"] = self.evidence[0].to_dict()
            payload["evidences"] = [item.to_dict() for item in self.evidence]
        return payload


__all__ = ["EntityCandidate", "RelationCandidate"]
