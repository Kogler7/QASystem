import json

import pytest

from eval.tools.offline_matrix_audit import aggregate, audit, audit_case, markdown, nonempty, rule_fields


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def golden(root):
    path = root / "inputs/json/rule/case/extract/golden.json"
    write(path, {"entities": [{"type": "RuleType", "name": "A", "properties": {"antecedent": "A", "consequent": "B"}}], "relations": []})
    return path


def score_case(path, *, status="partial", graph=True, count=1, tp=0):
    side = {"golden": 1, "predicted": count, "tp": tp, "fp": count - tp, "fn": 1 - tp,
            "precision": tp / count if count else 0, "recall": tp, "f1": 2 * tp / (1 + count)}
    empty = {"golden": 0, "predicted": 0, "tp": 0, "fp": 0, "fn": 0, "precision": 0, "recall": 0, "f1": 0}
    write(path / "scores.json", {"status": "failed" if status == "partial" else status, "scored": True,
        "backend": {"modules": {"extract": {"status": status, "metrics": {"match_mode": "llm", "entities": side, "relations": empty}}}}})
    if graph:
        write(path / "backend_extract.json", {"data": {"graph": {"nodes": [
            {"label": "RuleType", "properties": {"name": "A", "antecedent": "A", "consequent": "B"}}
            for _ in range(count)], "relationships": []}}})


def test_nonempty_does_not_equate_fields_with_truth():
    assert not nonempty("  ") and not nonempty(None)
    assert nonempty(False) and nonempty(0)
    fields = rule_fields({"graph": {"entities": [{"type": "RuleType", "properties": {"antecedent": "false premise", "consequent": "wrong conclusion"}}], "relations": []}})
    assert fields["counts"]["both"] == 1  # Existence, not semantic validation.


def test_malformed_rows_are_not_silently_discarded():
    with pytest.raises(ValueError, match="row_invalid"):
        rule_fields({"nodes": [None], "relationships": []})


def test_partial_valid_scores_stay_in_conditional_denominator(tmp_path):
    g = golden(tmp_path)
    path = tmp_path / "case"
    score_case(path)
    record = audit_case("rule/case", path, g)
    totals = aggregate([record])
    assert totals["valid_scored"] == 1 and totals["classification_counts"]["partial_or_failed_result"] == 1
    assert totals["micro"]["entities"]["golden"] == 1 and totals["micro"]["entities"]["f1"] == 0


def test_infrastructure_no_graph_is_not_imputed_as_zero(tmp_path):
    g = golden(tmp_path)
    score_case(tmp_path / "case", graph=False)
    record = audit_case("rule/case", tmp_path / "case", g)
    assert not record["valid_score"] and record["classification"] == "infrastructure_or_no_result"
    assert aggregate([record])["micro"]["entities"]["f1"] is None
    assumption = aggregate([record])["failure_as_empty_assumption"]
    assert assumption["all_expected_covered"] and assumption["micro"]["entities"]["f1"] == 0


def test_inflight_graph_without_final_score_is_pending(tmp_path):
    g = golden(tmp_path)
    write(tmp_path / "case/backend_extract.json", {"data": {"graph": {"nodes": [], "relationships": []}}})
    assert audit_case("rule/case", tmp_path / "case", g)["classification"] == "pending"
    pending = aggregate([audit_case("rule/case", tmp_path / "case", g)])["failure_as_empty_assumption"]
    assert not pending["all_expected_covered"] and pending["micro"]["entities"]["f1"] is None


def test_observed_empty_output_is_valid_zero_with_golden_denominator(tmp_path):
    g = golden(tmp_path)
    score_case(tmp_path / "case", count=0)
    record = audit_case("rule/case", tmp_path / "case", g)
    assert record["classification"] == "observed_empty" and record["valid_score"]
    assert aggregate([record])["micro"]["entities"]["golden"] == 1


def test_graph_metric_mismatch_blocks_score(tmp_path):
    g = golden(tmp_path)
    score_case(tmp_path / "case")
    write(tmp_path / "case/backend_extract.json", {"data": {"graph": {"nodes": [], "relationships": []}}})
    record = audit_case("rule/case", tmp_path / "case", g)
    assert not record["valid_score"] and record["reason"] == "graph_metric_denominator_mismatch"


def test_recovery_requires_explicit_note_and_preserves_failed_attempt(tmp_path):
    golden(tmp_path)
    write(tmp_path / "plan.json", {"cases": ["rule/case"], "extractors": [{"id": "api"}], "judge": {"id": "judge"}})
    main = tmp_path / "evaluation/json/api/judge/runs/001/rule/case"
    recovery = tmp_path / "restart-recovery-new/json/api/judge/runs/002/rule/case"
    score_case(main, graph=False)
    score_case(recovery, status="ok", tp=1)
    with pytest.raises(ValueError, match="equivalence_note"):
        audit(tmp_path, variants=("json",), combine_recovery=True)
    separate = audit(tmp_path, variants=("json",))
    assert "combined_groups" not in separate and separate["groups"]["json/api"]["valid_scored"] == 0
    combined = audit(tmp_path, variants=("json",), combine_recovery=True, equivalence_note="same frozen extraction source, explicit study authorization")
    case = combined["combined_groups"]["json/api"]["cases"][0]
    assert case["valid_score"] and case["classification"] == "success"
    assert case["attempt_statuses"][0]["classification"] == "infrastructure_or_no_result"


def test_summary_dropped_failed_case_denominator_is_visible(tmp_path):
    golden(tmp_path)
    write(tmp_path / "plan.json", {"cases": ["rule/case"], "extractors": [{"id": "api"}], "judge": {"id": "judge"}})
    run = tmp_path / "evaluation/json/api/judge/runs/001"
    score_case(run / "rule/case")
    write(run / "summary.json", {"cases": [], "overall": {"backend_extract": {"entities": {"golden": 0}}}})
    group = audit(tmp_path, variants=("json",))["groups"]["json/api"]
    assert group["expected"] == 1 and group["reported_summary_case_count"] == 0
    assert group["summary_micro_differences"]["entities"]["golden"] == {"reported": 0, "audited": 1}


def test_interrupted_case_is_not_failed_zero_and_markdown_has_coverage(tmp_path):
    golden(tmp_path)
    write(tmp_path / "plan.json", {"cases": ["rule/case"], "extractors": [{"id": "api"}], "judge": {"id": "judge"}})
    result = audit(tmp_path, variants=("json",), interrupted_groups=("json/api",))
    g = result["groups"]["json/api"]
    assert g["classification_counts"]["interrupted"] == 1
    assert g["failure_as_empty_assumption"]["covered_cases"] == 0
    assert "0/1" in markdown(result)


def test_independent_output_root_reuses_explicit_frozen_plan_and_inputs(tmp_path):
    frozen = tmp_path / "frozen"
    golden(frozen)
    write(frozen / "plan.json", {"cases": ["rule/case"], "extractors": [{"id": "api"}], "judge": {"id": "judge"}})
    output = tmp_path / "new-source-hash"
    score_case(output / "results/json/api/judge/runs/001/rule/case", status="ok", tp=1)
    result = audit(output, variants=("json",), plan_path=frozen / "plan.json", inputs_root=frozen / "inputs", evaluation_root=output / "results")
    assert result["groups"]["json/api"]["valid_scored"] == 1
    assert result["inputs_root"] == str(frozen / "inputs")
