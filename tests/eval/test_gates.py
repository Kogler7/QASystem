from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from eval import run as eval_run


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(eval_run, "RETRY_SLEEP_SECONDS", 0)


GOLDEN = {"entities": [{"type": "T", "name": "n1"}], "relations": []}


def _metrics(f1: float, *, golden: int = 1, predicted: int = 1) -> dict:
    return {
        "entities": {
            "golden": golden,
            "predicted": predicted,
            "precision": f1,
            "recall": f1,
            "f1": f1,
            "accuracy": f1,
            "tp": 0,
            "fp": 0,
            "fn": 0,
        },
        "relations": {
            "golden": 0,
            "predicted": 0,
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "accuracy": 0.0,
            "tp": 0,
            "fp": 0,
            "fn": 0,
        },
    }


def _workspace(tmp_path: Path) -> Path:
    """最小评测工作区：一个类型目录 + 类型级本体。"""
    ws = tmp_path / "eval_ws"
    ontology = ws / "g" / "ontology"
    ontology.mkdir(parents=True, exist_ok=True)
    (ontology / "onto.puml").write_text("@startuml\nentity T\n@enduml\n", encoding="utf-8")
    return ws


def _make_case(ws: Path, case: str, *, golden) -> None:
    case_dir = ws / "g" / case
    (case_dir / "docs").mkdir(parents=True, exist_ok=True)
    (case_dir / "docs" / "a.txt").write_text(f"content of {case}", encoding="utf-8")
    if golden is not None:
        (case_dir / "extract").mkdir(parents=True, exist_ok=True)
        (case_dir / "extract" / "golden.json").write_text(
            json.dumps(golden, ensure_ascii=False), encoding="utf-8"
        )


def _backend_ok(*, nodes: int, metrics, extract_elapsed: float = 1.0) -> dict:
    return {
        "system": {"status": "skipped"},
        "modules": {
            "parse": {"status": "succeeded", "elapsed_seconds": 1.0},
            "chunk": {"status": "succeeded", "elapsed_seconds": 1.0},
            "extract": {
                "status": "succeeded",
                "nodes": nodes,
                "relationships": 0,
                "metrics": metrics,
                "elapsed_seconds": extract_elapsed,
            },
        },
        "markdown": "# doc",
    }


def _run_eval(monkeypatch, tmp_path: Path, ws: Path, backend_fn, *argv):
    """以 fake backend 跑 main()，返回 (退出码, 落盘的 summary.json)。"""
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(eval_run, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--workspace", str(ws), "--match-mode", "identity", *argv],
    )
    with patch.object(eval_run, "run_backend_case", side_effect=backend_fn):
        code = eval_run.main()
    summaries = sorted(runs_dir.glob("*/summary.json"))
    assert summaries, "summary.json 未落盘"
    payload = json.loads(summaries[-1].read_text(encoding="utf-8"))
    return code, payload


def test_zero_node_scored_case_fails_with_exit_2(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    _make_case(ws, "c1", golden=GOLDEN)
    code, summary = _run_eval(
        monkeypatch, tmp_path, ws, lambda **kw: _backend_ok(nodes=0, metrics=_metrics(0.0))
    )
    assert code == 2
    assert summary["failed"] == 1
    case = summary["cases"][0]
    assert case["status"] == "failed"
    assert case["error"] == "empty_extraction"
    assert case["empty_extraction"] is True
    assert summary["gate"]["empty_failures"] == ["g/c1"]


def test_no_fail_on_empty_keeps_case_success(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    _make_case(ws, "c1", golden=GOLDEN)
    code, summary = _run_eval(
        monkeypatch,
        tmp_path,
        ws,
        lambda **kw: _backend_ok(nodes=0, metrics=_metrics(0.0)),
        "--no-fail-on-empty",
    )
    assert code == 0
    case = summary["cases"][0]
    assert case["status"] == "succeeded"
    assert case["error"] is None
    assert case["empty_extraction"] is True
    assert summary["failed"] == 0
    # 仍记录进门禁清单，但 passed 只看耗时预算与 min-f1
    assert summary["gate"]["empty_failures"] == ["g/c1"]
    assert summary["gate"]["passed"] is True


def test_unscored_empty_golden_case_untouched(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    _make_case(ws, "c1", golden=None)  # 无 golden.json → 不评分

    def backend_fn(**kw):
        block = _backend_ok(nodes=0, metrics=None)
        block["modules"]["extract"]["metrics"] = None
        return block

    code, summary = _run_eval(monkeypatch, tmp_path, ws, backend_fn)
    assert code == 0
    case = summary["cases"][0]
    assert case["scored"] is False
    assert case["status"] == "succeeded"
    assert summary["unscored"] == 1
    assert summary["gate"]["empty_failures"] == []
    # 全部未评分：门禁不算过也不算不过
    assert summary["gate"]["passed"] is None


def test_case_budget_violation_exit_4(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    _make_case(ws, "c1", golden=GOLDEN)
    monkeypatch.setattr(eval_run, "elapsed_seconds", lambda _started: 42.0)
    code, summary = _run_eval(
        monkeypatch,
        tmp_path,
        ws,
        lambda **kw: _backend_ok(nodes=2, metrics=_metrics(1.0)),
        "--max-case-seconds",
        "10",
    )
    assert code == 4
    case = summary["cases"][0]
    assert case["status"] == "succeeded"  # 超预算不改 case 状态
    assert summary["failed"] == 0
    assert summary["gate"]["budget_violations"] == [
        {"case": "g/c1", "stage": "case", "elapsed": 42.0, "budget": 10.0}
    ]
    assert summary["gate"]["passed"] is False


def test_stage_budget_violation_exit_4(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    _make_case(ws, "c1", golden=GOLDEN)
    code, summary = _run_eval(
        monkeypatch,
        tmp_path,
        ws,
        lambda **kw: _backend_ok(nodes=2, metrics=_metrics(1.0), extract_elapsed=500.0),
        "--max-stage-seconds",
        "parse=60, extract = 300",  # 容忍空格
    )
    assert code == 4
    violations = summary["gate"]["budget_violations"]
    assert {"case": "g/c1", "stage": "extract", "elapsed": 500.0, "budget": 300.0} in violations
    # parse 阶段 1.0s 未超 60s 预算
    assert [v for v in violations if v["stage"] == "parse"] == []
    assert summary["gate"]["passed"] is False


def test_min_f1_below_threshold_exit_4(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    _make_case(ws, "c1", golden=GOLDEN)
    code, summary = _run_eval(
        monkeypatch,
        tmp_path,
        ws,
        lambda **kw: _backend_ok(nodes=1, metrics=_metrics(0.4)),
        "--min-f1",
        "0.5",
    )
    assert code == 4
    assert summary["failed"] == 0
    assert summary["gate"]["min_f1"]["threshold"] == 0.5
    assert summary["gate"]["min_f1"]["below"] == [{"case": "g/c1", "f1": 0.4}]
    assert summary["gate"]["passed"] is False


def test_all_pass_exit_0(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    _make_case(ws, "c1", golden=GOLDEN)
    code, summary = _run_eval(
        monkeypatch,
        tmp_path,
        ws,
        lambda **kw: _backend_ok(nodes=2, metrics=_metrics(1.0)),
        "--min-f1",
        "0.5",
        "--max-case-seconds",
        "600",
        "--max-stage-seconds",
        "extract=300",
    )
    assert code == 0
    gate = summary["gate"]
    assert gate["passed"] is True
    assert gate["empty_failures"] == []
    assert gate["budget_violations"] == []
    assert gate["min_f1"]["below"] == []


def test_case_failure_takes_priority_over_gate(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    _make_case(ws, "bad", golden=GOLDEN)
    _make_case(ws, "slow", golden=GOLDEN)

    def backend_fn(**kw):
        if kw["out_dir"].name == "bad":
            return {
                "system": {"status": "skipped"},
                "modules": {
                    "parse": {"status": "succeeded"},
                    "chunk": {"status": "succeeded"},
                    "extract": {"status": "failed", "message": "boom", "elapsed_seconds": 1.0},
                },
                "markdown": "# doc",
            }
        return _backend_ok(nodes=2, metrics=_metrics(1.0), extract_elapsed=999.0)

    code, summary = _run_eval(
        monkeypatch, tmp_path, ws, backend_fn, "--max-stage-seconds", "extract=10"
    )
    assert code == 2  # 有失败 case 时优先级高于门禁
    assert summary["failed"] == 1
    by_name = {item["case"]: item for item in summary["cases"]}
    assert by_name["bad"]["status"] == "failed"
    assert by_name["slow"]["status"] == "succeeded"
    # 预算违规仍记录进门禁，便于发布排查
    assert [v for v in summary["gate"]["budget_violations"] if v["case"] == "g/slow"]
