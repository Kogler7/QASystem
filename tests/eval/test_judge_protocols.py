import pytest

from eval.judge_protocols import ChoiceJudge, compatible_candidates, controlled_input, endpoint_consistent_candidates, scalar_conflicts, score_complete, validate_choices


def graph():
    entities = [{"type": "Term", "name": str(i), "properties": {"definition": f"value{i}"}} for i in range(5)]
    return {"entities": {"golden": entities, "predicted": entities}, "relations": {"golden": [], "predicted": []}}


def test_candidates_only_exclude_incompatible_signature_not_name():
    candidates = compatible_candidates([{"type": "Term", "name": "A"}], [
        {"type": "Term", "name": "entirely unrelated"}, {"type": "Problem", "name": "A"}])
    assert candidates == [(0, 0)]


def test_context_one_changes_surroundings_but_preserves_target_attributes():
    data = graph()
    local, target = controlled_input(data, "entities", 2, 3)
    context, reference = controlled_input(data, "entities", 2, 3, "context_original_one")
    first, reordered = controlled_input(data, "entities", 2, 3, "context_first_one")
    many, _ = controlled_input(data, "entities", 2, 3, "context_original_all")
    assert target == reordered == "g0_p0"
    assert reference == "g2_p3"
    assert len(local["questions"]) == len(context["questions"]) == len(first["questions"]) == 1
    assert len(many["questions"]) == 16
    for payload in (local, context, first, many):
        assert "value2" in payload["state"] and "value3" in payload["state"]
    assert "value0" not in local["state"] and "value0" in context["state"]


def test_named_questions_bind_each_object_without_altering_window_attributes():
    data = graph()
    original, _ = controlled_input(data, "entities", 2, 3, "context_original_all")
    named, target = controlled_input(data, "entities", 2, 3, "context_named_all")
    assert target == "g2_p3" and named["state"] == original["state"]
    instructions = named["questions"][target]["instructions"]
    assert '"name": "2"' in instructions and '"name": "3"' in instructions
    assert "value2" not in instructions  # Attributes remain in their original state rows.
    assert named["questions"].keys() == original["questions"].keys()


@pytest.mark.parametrize("answers", [{}, {"g0_p0": {"choice": "maybe"}}, {"other": {"choice": "yes"}}])
def test_invalid_or_missing_answer_fails(answers):
    with pytest.raises(ValueError):
        validate_choices(answers, {"g0_p0": {}})


def test_incomplete_failed_or_duplicate_full_scan_never_scores():
    data = graph()
    rows = [{"collection": "entities", "g": g, "p": p, "status": "succeeded", "decision": g == p}
            for g in range(5) for p in range(5)]
    assert score_complete(data, rows)["status"] == "scored"
    assert score_complete(data, rows[:-1])["status"] == "unscored"
    assert score_complete(data, rows + [rows[0]])["status"] == "unscored"
    rows[0]["status"] = "judge_failed"
    assert score_complete(data, rows)["status"] == "unscored"


def test_http_input_limit_is_explicit_failure_without_context_trimming(monkeypatch):
    payload, _ = controlled_input(graph(), "entities", 2, 3)
    seen = []
    def response(*args, **kwargs):
        seen.append(kwargs["data"])
        return 413, {}, b'{}'
    monkeypatch.setattr("eval.run.http_request", response)
    result = ChoiceJudge().classify(payload)
    assert result["status"] == "judge_failed" and result["choices"] == {}
    assert len(seen) == 1
    assert b"value2" in seen[0] and b"value3" in seen[0]


def test_attribute_diagnostics_are_separate_from_match_metrics():
    data = {"entities": {"golden": [{"type": "Term", "name": "A", "properties": {"unit": "100 kPa", "description": "A"}}],
                           "predicted": [{"type": "Term", "name": "A", "properties": {"unit": "0.1 MPa"}}]},
            "relations": {"golden": [], "predicted": []}}
    score = score_complete(data, [{"collection": "entities", "g": 0, "p": 0, "status": "succeeded", "decision": True}])
    assert score["metrics"]["entities"]["tp"] == 1
    audit = score["attribute_diagnostics"]["matched_entities"][0]
    assert audit["normalized_value_difference_keys"] == ["unit"]
    assert audit["golden_nonempty_keys_absent_in_prediction"] == ["description"]


@pytest.mark.parametrize("left,right,conflict", [
    ("2026-04-10", "2026/4/10", False), ("2026-04-10", "2026-04-20", True),
    ("100 kPa", "0.1 MPa", False), ("80 km/h", "100 km/h", True),
    ("10 m/s", "36 km/h", False), ("完整文字描述", "另一种文字描述", False),
    ("100", "200", False),  # Could be IDs rather than scalar quantities.
])
def test_scalar_guard_is_narrow_and_unit_aware(left, right, conflict):
    assert bool(scalar_conflicts({"properties": {"值": left}}, {"properties": {"值": right}})) == conflict


def test_endpoint_guard_uses_candidate_graph_not_selected_assignment():
    data = {"entities": {"golden": [{"type": "Term", "name": "A"}, {"type": "Term", "name": "B"}],
                         "predicted": [{"type": "Term", "name": "X"}, {"type": "Term", "name": "Y"}]},
            "relations": {"golden": [{"from_type": "Term", "from_name": "A", "rel_type": "RELATED", "to_type": "Term", "to_name": "B"}],
                          "predicted": [{"from_type": "Term", "from_name": "X", "rel_type": "RELATED", "to_type": "Term", "to_name": "Y"}]}}
    all_edges = [(0, 0), (0, 1), (1, 0), (1, 1)]
    accepted, rejected = endpoint_consistent_candidates(data, all_edges, [(0, 0)])
    assert accepted == [(0, 0)] and rejected == []
    accepted, rejected = endpoint_consistent_candidates(data, [(0, 0)], [(0, 0)])
    assert accepted == [] and rejected[0]["reasons"] == ["to_not_confirmed_by_entity_judge"]
    data["entities"]["golden"].append({"type": "Term", "name": "A"})
    _, rejected = endpoint_consistent_candidates(data, all_edges, [(0, 0)])
    assert "from_missing_or_ambiguous_identity" in rejected[0]["reasons"]
