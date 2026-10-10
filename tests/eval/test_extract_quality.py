"""Backend best-effort graphs retain partial failure through scoring and reuse."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
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


def _quality(status):
    return {
        "status": status, "extract_version": "v3.1", "graph_publishable": False,
        "error_code": "LLM_TIMEOUT",
        "verification": {"valid": False, "failed_chunks": ["chunk-0001"]},
    }


def _save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("path", ["live", "saved"])
@pytest.mark.parametrize("status", ["partial", "failed"])
@pytest.mark.parametrize("quality_location", ["data", "nested_data"])
def test_non_success_quality_keeps_metrics_and_original_evidence(
    tmp_path, monkeypatch, path, status, quality_location,
):
    quality = _quality(status)
    data = {"graph": deepcopy(GOLDEN), "quality": quality}
    envelope = {"data": data if quality_location == "data" else {"data": data}}
    original = deepcopy(envelope)
    if path == "live":
        monkeypatch.setattr(eval_run, "extract_graphs", Mock(return_value=envelope))
        block = eval_run._extract_items(
            "http://unused", "ontology", [{"text": "n1 links to n2"}], {}, {},
            GOLDEN, tmp_path, {"match_mode": "identity"},
        )
    else:
        block = eval_run._extract_block_from_envelope(
            envelope, GOLDEN, tmp_path, {"match_mode": "identity"},
            status="reused", source="saved.json", elapsed=3,
        )
    assert block["status"] == status
    assert block["quality_status"] == status
    assert block["quality"] == quality
    assert block["failed_chunks"] == ["chunk-0001"]
    assert block["extract_version"] == "v3.1"
    assert block["nodes"] == 2
    assert block["metrics"]["entities"]["f1"] == 1
    assert block["metrics"]["relations"]["f1"] == 1
    assert eval_run.stage_failed({"modules": {"extract": block}})
    # Result metadata must not mutate the persisted Backend response.
    block["quality"]["verification"]["failed_chunks"].append("changed")
    block["failed_chunks"].append("changed")
    assert envelope == original


@pytest.mark.parametrize("status", ["succeeded", "reused"])
def test_successful_quality_retains_requested_execution_status(tmp_path, status):
    quality = {"status": "succeeded", "failed_chunks": [], "graph_publishable": True}
    block = eval_run._extract_block_from_envelope(
        {"data": {"graph": GOLDEN, "quality": quality}}, GOLDEN, tmp_path,
        {"match_mode": "identity"}, status=status, elapsed=3,
    )
    assert block["status"] == status
    assert block["quality_status"] == "succeeded"
    assert block["quality"] == quality
    assert block["failed_chunks"] == []
    assert not eval_run.stage_failed(block)


@pytest.mark.parametrize("reuse_previous_run", [False, True])
@pytest.mark.parametrize("status", ["partial", "failed"])
def test_best_effort_graph_fails_cli_gate_even_with_perfect_score(
    tmp_path, monkeypatch, reuse_previous_run, status,
):
    workspace = tmp_path / "workspace"
    case = workspace / "g/c1"
    (case / "docs").mkdir(parents=True)
    (case / "docs/a.txt").write_text("n1 links to n2", encoding="utf-8")
    ontology = workspace / "g/ontology"
    ontology.mkdir()
    (ontology / "onto.puml").write_text("@startuml\nentity T\n@enduml", encoding="utf-8")
    _save(case / "extract/golden.json", GOLDEN)
    chunks = tmp_path / "frozen"
    _save(chunks / "g/c1/chunks.json", {"data": {"items": [{"text": "n1 links to n2"}]}})
    envelope = {"data": {"graph": GOLDEN, "quality": _quality(status)}}
    backend_call = Mock(return_value=envelope)
    monkeypatch.setattr(eval_run, "extract_graphs", backend_call)
    monkeypatch.setattr(eval_run, "http_request", Mock(side_effect=AssertionError("No real HTTP allowed")))
    monkeypatch.setattr(eval_run, "RETRY_SLEEP_SECONDS", 0)
    runs = tmp_path / "runs"
    monkeypatch.setattr(eval_run, "RUNS_DIR", runs)
    args = ["run.py", "--workspace", str(workspace), "--match-mode", "identity",
            "--mode", "extract", "--from-run", str(chunks), "--min-f1", "0.5",
            # Isolate execution quality from legacy reuse provenance checks.
            "--no-require-scored"]
    if reuse_previous_run:
        previous = tmp_path / "previous"
        _save(previous / "g/c1/backend_extract.json", envelope)
        # Old evaluator incorrectly marked the partial graph succeeded; the
        # raw Backend quality takes precedence when evaluating it again.
        _save(previous / "g/c1/scores.json", {
            "status": "succeeded", "elapsed_seconds": 4,
            "backend": {"modules": {"extract": {
                "status": "succeeded", "elapsed_seconds": 3, "score_elapsed_seconds": 1,
            }}},
        })
        args.extend(["--reuse-extract-from", str(previous)])
    monkeypatch.setattr(sys, "argv", args)

    assert eval_run.main() == 2
    assert backend_call.call_count == (0 if reuse_previous_run else 1)
    run_dir = next(runs.iterdir())
    summary = json.loads((run_dir / "summary.json").read_text())
    record = summary["cases"][0]
    block = record["backend"]["modules"]["extract"]
    assert record["status"] == "failed"
    assert record["attempts"] == eval_run.MAX_ATTEMPTS
    assert block["status"] == status  # Also survives same-run cache retries.
    assert block["quality"] == envelope["data"]["quality"]
    assert block["failed_chunks"] == ["chunk-0001"]
    assert block["metrics"]["entities"]["f1"] == 1
    assert block["metrics"]["relations"]["f1"] == 1
    assert summary["failed"] == 1 and summary["succeeded"] == 0
    assert summary["gate"]["passed"] is False
    assert summary["gate"]["min_f1"]["below"] == []
    assert summary["gate"]["quality"]["passed"] is True
    coverage = summary["quality"]["modules"]["backend_extract"]["coverage"]
    assert coverage["failed"] == 1
    assert coverage["scored"] == 1
    assert summary["overall"]["backend_extract"]["entities"]["f1"] == 1
    assert json.loads((run_dir / "g/c1/backend_extract.json").read_text()) == envelope
