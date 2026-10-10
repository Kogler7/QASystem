import json

import pytest

from eval import run


@pytest.mark.parametrize("data", [
    {"entities": "oops"}, {"entities": [None]}, {"graph": []}, {},
    {"entities": [{"type": "T"}]},
    {"entities": [{"type": "T", "name": "a", "properties": []}]},
    {"relations": [{"rel_type": "R"}]},
    {"nodes": [{"label": "T"}], "relationships": []},
    {"nodes": [], "relationships": [{"from_uid": "a", "to_uid": "b", "type": "R"}]},
])
def test_invalid_annotation_is_unscored_with_actionable_reason(tmp_path, data):
    path = tmp_path / "extract" / "golden.json"
    path.parent.mkdir()
    path.write_text(json.dumps(data), encoding="utf-8")
    _, golden, reason = run.load_golden(tmp_path)
    assert golden is None
    assert reason and "golden" in reason


def test_annotated_negative_case_does_not_fail_positive_entity_recall_threshold(tmp_path):
    golden = {"entities": [], "relations": [], "score_empty": True}
    path = tmp_path / "extract" / "golden.json"
    path.parent.mkdir()
    path.write_text(json.dumps(golden), encoding="utf-8")
    _, loaded, reason = run.load_golden(tmp_path)
    assert loaded == golden and reason is None
    metrics = run.score_against_golden(loaded, {"nodes": [], "relationships": []}, match_mode="identity")
    summary = {"match_mode": "identity", "cases": [{
        "group": "g", "case": "negative", "scored": True,
        "backend": {"modules": {"extract": {"metrics": metrics}}},
    }]}
    gate = run.build_run_gate(summary, min_f1=0.5, min_relation_f1=0.5,
                              require_scored=True, max_case_seconds=None, stage_budgets={})
    assert gate["passed"] is True


def test_hallucinated_entity_on_negative_case_fails_threshold():
    metrics = run.score_against_golden({"entities": [], "score_empty": True},
                                      {"entities": [{"type": "T", "name": "made up"}]},
                                      match_mode="identity")
    gate = run.build_run_gate({"cases": [{"scored": True, "backend": {"modules": {
        "extract": {"metrics": metrics},
    }}}]}, min_f1=0.5, max_case_seconds=None, stage_budgets={})
    assert gate["passed"] is False
