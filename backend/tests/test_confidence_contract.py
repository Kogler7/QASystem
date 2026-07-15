from __future__ import annotations

from algorithm.confidence_calculate.calculator import ConfidenceCalculator


class _Evidence:
    def __init__(self, score: float):
        self.score = score

    def get_score(self, _triple):
        return self.score

    def close(self):
        pass


def test_confidence_result_is_ordered_and_does_not_repeat_graph():
    calculator = ConfidenceCalculator(
        text_evidence=_Evidence(0.8),
        path_evidence=_Evidence(0.6),
    )
    result = calculator.calculate(
        {
            "graph": {
                "nodes": [
                    {"uid": "a", "properties": {"name": "A"}},
                    {"uid": "b", "properties": {"name": "B"}},
                ],
                "relationships": [
                    {"from_uid": "a", "to_uid": "b", "type": "RELATED"},
                ],
                "ontology_relations": [
                    {"from_uid": "b", "to_uid": "a", "type": "ALLOWS"},
                ],
            }
        }
    )

    data = result["data"]
    assert data["total_count"] == 2
    assert len(data["relationships"]) == 1
    assert len(data["ontology_relations"]) == 1
    assert data["relationships"][0]["confidence"] == 0.74
    assert "graph" not in data
    assert "details" not in data
    assert "message" not in result
