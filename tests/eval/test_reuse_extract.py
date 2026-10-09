from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from eval import run as eval_run


GOLDEN = {"entities": [{"type": "T", "name": "n1"}], "relations": []}
ENVELOPE = {"data": {"graph": GOLDEN, "quality": {"extract_version": "v3.1"}}}


def _save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _scores(*, case=130.0, extract=100.0, score=20.0) -> dict:
    return {
        "elapsed_seconds": case,
        "backend": {"modules": {"extract": {
            "elapsed_seconds": extract,
            "score_elapsed_seconds": score,
        }}},
    }


def _backend(tmp_path: Path, source: Path, monkeypatch):
    chunks = tmp_path / "chunks"
    _save(chunks / "chunks.json", {"data": {"items": [{"text": "固定切片"}]}})
    backend_call = Mock(return_value=ENVELOPE)
    monkeypatch.setattr(eval_run, "extract_graphs", backend_call)
    result = eval_run.run_backend_case(
        base_url="http://unused", ontology="ontology", sources=[], options={}, params={},
        golden=GOLDEN, out_dir=tmp_path / "new", score_ctx={"match_mode": "identity"},
        modes=["extract"], reuse_dir=chunks, reuse_extract_dir=source,
    )
    return result, backend_call


def test_reuse_extract_scores_copy_without_backend_and_retains_source_timing(tmp_path, monkeypatch):
    source = tmp_path / "old"
    _save(source / "backend_extract.json", ENVELOPE)
    _save(source / "scores.json", _scores())
    before = {p.name: p.read_bytes() for p in source.iterdir()}

    result, backend_call = _backend(tmp_path, source, monkeypatch)

    backend_call.assert_not_called()
    block = result["modules"]["extract"]
    assert block["status"] == "reused"
    assert block["nodes"] == 1
    assert block["metrics"]["entities"]["f1"] == 1
    assert block["source"] == str(source / "backend_extract.json")
    assert block["elapsed_seconds"] == 100.0
    assert block["source_case_elapsed_seconds"] == 130.0
    assert block["extract_version"] == "v3.1"
    assert "source_timing_missing" not in block
    assert json.loads((tmp_path / "new" / "backend_extract.json").read_text()) == ENVELOPE
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before

    record = {"backend": result}
    eval_run.apply_reused_case_timing(record, 15.0)
    assert record["elapsed_seconds"] == 125.0
    assert record["rerun_elapsed_seconds"] == 15.0
    assert record["source_extract_elapsed_seconds"] == 100.0
    gate = eval_run.build_run_gate(
        {"cases": [{**record, "scored": True}]},
        min_f1=0, max_case_seconds=120, stage_budgets={"extract": 90},
    )
    assert {item["stage"] for item in gate["budget_violations"]} == {"case", "extract"}


def test_missing_source_case_extracts_normally(tmp_path, monkeypatch):
    result, backend_call = _backend(tmp_path, tmp_path / "missing", monkeypatch)
    backend_call.assert_called_once()
    assert result["modules"]["extract"]["status"] == "succeeded"
    assert "reuse_extract_from" not in result["modules"]["extract"]


@pytest.mark.parametrize("content", ["{broken", "[]", "null"])
def test_broken_saved_extract_fails_without_backend(tmp_path, monkeypatch, content):
    source = tmp_path / "old"
    source.mkdir()
    (source / "backend_extract.json").write_text(content, encoding="utf-8")
    backend_call = Mock(side_effect=AssertionError("不应重抽"))
    monkeypatch.setattr(eval_run, "extract_graphs", backend_call)
    with pytest.raises(RuntimeError, match="复用抽取产物"):
        eval_run._reuse_extract_case(source, GOLDEN, tmp_path / "new", {"match_mode": "identity"})
    backend_call.assert_not_called()
    assert not (tmp_path / "new" / "backend_extract.json").exists()


def test_source_without_scores_keeps_unknown_timing(tmp_path, monkeypatch):
    source = tmp_path / "old"
    _save(source / "backend_extract.json", ENVELOPE)
    result, backend_call = _backend(tmp_path, source, monkeypatch)
    backend_call.assert_not_called()
    block = result["modules"]["extract"]
    assert block["elapsed_seconds"] is None
    assert block["source_timing_missing"] is True
    record = {"backend": result}
    eval_run.apply_reused_case_timing(record, 15.0)
    assert record["elapsed_seconds"] is None
    assert record["source_case_elapsed_seconds"] is None
    assert record["source_extract_elapsed_seconds"] is None
    assert record["rerun_elapsed_seconds"] == 15.0
    assert record["source_timing_missing"] is True


@pytest.mark.parametrize("case_budget,stage_budgets,blocked", [
    (1800, {}, True), (None, {"extract": 900}, True),
    (None, {}, False), (None, {"parse": 300}, False),
])
def test_unknown_source_timing_cannot_pass_requested_time_gate(case_budget, stage_budgets, blocked):
    gate = eval_run.build_run_gate(
        {"cases": [{
            "group": "g", "case": "c1", "scored": True,
            "source_timing_missing": True, "elapsed_seconds": None,
        }]},
        min_f1=0.5, max_case_seconds=case_budget, stage_budgets=stage_budgets,
    )
    assert gate["timing_unknown"] == (["g/c1"] if blocked else [])
    assert gate["passed"] is not blocked
    assert gate["budget_violations"] == []


@pytest.mark.parametrize("case,score", [(None, 20), (130, None), (10, 20)])
def test_incomplete_source_case_timing_is_not_invented(tmp_path, monkeypatch, case, score):
    source = tmp_path / "old"
    _save(source / "backend_extract.json", ENVELOPE)
    _save(source / "scores.json", _scores(case=case, score=score))
    result, _ = _backend(tmp_path, source, monkeypatch)
    record = {"backend": result}
    eval_run.apply_reused_case_timing(record, 15.0)
    assert record["elapsed_seconds"] is None
    assert record["source_extract_elapsed_seconds"] == 100.0
    assert record["source_timing_missing"] is True


@pytest.mark.parametrize("args", [
    [], ["--mode", "extract"],
    ["--mode", "extract", "--from-run", "old", "--stage", "all"],
    ["--mode", "extract", "--from-run", "old", "--system"],
    ["--mode", "chunk-extract", "--from-run", "old"],
])
def test_invalid_reuse_arguments_fail_before_cleanup_or_judge(tmp_path, monkeypatch, args):
    cleanup = Mock(side_effect=AssertionError("不得清理"))
    judge = Mock(side_effect=AssertionError("不得初始化 judge"))
    monkeypatch.setattr(eval_run, "cleanup_old_runs", cleanup)
    monkeypatch.setattr(eval_run, "build_judge_client", judge)
    monkeypatch.setattr(sys, "argv", ["run.py", "--reuse-extract-from", str(tmp_path), *args])
    assert eval_run.main() == 1
    cleanup.assert_not_called()
    judge.assert_not_called()


def test_main_protects_source_run_records_provenance_and_case_timing(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    ontology = workspace / "g" / "ontology" / "ontology.puml"
    ontology.parent.mkdir(parents=True)
    ontology.write_text("@startuml\nentity T\n@enduml", encoding="utf-8")
    document = workspace / "g" / "c1" / "docs" / "a.txt"
    document.parent.mkdir(parents=True)
    document.write_text("hello", encoding="utf-8")
    _save(workspace / "g" / "c1" / "extract" / "golden.json", GOLDEN)
    runs = tmp_path / "runs"
    chunks_run = runs / "20000101-000001"
    source_run = runs / "20000101-000002"
    _save(chunks_run / "g" / "c1" / "chunks.json", {"data": {"items": [{"text": "hello"}]}})
    _save(source_run / "g" / "c1" / "backend_extract.json", ENVELOPE)
    _save(source_run / "g" / "c1" / "scores.json", _scores())
    source_params = {"options": {"extraction": {"model": "original-model"}}}
    _save(source_run / "params.resolved.json", source_params)
    before = {str(p.relative_to(source_run)): p.read_bytes() for p in source_run.rglob("*") if p.is_file()}
    unused = runs / "20000101-000003"
    unused.mkdir()
    monkeypatch.setattr(eval_run, "RUNS_DIR", runs)
    backend_call = Mock(side_effect=AssertionError("复用不得调用 Backend"))
    monkeypatch.setattr(eval_run, "extract_graphs", backend_call)
    monkeypatch.setattr(sys, "argv", [
        "run.py", "--workspace", str(workspace), "--match-mode", "identity",
        "--mode", "extract", "--from-run", str(chunks_run),
        "--reuse-extract-from", str(source_run), "--max-case-seconds", "90",
    ])
    assert eval_run.main() == 4
    backend_call.assert_not_called()
    assert source_run.is_dir() and chunks_run.is_dir()
    assert not unused.exists()
    assert {str(p.relative_to(source_run)): p.read_bytes() for p in source_run.rglob("*") if p.is_file()} == before
    new_run = next(p for p in runs.iterdir() if p not in {source_run, chunks_run})
    summary = json.loads((new_run / "summary.json").read_text())
    record = summary["cases"][0]
    assert record["status"] == "succeeded"
    assert record["source_case_elapsed_seconds"] == 130
    assert record["source_extract_elapsed_seconds"] == 100
    assert record["elapsed_seconds"] == round(110 + record["rerun_elapsed_seconds"], 3)
    assert summary["reuse_extract_from"] == str(source_run)
    params = json.loads((new_run / "params.resolved.json").read_text())
    assert params["reuse_extract_source_params"] == str(source_run / "params.resolved.json")
    assert json.loads((new_run / "reuse_extract.params.resolved.json").read_text()) == source_params
