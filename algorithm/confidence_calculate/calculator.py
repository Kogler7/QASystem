from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

class ConfidenceCalculator:
    def __init__(self, text_evidence: Any | None = None, path_evidence: Any | None = None):
        config_path = Path(__file__).with_name("config.yaml")
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        self.text_weight = float(config.get("text_weight", 0.7))
        self.path_weight = float(config.get("path_weight", 0.3))
        total_weight = self.text_weight + self.path_weight
        if total_weight <= 0:
            raise ValueError("置信度权重之和必须大于 0")
        self.text_weight /= total_weight
        self.path_weight /= total_weight
        if text_evidence is None or path_evidence is None:
            from external_services.application import ExternalCapabilityUnavailable

            raise ExternalCapabilityUnavailable(
                "confidence.calculate", "第三方置信度接口尚未开通"
            )
        self.text_evidence = text_evidence
        self.path_evidence = path_evidence
        self._lock = RLock()

    def calculate(self, data: dict[str, Any]) -> dict[str, Any]:
        graph = data.get("graph") or {}
        nodes = graph.get("nodes") or []
        relationships = graph.get("relationships") or []
        ontology_relations = graph.get("ontology_relations") or []
        names = {
            node.get("uid", ""): (node.get("properties") or {}).get("name", node.get("uid", ""))
            for node in nodes
        }
        relationship_scores: list[dict[str, Any]] = []
        ontology_relation_scores: list[dict[str, Any]] = []

        with self._lock:
            for source, target in (
                (relationships, relationship_scores),
                (ontology_relations, ontology_relation_scores),
            ):
                for relation in source:
                    from_uid = relation.get("from_uid", "")
                    to_uid = relation.get("to_uid", "")
                    triple = {
                        "subject": names.get(from_uid, from_uid),
                        "predicate": relation.get("type", ""),
                        "object": names.get(to_uid, to_uid),
                    }
                    text_score = float(self.text_evidence.get_score(triple))
                    path_score = float(self.path_evidence.get_score(triple))
                    confidence = round(
                        self.text_weight * text_score + self.path_weight * path_score,
                        4,
                    )
                    target.append(
                        {
                            "text_score": text_score,
                            "path_score": path_score,
                            "text_weight": self.text_weight,
                            "path_weight": self.path_weight,
                            "confidence": confidence,
                        }
                    )

        return {
            "code": 0,
            "info": "置信度评估完成",
            "data": {
                "evaluation_id": f"EVAL-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                "total_count": len(relationships) + len(ontology_relations),
                "relationships": relationship_scores,
                "ontology_relations": ontology_relation_scores,
            },
            "error": None,
        }

    def close(self) -> None:
        self.text_evidence.close()
        self.path_evidence.close()


@lru_cache(maxsize=1)
def get_confidence_calculator() -> ConfidenceCalculator:
    return ConfidenceCalculator()


def calculate_graph_confidence(data: dict[str, Any]) -> dict[str, Any]:
    return get_confidence_calculator().calculate(data)


def close_confidence_resources() -> None:
    if get_confidence_calculator.cache_info().currsize:
        get_confidence_calculator().close()
        get_confidence_calculator.cache_clear()
