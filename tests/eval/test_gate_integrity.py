"""CLI integration contracts: a completed batch is not automatically a pass."""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from eval import run as eval_run


GOLDEN = {
    "entities": [{"type": "T", "name": "n1"}, {"type": "T", "name": "n2"}],
    "relations": [{
        "from_type": "T", "from_name": "n1", "rel_type": "links",
        "to_type": "T", "to_name": "n2",
    }],
}


@pytest.fixture
def cli(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    ontology = workspace / "g" / "ontology"
    ontology.mkdir(parents=True)
    (ontology / "onto.puml").write_text("@startuml\nentity T\n@enduml\n", encoding="utf-8")
    case = workspace / "g" / "c1"
    (case / "docs").mkdir(parents=True)
    (case / "docs" / "a.txt").write_text("n1 links to n2", encoding="utf-8")
    (case / "extract").mkdir()
    golden = case / "extract" / "golden.json"
    golden.write_text(json.dumps(GOLDEN), encoding="utf-8")
    (workspace / "params.yaml").write_text("eval:\n  match_mode: llm\n", encoding="utf-8")
    runs = tmp_path / "runs"
    monkeypatch.setattr(eval_run, "RUNS_DIR", runs)
    monkeypatch.setattr(eval_run, "RETRY_SLEEP_SECONDS", 0)

    def invoke(*args, match_mode="identity"):
        argv = ["run.py", "--workspace", str(workspace)]
        if match_mode is not None:
            argv.extend(["--match-mode", match_mode])
        monkeypatch.setattr(sys, "argv", [*argv, *args])
        return eval_run.main()

    def summary():
        paths = sorted(runs.glob("*/summary.json"))
        assert paths, "The completed run must persist summary.json"
        return json.loads(paths[-1].read_text(encoding="utf-8"))

    return SimpleNamespace(run=invoke, summary=summary, workspace=workspace, runs=runs, golden=golden)


def _backend(*, predicted=None, missing_metrics=False, pipeline_status=None):
    graph = deepcopy(GOLDEN if predicted is None else predicted)

    def invoke(**kwargs):
        metrics = None if missing_metrics else eval_run.maybe_score(kwargs["golden"], graph, kwargs["score_ctx"])
        result = {
            "status": pipeline_status or "succeeded",
            "nodes": len(graph["entities"]), "relationships": len(graph["relations"]),
            "metrics": metrics, "elapsed_seconds": 1.0,
        }
        if pipeline_status is not None:
            return {"system": result, "modules": {}}
        return {
            "system": {"status": "skipped"},
            "modules": {
                "parse": {"status": "succeeded", "elapsed_seconds": 1.0},
                "chunk": {"status": "succeeded", "elapsed_seconds": 1.0},
                "extract": result,
            },
            "markdown": "# n1 links to n2",
        }

    return invoke


def test_llm_fallback_is_a_gate_failure_and_cannot_enter_overall(cli, monkeypatch):
    judge = SimpleNamespace(
        model="test-judge", base_url="https://judge.invalid/v1", timeout=10,
        max_tokens=1024, response_format="json_schema", disable_thinking=True,
    )
    monkeypatch.setattr(eval_run, "build_judge_client", lambda _params: judge)
    monkeypatch.setattr(eval_run, "llm_judge_score_case", Mock(side_effect=RuntimeError("judge unavailable")))
    monkeypatch.setattr(eval_run, "run_backend_case", _backend())

    assert cli.run(match_mode="llm") == 4
    summary = cli.summary()
    assert summary["succeeded"] == 1
    assert summary["failed"] == 0
    assert summary["gate"]["passed"] is False
    assert summary["gate"]["quality"]["issues"][0]["reason"] == "scorer_mismatch"
    coverage = summary["quality"]["modules"]["backend_extract"]["coverage"]
    assert coverage["expected"] == 1
    assert coverage["fallback"] == 1
    assert coverage["scored"] == 0
    # The saved diagnostic retains the perfect exact score; the reported LLM
    # aggregate must not silently claim the same success.
    diagnostic = summary["cases"][0]["backend"]["modules"]["extract"]["metrics"]
    assert diagnostic["match_mode"] == "identity_fallback"
    assert diagnostic["entities"]["f1"] == 1.0
    assert summary["overall"]["backend_extract"]["entities"]["tp"] == 0
    assert summary["overall"]["backend_extract"]["entities"]["f1"] is None


@pytest.mark.parametrize("annotated", [True, False])
def test_require_scored_rejects_missing_scoring_even_when_backend_succeeds(cli, monkeypatch, annotated):
    if not annotated:
        cli.golden.unlink()
    monkeypatch.setattr(eval_run, "run_backend_case", _backend(missing_metrics=True))
    assert cli.run("--require-scored") == 4
    summary = cli.summary()
    assert summary["succeeded"] == 1
    assert summary["gate"]["passed"] is False
    reasons = {item["reason"] for item in summary["gate"]["quality"]["issues"]}
    assert "missing_metrics" in reasons
    if not annotated:
        assert "unscored_case" in reasons
    assert summary["quality"]["modules"]["backend_extract"]["coverage"]["missing"] == 1


def test_relation_threshold_blocks_entity_perfect_but_relation_empty_output(cli, monkeypatch):
    predicted = {"entities": GOLDEN["entities"], "relations": []}
    monkeypatch.setattr(eval_run, "run_backend_case", _backend(predicted=predicted))
    assert cli.run("--min-f1", "0.9", "--min-relation-f1", "0.5") == 4
    summary = cli.summary()
    assert summary["gate"]["min_f1"]["below"] == []
    assert summary["gate"]["quality"]["min_relation_f1"]["below"] == [
        {"case": "g/c1", "module": "backend_extract", "f1": 0.0}
    ]
    assert summary["overall"]["backend_extract"]["entities"]["f1"] == 1.0
    assert summary["overall"]["backend_extract"]["relations"]["f1"] == 0.0


def test_configured_llm_without_judge_fails_instead_of_switching_to_identity(cli, monkeypatch):
    judge = Mock(side_effect=RuntimeError("missing judge credentials"))
    backend = Mock(side_effect=AssertionError("Backend must not run after invalid judge configuration"))
    monkeypatch.setattr(eval_run, "build_judge_client", judge)
    monkeypatch.setattr(eval_run, "run_backend_case", backend)
    assert cli.run(match_mode=None) == 1
    judge.assert_called_once()
    backend.assert_not_called()
    assert not list(cli.runs.glob("*/summary.json"))


def test_dry_run_never_cleans_history_or_constructs_judge(cli, monkeypatch):
    old = cli.runs / "20000101-000000"
    old.mkdir(parents=True)
    marker = old / "retain.txt"
    marker.write_text("history", encoding="utf-8")
    cleanup = Mock(wraps=eval_run.cleanup_old_runs)
    judge = Mock(side_effect=AssertionError("dry-run must not construct a judge"))
    monkeypatch.setattr(eval_run, "cleanup_old_runs", cleanup)
    monkeypatch.setattr(eval_run, "build_judge_client", judge)
    assert cli.run("--dry-run", match_mode=None) == 0
    cleanup.assert_not_called()
    judge.assert_not_called()
    assert marker.read_text(encoding="utf-8") == "history"


@pytest.mark.parametrize("baseline_is_file", [True, False])
def test_baseline_is_protected_from_history_cleanup(cli, monkeypatch, baseline_is_file):
    old = cli.runs / "20000101-000000"
    old.mkdir(parents=True)
    baseline = old / "summary.json"
    baseline.write_text(json.dumps({"run_id": old.name, "cases": []}), encoding="utf-8")
    cleanup = Mock(wraps=eval_run.cleanup_old_runs)
    monkeypatch.setattr(eval_run, "cleanup_old_runs", cleanup)
    monkeypatch.setattr(eval_run, "run_backend_case", _backend())
    # This deliberately incomplete baseline cannot pass comparison, but it is
    # still protected while evaluating and when reporting the comparison error.
    assert cli.run("--baseline", str(baseline if baseline_is_file else old), "--runs-retention-days", "1") == 4
    cleanup.assert_called_once()
    assert old.resolve() in cleanup.call_args.kwargs["protected"]
    assert baseline.is_file()
    assert cli.summary()["baseline_comparison"]["passed"] is False


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
@pytest.mark.parametrize("option", [
    "--min-f1", "--min-relation-f1", "--max-f1-drop", "--max-latency-increase", "--max-case-seconds",
    "--max-stage-seconds",
])
def test_nonfinite_or_negative_thresholds_and_budgets_are_config_errors(cli, monkeypatch, option, value):
    backend = Mock(side_effect=AssertionError("Invalid budgets must fail before evaluation"))
    monkeypatch.setattr(eval_run, "run_backend_case", backend)
    argument = f"extract={value}" if option == "--max-stage-seconds" else value
    assert cli.run(f"{option}={argument}") == 1
    backend.assert_not_called()
    assert not list(cli.runs.glob("*/summary.json"))


def test_partial_pipeline_cannot_be_reported_as_success(cli, monkeypatch):
    backend = Mock(side_effect=_backend(pipeline_status="partial"))
    monkeypatch.setattr(eval_run, "run_backend_case", backend)
    assert cli.run("--mode", "pipeline") == 2
    assert backend.call_count == eval_run.MAX_ATTEMPTS
    summary = cli.summary()
    assert summary["failed"] == 1
    assert summary["succeeded"] == 0
    assert summary["cases"][0]["status"] == "failed"
    assert summary["cases"][0]["skipped_after_retries"] is True
    assert summary["quality"]["modules"]["backend_system"]["coverage"]["failed"] == 1
