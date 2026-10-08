from __future__ import annotations

from copy import deepcopy

import pytest

from eval.quality import build_quality_gate, build_quality_summary, expected_modules


def _side(tp=1, fp=0, fn=0):
    return {
        "golden": tp + fn, "predicted": tp + fp, "tp": tp, "fp": fp, "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0,
    }


def _metrics(*, mode="llm", entities=None, relations=None):
    return {
        "match_mode": mode,
        "entities": entities if entities is not None else _side(),
        "relations": relations if relations is not None else _side(0),
    }


def _case(name="c1", *, group="g", metrics=None, status="succeeded", scored=True):
    return {
        "group": group, "case": name, "scored": scored, "status": status,
        "backend": {"modules": {"extract": {"status": status, "metrics": metrics}}},
    }


def _summary(*cases, stage="backend", modes=None, match_mode="llm"):
    return {
        "stage": stage, "modes": modes or ["chunk-extract"],
        "match_mode": match_mode, "cases": list(cases),
    }


@pytest.mark.parametrize(("stage", "modes", "expected"), [
    ("backend", ["extract"], ("backend_extract",)),
    ("backend", ["pipeline"], ("backend_system",)),
    ("plugin", ["chunk-extract"], ("plugin_extract",)),
    ("all", ["chunk-extract", "pipeline"], ("backend_extract", "backend_system", "plugin_extract")),
])
def test_expected_modules_follow_selection(stage, modes, expected):
    assert expected_modules(_summary(stage=stage, modes=modes)) == expected


@pytest.mark.parametrize("mode", ["identity_fallback", "identity", None])
def test_llm_gate_rejects_other_or_unknown_scorers(mode):
    summary = _summary(_case(metrics=_metrics(mode=mode)))
    result = build_quality_gate(summary)
    assert result["passed"] is False
    assert result["issues"] == [{
        "case": "g/c1", "module": "backend_extract", "reason": "scorer_mismatch",
        "expected_match_mode": "llm", "actual_match_mode": mode,
    }]


def test_llm_gate_also_checks_supplementary_module_fallback():
    case = _case(metrics=_metrics())
    case["plugin"] = {"metrics": _metrics(mode="identity_fallback")}
    result = build_quality_gate(_summary(case))
    assert result["passed"] is False
    assert result["issues"][0]["module"] == "plugin_extract"


def test_require_scored_rejects_empty_inventory_and_unannotated_cases():
    assert build_quality_gate(_summary(), require_scored=True)["issues"][0]["reason"] == "no_cases"
    result = build_quality_gate(_summary(_case(scored=False)), require_scored=True)
    assert {issue["reason"] for issue in result["issues"]} == {"unscored_case", "missing_metrics"}
    assert result["passed"] is False
    assert build_quality_gate(_summary(_case(scored=False)))["passed"] is True


def test_require_scored_checks_every_selected_module():
    summary = _summary(_case(metrics=_metrics()), stage="all", modes=["extract", "pipeline"])
    result = build_quality_gate(summary, require_scored=True)
    assert result["passed"] is False
    assert {issue["module"] for issue in result["issues"]} == {"backend_system", "plugin_extract"}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1, "0.8", None, True])
def test_invalid_score_cannot_pass_gate_or_enter_aggregates(value):
    metrics = _metrics()
    metrics["entities"]["f1"] = value
    summary = _summary(_case(metrics=metrics))
    result = build_quality_gate(summary)
    assert result["passed"] is False
    assert result["issues"][0]["fields"] == ["entities.f1"]
    aggregate = build_quality_summary(summary)["modules"]["backend_extract"]
    assert aggregate["coverage"]["invalid"] == 1
    assert aggregate["coverage"]["scored"] == 0
    assert aggregate["micro"]["entities"]["f1"] is None


def test_missing_metric_sides_and_negative_counts_are_invalid():
    metrics = _metrics()
    del metrics["relations"]
    metrics["entities"]["tp"] = -1
    result = build_quality_gate(_summary(_case(metrics=metrics)))
    assert result["passed"] is False
    assert "relations.f1" in result["issues"][0]["fields"]
    assert "entities.tp" in result["issues"][0]["fields"]


def test_relation_threshold_ignores_both_empty_but_rejects_hallucinations():
    summary = _summary(
        _case("empty", metrics=_metrics()),
        _case("missing", metrics=_metrics(relations=_side(0, 0, 1))),
        _case("extra", metrics=_metrics(relations=_side(0, 1, 0))),
    )
    result = build_quality_gate(summary, min_relation_f1=0.5)
    assert result["min_relation_f1"] == {"threshold": 0.5, "below": [
        {"case": "g/missing", "module": "backend_extract", "f1": 0.0},
        {"case": "g/extra", "module": "backend_extract", "f1": 0.0},
    ]}
    assert result["passed"] is False
    assert build_quality_gate(_summary(_case(metrics=_metrics())), min_relation_f1=0.5)["passed"] is True


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), -1, 2, True])
def test_relation_threshold_must_be_finite_unit_interval(threshold):
    with pytest.raises(ValueError):
        build_quality_gate(_summary(), min_relation_f1=threshold)


def test_conditional_aggregates_keep_failed_observations_and_disclose_missing():
    summary = _summary(
        _case("small", metrics=_metrics(entities=_side(1))),
        _case("big", group="h", metrics=_metrics(entities=_side(0, 0, 9)), status="failed"),
        _case("transport-failed", status="failed"),
        _case("judge-fallback", metrics=_metrics(mode="identity_fallback", entities=_side(100))),
        _case("other-scorer", metrics=_metrics(mode="identity", entities=_side(100))),
    )
    snapshot = deepcopy(summary)
    quality = build_quality_summary(summary)
    assert summary == snapshot  # both public functions are safe for repeated reporting
    assert quality["score_scope"] == "conditional_on_valid_scores"
    aggregate = quality["modules"]["backend_extract"]
    assert aggregate["coverage"] == {
        "expected": 5, "scored": 2, "failed": 2, "missing": 1,
        "fallback": 1, "invalid": 0, "mode_mismatch": 1, "unscored": 0, "scored_ratio": 0.4,
    }
    assert aggregate["micro"]["entities"] == {
        "golden": 10, "predicted": 1, "tp": 1, "fp": 0, "fn": 9,
        "precision": 1.0, "recall": 0.1, "f1": 0.181818, "jaccard": 0.1, "accuracy": 0.1,
    }
    assert aggregate["case_macro"]["entities"]["f1"] == 0.5
    assert aggregate["by_group"]["h"]["coverage"]["failed"] == 1
    assert aggregate["by_group"]["h"]["micro"]["entities"]["f1"] == 0.0


def test_modules_are_separate_and_missing_modules_have_no_estimated_score():
    case = _case(metrics=_metrics(entities=_side(8)))
    case["plugin"] = {"status": "succeeded", "metrics": _metrics(entities=_side(0, 2, 2))}
    summary = _summary(case, stage="all", modes=["chunk-extract", "pipeline"])
    modules = build_quality_summary(summary)["modules"]
    assert modules["backend_extract"]["micro"]["entities"]["f1"] == 1.0
    assert modules["plugin_extract"]["micro"]["entities"]["f1"] == 0.0
    assert modules["backend_system"]["coverage"]["missing"] == 1
    assert modules["backend_system"]["micro"]["entities"]["f1"] is None


def test_macro_does_not_penalize_truly_empty_relations():
    summary = _summary(
        _case("empty", metrics=_metrics()),
        _case("relation", metrics=_metrics(relations=_side())),
    )
    aggregate = build_quality_summary(summary)["modules"]["backend_extract"]
    assert aggregate["case_macro"]["relations"] == {
        "cases": 1, "empty_cases": 1, "precision": 1.0, "recall": 1.0, "f1": 1.0,
    }
    empty = build_quality_summary(_summary(_case(metrics=_metrics())))["modules"]["backend_extract"]
    assert empty["micro"]["relations"]["f1"] is None


def test_identity_legacy_metrics_are_still_accepted():
    metrics = _metrics(mode=None)
    del metrics["match_mode"]
    summary = _summary(_case(metrics=metrics), match_mode="identity")
    assert build_quality_gate(summary, require_scored=True)["passed"] is True
    assert build_quality_summary(summary)["modules"]["backend_extract"]["coverage"]["scored"] == 1


def test_empty_summary_reports_zero_coverage_not_perfect_scores():
    module = build_quality_summary(_summary())["modules"]["backend_extract"]
    assert module["coverage"]["expected"] == 0
    assert module["coverage"]["scored_ratio"] is None
    assert module["micro"]["entities"]["f1"] is None
    assert module["micro"]["entities"]["jaccard"] is None
    assert module["micro"]["entities"]["accuracy"] is None
    assert module["case_macro"]["entities"]["f1"] is None
    assert module["group_macro"]["entities"]["f1"] is None


def test_group_macro_weights_groups_equally_and_omits_groups_without_scores():
    summary = _summary(
        _case("a", group="large", metrics=_metrics(entities=_side())),
        _case("b", group="large", metrics=_metrics(entities=_side())),
        _case("c", group="small", metrics=_metrics(entities=_side(0, 1, 1))),
        _case("d", group="missing", status="failed"),
    )
    aggregate = build_quality_summary(summary)["modules"]["backend_extract"]
    assert aggregate["case_macro"]["entities"]["f1"] == 0.666667
    assert aggregate["group_macro"]["entities"] == {
        "groups": 2, "precision": 0.5, "recall": 0.5, "f1": 0.5,
    }
    assert aggregate["group_macro"]["relations"] == {
        "groups": 0, "precision": None, "recall": None, "f1": None,
    }


@pytest.mark.parametrize("case_status, block_status", [("partial", "succeeded"), ("succeeded", "partial")])
def test_partial_cases_or_modules_are_disclosed_as_failed(case_status, block_status):
    case = _case(metrics=_metrics(), status=case_status)
    case["backend"]["modules"]["extract"]["status"] = block_status
    coverage = build_quality_summary(_summary(case))["modules"]["backend_extract"]["coverage"]
    assert coverage["failed"] == 1
    assert coverage["scored"] == 1


@pytest.mark.parametrize("verified", [False, None, "true", 1])
def test_release_gate_requires_explicit_verification_of_reused_prediction_inputs(verified):
    summary = _summary(_case(metrics=_metrics()))
    reused = {"id": "g/c1"}
    if verified is not None:
        reused["input_verified"] = verified
    summary["manifest"] = {"reused_predictions": [reused]}
    result = build_quality_gate(summary, require_scored=True)
    assert result["passed"] is False
    assert result["issues"] == [{
        "case": "g/c1", "module": "backend_extract", "reason": "reused_prediction_input_unverified",
    }]
    assert build_quality_gate(summary, require_scored=False)["passed"] is True


def test_verified_reused_prediction_inputs_can_pass_release_gate():
    summary = _summary(_case(metrics=_metrics()))
    summary["manifest"] = {"reused_predictions": [{"id": "g/c1", "input_verified": True}]}
    assert build_quality_gate(summary, require_scored=True)["passed"] is True
