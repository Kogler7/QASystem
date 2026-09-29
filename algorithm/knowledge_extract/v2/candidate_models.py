"""Internal mention-level candidates shared by generation and resolution."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .evidence import Evidence


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
    mapping_status: str = "accepted"
    warnings: list[str] = field(default_factory=list)
    identity_property: str = "name"
    identity_value: str = ""
    resolution_method: str = "new_entity"
    resolution_methods: list[str] = field(default_factory=list)
    property_conflicts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.canonical_type or self.proposed_type,
            "name": self.name,
            "properties": dict(self.properties),
            "candidate_id": self.candidate_id,
        }
        if self.evidence:
            payload["evidence"] = self.evidence[0].to_dict()
        return payload

    def object_model(self, features: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": self.candidate_id,
            "type": self.canonical_type or self.proposed_type,
            "identity": self.identity_value or self.name,
            "display_name": self.name,
            "properties": dict(self.properties),
            "features": features,
            "evidence": [evidence.to_dict() for evidence in self.evidence],
            "mapping": {"score": self.mapping_score, "status": self.mapping_status},
            "resolution": {
                "method": self.resolution_method,
                "methods": list(self.resolution_methods),
                "property_conflicts": list(self.property_conflicts),
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
        }
        if self.evidence:
            payload["evidence"] = self.evidence[0].to_dict()
        return payload


__all__ = ["EntityCandidate", "RelationCandidate"]
