from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from eval.provenance import build_manifest, compare_baseline


def _workspace(root: Path) -> tuple[Path, list[dict], list[Path]]:
    workspace = root / "dataset"
    case = workspace / "group" / "case"
    case.mkdir(parents=True)
    files = {"doc.txt": "input", "golden.json": '{"entities": []}', "ontology.puml": "entity T"}
    for name, content in files.items():
        (case / name).write_text(content, encoding="utf-8")
    evaluator = root / "code" / "run.py"
    evaluator.parent.mkdir()
    evaluator.write_text("# evaluator version 1\n", encoding="utf-8")
    cases = [{"group": "group", "case": "case", "sources": [case / "doc.txt"],
              "golden": case / "golden.json", "ontology": case / "ontology.puml"}]
    return workspace, cases, [evaluator]


def _manifest(root: Path, **overrides) -> dict:
    workspace, cases, paths = _workspace(root)
    kwargs = {"workspace": workspace, "cases": cases, "reuse_dir": None,
              "settings": {"stage": "backend", "modes": ["chunk-extract"], "match_mode": "llm",
                           "judge": {"model": "judge-v1", "batch_size": 20}, "options": {"extract_version": "v2"}},
              "evaluator_paths": paths}
    kwargs.update(overrides)
    return build_manifest(**kwargs)


def _summary(manifest: dict) -> dict:
    metrics = {"match_mode": "llm", "entities": {"f1": 0.8}, "relations": {"f1": 0.7}}
    return {"manifest": manifest, "stage": "backend", "modes": ["chunk-extract"], "match_mode": "llm",
            "cases": [{"group": "group", "case": "case", "status": "succeeded", "scored": True,
                       "elapsed_seconds": 1200.0,
                       "backend": {"modules": {
                           "parse": {"status": "succeeded", "elapsed_seconds": 5.0},
                           "chunk": {"status": "succeeded", "elapsed_seconds": 2.0},
                           "extract": {"status": "succeeded", "elapsed_seconds": 20.0,
                                       "score_elapsed_seconds": 1000.0, "metrics": metrics}}}}]}


def _compare(current: dict, baseline: dict, **overrides) -> dict:
    kwargs = {"max_f1_drop": 0.02, "max_latency_increase": 0.2, **overrides}
    return compare_baseline(current, baseline, **kwargs)


def _extract(summary: dict) -> dict:
    return summary["cases"][0]["backend"]["modules"]["extract"]


def _reasons(result: dict) -> set[str]:
    return {issue["reason"] for issue in result["issues"]}


def test_manifest_portable_sorted_and_sensitive_to_file_bytes(tmp_path):
    first = _manifest(tmp_path / "one")
    moved = _manifest(tmp_path / "two")
    assert first == moved
    code = tmp_path / "two" / "code" / "run.py"
    code.write_text("# uncommitted evaluator change\n", encoding="utf-8")
    workspace = tmp_path / "two" / "dataset"
    case = workspace / "group" / "case"
    kwargs = {"workspace": workspace, "cases": [{"group": "group", "case": "case", "sources": [case / "doc.txt"],
              "golden": case / "golden.json", "ontology": case / "ontology.puml"}], "reuse_dir": None,
              "settings": moved["settings"], "evaluator_paths": [code]}
    changed_code = build_manifest(**kwargs)
    assert first["evaluator"]["sha256"] != changed_code["evaluator"]["sha256"]
    assert first["dataset_sha256"] == changed_code["dataset_sha256"]
    (case / "golden.json").write_text('{"entities": [{"name": "added"}]}', encoding="utf-8")
    assert first["dataset_sha256"] != build_manifest(**kwargs)["dataset_sha256"]


@pytest.mark.parametrize("filename,mode,input_mode", [("chunks.json", "extract", "reused_chunks"),
                                                       ("document.md", "chunk-extract", "reused_markdown")])
def test_reused_input_bytes_are_part_of_dataset(tmp_path, filename, mode, input_mode):
    workspace, cases, paths = _workspace(tmp_path)
    reuse = tmp_path / "old-run"
    case_dir = reuse / "group" / "case"
    case_dir.mkdir(parents=True)
    (case_dir / filename).write_text("original reused bytes", encoding="utf-8")
    kwargs = {"workspace": workspace, "cases": cases, "evaluator_paths": paths, "reuse_dir": reuse,
              "settings": {"stage": "backend", "modes": [mode, "pipeline"]}}
    manifest = build_manifest(**kwargs)
    assert manifest["cases"][0]["input_mode"] == {"backend_extract": input_mode, "backend_system": "source_documents"}
    (case_dir / filename).write_text("changed reused bytes", encoding="utf-8")
    assert build_manifest(**kwargs)["dataset_sha256"] != manifest["dataset_sha256"]


def test_missing_optional_markdown_uses_raw_source_fingerprint(tmp_path):
    workspace, cases, paths = _workspace(tmp_path)
    kwargs = {"workspace": workspace, "cases": cases, "evaluator_paths": paths, "settings": {"modes": ["chunk-extract"]}}
    normal = build_manifest(**kwargs, reuse_dir=None)
    reuse_missing = build_manifest(**kwargs, reuse_dir=tmp_path / "missing-run")
    assert normal["dataset_sha256"] == reuse_missing["dataset_sha256"]


@pytest.mark.parametrize("nested", [False, True])
def test_real_ontology_directory_follows_runner_selection_and_passes_baseline(tmp_path, nested):
    workspace, cases, paths = _workspace(tmp_path)
    ontology = workspace / "group" / "ontology"
    ontology.mkdir()
    selected_dir = ontology / "nested" if nested else ontology
    selected_dir.mkdir(exist_ok=True)
    (selected_dir / "a.puml").write_text("entity A", encoding="utf-8")
    (selected_dir / "b.UML").write_text("entity B", encoding="utf-8")
    (ontology / "ignored.txt").write_text("not an ontology", encoding="utf-8")
    cases[0]["ontology"] = ontology
    kwargs = {"workspace": workspace, "cases": cases, "reuse_dir": None,
              "settings": {"match_mode": "llm"}, "evaluator_paths": paths}
    manifest = build_manifest(**kwargs)
    record = manifest["cases"][0]["ontology"]
    assert record["missing"] is False
    assert len(record["files"]) == 2
    summary = _summary(manifest)
    assert _compare(summary, summary)["passed"]
    (selected_dir / "a.puml").write_text("entity Changed", encoding="utf-8")
    assert build_manifest(**kwargs)["dataset_sha256"] != manifest["dataset_sha256"]
    if not nested:
        # The runner ignores nested ontologies if top-level files exist.
        (ontology / "nested").mkdir()
        (ontology / "nested" / "ignored.puml").write_text("entity Ignored", encoding="utf-8")
        before = build_manifest(**kwargs)
        (ontology / "nested" / "ignored.puml").write_text("entity ChangedIgnored", encoding="utf-8")
        assert build_manifest(**kwargs)["dataset_sha256"] == before["dataset_sha256"]


def test_empty_ontology_directory_is_explicitly_missing(tmp_path):
    workspace, cases, paths = _workspace(tmp_path)
    ontology = workspace / "empty-ontology"
    ontology.mkdir()
    cases[0]["ontology"] = ontology
    manifest = build_manifest(workspace=workspace, cases=cases, reuse_dir=None, settings={}, evaluator_paths=paths)
    assert manifest["cases"][0]["ontology"]["missing"] is True


def test_missing_golden_is_recorded_for_smoke_but_cannot_pass_baseline(tmp_path):
    workspace, cases, paths = _workspace(tmp_path)
    cases[0]["golden"] = None
    manifest = build_manifest(workspace=workspace, cases=cases, reuse_dir=None, settings={"match_mode": "llm"}, evaluator_paths=paths)
    assert manifest["cases"][0]["golden"] == {"path": None, "sha256": None, "missing": True}
    summary = _summary(manifest)
    result = _compare(summary, summary)
    assert not result["passed"]
    assert "missing_dataset_files" in _reasons(result)


def test_reused_predictions_recorded_without_conflating_outputs_and_inputs(tmp_path):
    workspace, cases, paths = _workspace(tmp_path)
    reuse = tmp_path / "old-run"
    old_case = reuse / "group" / "case"
    old_case.mkdir(parents=True)
    prediction = old_case / "backend_extract.json"
    prediction.write_text('{"nodes": []}', encoding="utf-8")
    (old_case / "scores.json").write_text('{"elapsed_seconds": 5}', encoding="utf-8")
    kwargs = {"workspace": workspace, "cases": cases, "reuse_dir": None, "settings": {},
              "evaluator_paths": paths, "reuse_extract_dir": reuse}
    first = build_manifest(**kwargs)
    prediction.write_text('{"nodes": [{"name": "new"}]}', encoding="utf-8")
    second = build_manifest(**kwargs)
    assert first["dataset_sha256"] == second["dataset_sha256"]
    assert first["reused_predictions"] != second["reused_predictions"]
    assert first["reused_predictions"][0]["input_verified"] is False


@pytest.mark.parametrize("source_chunks", ["same", "different", "missing"])
def test_reused_prediction_inputs_must_match_selected_chunks(tmp_path, source_chunks):
    workspace, cases, paths = _workspace(tmp_path)
    input_run, prediction_run = tmp_path / "inputs", tmp_path / "predictions"
    for root in (input_run, prediction_run):
        (root / "group" / "case").mkdir(parents=True)
    (input_run / "group" / "case" / "chunks.json").write_text("input chunks", encoding="utf-8")
    prediction_case = prediction_run / "group" / "case"
    (prediction_case / "backend_extract.json").write_text("{}", encoding="utf-8")
    (prediction_case / "scores.json").write_text("{}", encoding="utf-8")
    if source_chunks != "missing":
        (prediction_case / "chunks.json").write_text("input chunks" if source_chunks == "same" else "other chunks", encoding="utf-8")
    source_manifest = build_manifest(workspace=workspace, cases=cases, reuse_dir=None, settings={}, evaluator_paths=paths)
    (prediction_run / "manifest.json").write_text(json.dumps(source_manifest), encoding="utf-8")
    manifest = build_manifest(workspace=workspace, cases=cases, reuse_dir=input_run, evaluator_paths=paths,
                              reuse_extract_dir=prediction_run, settings={"modes": ["extract"], "match_mode": "llm"})
    assert manifest["reused_predictions"][0]["input_verified"] is (source_chunks == "same")
    summary = _summary(manifest)
    summary["modes"] = ["extract"]
    result = _compare(summary, summary)
    assert result["passed"] is (source_chunks == "same")
    if source_chunks != "same":
        assert "reused_prediction_input_unverified" in _reasons(result)


@pytest.mark.parametrize("change", ["missing_manifest", "changed_ontology", "missing_ontology", "unverified_ancestor"])
def test_reused_prediction_requires_same_source_ontology(tmp_path, change):
    workspace, cases, paths = _workspace(tmp_path)
    reuse = tmp_path / "old"
    source = reuse / "group" / "case"
    source.mkdir(parents=True)
    (source / "chunks.json").write_text("same chunks", encoding="utf-8")
    (source / "backend_extract.json").write_text("{}", encoding="utf-8")
    source_manifest = build_manifest(workspace=workspace, cases=cases, reuse_dir=None, settings={}, evaluator_paths=paths)
    if change == "changed_ontology":
        source_manifest["cases"][0]["ontology"]["sha256"] = "different ontology"
    if change == "missing_ontology":
        source_manifest["cases"][0].pop("ontology")
    if change == "unverified_ancestor":
        source_manifest["reused_predictions"] = [{"id": "group/case", "input_verified": False}]
    if change != "missing_manifest":
        (reuse / "manifest.json").write_text(json.dumps(source_manifest), encoding="utf-8")
    manifest = build_manifest(workspace=workspace, cases=cases, reuse_dir=reuse, reuse_extract_dir=reuse,
                              settings={"modes": ["extract"], "match_mode": "llm"}, evaluator_paths=paths)
    assert manifest["reused_predictions"][0]["input_verified"] is False
    summary = _summary(manifest)
    summary["modes"] = ["extract"]
    assert "reused_prediction_input_unverified" in _reasons(_compare(summary, summary))


def test_same_conditions_pass_and_ignore_judge_wall_clock(tmp_path):
    baseline = _summary(_manifest(tmp_path))
    current = deepcopy(baseline)
    _extract(current)["score_elapsed_seconds"] = 9000
    current["cases"][0]["elapsed_seconds"] = 10000
    before = deepcopy(current)
    result = _compare(current, baseline)
    assert result["passed"]
    assert len(result["comparisons"]) == 5  # entities, relations, parse, chunk, extract
    assert current == before


def test_changed_extractor_options_allowed_but_judge_contract_change_rejected(tmp_path):
    baseline = _summary(_manifest(tmp_path / "base"))
    current = _summary(_manifest(tmp_path / "now"))
    current["manifest"]["settings"]["options"]["extract_version"] = "latest"
    assert _compare(current, baseline)["passed"]
    current["manifest"]["settings"]["judge"]["model"] = "judge-v2"
    current["manifest"]["scoring_contract"]["judge"]["model"] = "judge-v2"
    result = _compare(current, baseline)
    assert "scoring_contract_mismatch" in _reasons(result)


@pytest.mark.parametrize("change,reason", [("missing", "missing_manifest"), ("code", "evaluator_mismatch"),
                                           ("case", "case_set_mismatch"), ("data", "dataset_sha256_mismatch")])
def test_incompatible_or_missing_evidence_rejected(tmp_path, change, reason):
    baseline = _summary(_manifest(tmp_path))
    current = deepcopy(baseline)
    if change == "missing":
        current.pop("manifest")
    elif change == "code":
        current["manifest"]["evaluator"]["sha256"] = "different"
    elif change == "case":
        current["cases"][0]["case"] = "other"
    else:
        current["manifest"]["dataset_sha256"] = "different"
    result = _compare(current, baseline)
    assert not result["passed"]
    assert reason in _reasons(result)


def test_both_entity_and_relation_f1_are_paired_per_case(tmp_path):
    baseline = _summary(_manifest(tmp_path))
    current = deepcopy(baseline)
    _extract(current)["metrics"]["entities"]["f1"] = 0.78  # exactly allowed
    _extract(current)["metrics"]["relations"]["f1"] = 0.65
    result = _compare(current, baseline)
    regressions = [issue for issue in result["issues"] if issue["reason"] == "f1_regression"]
    assert len(regressions) == 1
    assert regressions[0]["metric"] == "relations.f1"
    assert regressions[0]["case"] == "group/case"


@pytest.mark.parametrize("side", ["current", "baseline"])
@pytest.mark.parametrize("change,reason", [("fallback", "scorer_mismatch"), ("failed", "unsuccessful_case"),
                                           ("missing", "missing_metrics"), ("unscored", "unscored_case")])
def test_bad_current_or_baseline_cannot_pass(tmp_path, side, change, reason):
    baseline = _summary(_manifest(tmp_path))
    current = deepcopy(baseline)
    target = {"current": current, "baseline": baseline}[side]
    if change == "fallback":
        _extract(target)["metrics"]["match_mode"] = "identity_fallback"
    elif change == "failed":
        target["cases"][0]["status"] = "failed"
    elif change == "missing":
        _extract(target).pop("metrics")
    else:
        target["cases"][0]["scored"] = False
    result = _compare(current, baseline)
    assert not result["passed"]
    assert reason in _reasons(result)


def test_stage_latency_relative_regression(tmp_path):
    baseline = _summary(_manifest(tmp_path))
    current = deepcopy(baseline)
    _extract(current)["elapsed_seconds"] = 24.0
    assert _compare(current, baseline)["passed"]
    _extract(current)["elapsed_seconds"] = 24.1
    result = _compare(current, baseline)
    assert "latency_regression" in _reasons(result)
    regression = next(issue for issue in result["issues"] if issue["reason"] == "latency_regression")
    assert regression["relative_increase"] == 0.205


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -1, True])
def test_missing_or_invalid_stage_latency_fails_closed(tmp_path, value):
    baseline = _summary(_manifest(tmp_path))
    current = deepcopy(baseline)
    _extract(current)["elapsed_seconds"] = value
    assert "missing_or_invalid_latency" in _reasons(_compare(current, baseline))


def test_reused_preprocessing_is_skipped_but_reused_inference_requires_source_time(tmp_path):
    baseline = _summary(_manifest(tmp_path))
    current = deepcopy(baseline)
    for summary in (baseline, current):
        for name in ("parse", "chunk"):
            summary["cases"][0]["backend"]["modules"][name] = {"status": "reused"}
        _extract(summary)["status"] = "reused"
    assert _compare(current, baseline)["passed"]
    _extract(current)["elapsed_seconds"] = None
    assert "missing_or_invalid_latency" in _reasons(_compare(current, baseline))


def test_zero_baseline_duration_and_changed_stage_execution_are_not_comparable(tmp_path):
    baseline = _summary(_manifest(tmp_path))
    current = deepcopy(baseline)
    _extract(baseline)["elapsed_seconds"] = 0
    current["cases"][0]["backend"]["modules"]["parse"]["status"] = "reused"
    reasons = _reasons(_compare(current, baseline))
    assert "nonpositive_baseline_latency" in reasons
    assert "stage_execution_mismatch" in reasons


def test_candidate_zero_duration_is_not_a_performance_improvement(tmp_path):
    baseline = _summary(_manifest(tmp_path))
    current = deepcopy(baseline)
    _extract(current)["elapsed_seconds"] = 0
    assert "nonpositive_current_latency" in _reasons(_compare(current, baseline))


@pytest.mark.parametrize("missing_module", [None, "backend_system", "plugin_extract"])
def test_pipeline_and_plugin_scores_are_compared_and_required(tmp_path, missing_module):
    settings = {"stage": "all", "modes": ["chunk-extract", "pipeline"], "match_mode": "llm"}
    baseline = _summary(_manifest(tmp_path, settings=settings))
    baseline.update(stage="all", modes=settings["modes"])
    baseline["cases"][0]["backend"]["system"] = deepcopy(_extract(baseline))
    baseline["cases"][0]["plugin"] = deepcopy(_extract(baseline))
    current = deepcopy(baseline)
    if missing_module == "backend_system":
        current["cases"][0]["backend"].pop("system")
    elif missing_module == "plugin_extract":
        current["cases"][0].pop("plugin")
    result = _compare(current, baseline)
    assert result["passed"] is (missing_module is None)
    if missing_module:
        assert any(issue["reason"] == "missing_metrics" and issue["module"] == missing_module for issue in result["issues"])
    else:
        f1_modules = {item["module"] for item in result["comparisons"] if item["metric"] == "entities.f1"}
        assert f1_modules == {"backend_extract", "backend_system", "plugin_extract"}


@pytest.mark.parametrize("kwargs", [{"max_f1_drop": -0.1}, {"max_f1_drop": 1.1},
                                    {"max_f1_drop": float("nan")}, {"max_latency_increase": -1},
                                    {"max_latency_increase": float("inf")}])
def test_invalid_thresholds_are_configuration_errors(kwargs):
    with pytest.raises(ValueError):
        _compare({}, {}, **kwargs)
