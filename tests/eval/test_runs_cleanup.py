from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from eval import run as eval_run


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(eval_run, "RETRY_SLEEP_SECONDS", 0)


NOW = datetime(2026, 9, 30, 12, 0, 0)
OLD_NAME = "20260821-120000"   # 40 天前
NEW_NAME = "20260920-120000"   # 10 天前


def _make_run(runs_dir: Path, name: str, *, files: dict[str, str] | None = None) -> Path:
    run_dir = runs_dir / name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(json.dumps({"run_id": name}), encoding="utf-8")
    for rel, text in (files or {}).items():
        path = run_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return run_dir


def test_cleanup_deletes_expired_keeps_recent_and_foreign(tmp_path):
    runs_dir = tmp_path / "runs"
    _make_run(runs_dir, OLD_NAME, files={"g/c1/scores.json": "{}"})
    _make_run(runs_dir, NEW_NAME)
    foreign = runs_dir / "not-a-run-id"
    foreign.mkdir()
    stray_file = runs_dir / "20260822-000000"  # 是文件不是目录，不删
    stray_file.write_text("x", encoding="utf-8")

    result = eval_run.cleanup_old_runs(runs_dir, 30, now=NOW)

    assert result["deleted"] == [OLD_NAME]
    assert not (runs_dir / OLD_NAME).exists()
    assert (runs_dir / NEW_NAME).is_dir()
    assert foreign.is_dir()
    assert stray_file.is_file()
    assert result["freed_bytes"] > 0
    assert result["errors"] == []
    assert result["kept_protected"] == []


def test_cleanup_disabled_when_retention_non_positive(tmp_path):
    runs_dir = tmp_path / "runs"
    _make_run(runs_dir, OLD_NAME)

    for retention in (0, -1):
        result = eval_run.cleanup_old_runs(runs_dir, retention, now=NOW)
        assert result["deleted"] == []
        assert (runs_dir / OLD_NAME).is_dir()


def test_cleanup_skips_protected_from_run(tmp_path):
    runs_dir = tmp_path / "runs"
    protected = _make_run(runs_dir, OLD_NAME)

    result = eval_run.cleanup_old_runs(runs_dir, 30, now=NOW, protected={protected})

    assert result["deleted"] == []
    assert result["kept_protected"] == [OLD_NAME]
    assert protected.is_dir()


def test_cleanup_missing_runs_dir_is_noop(tmp_path):
    result = eval_run.cleanup_old_runs(tmp_path / "missing", 30, now=NOW)
    assert result["deleted"] == []
    assert result["errors"] == []


def test_cleanup_falls_back_to_mtime_for_unparseable_run_id(tmp_path):
    runs_dir = tmp_path / "runs"
    weird = _make_run(runs_dir, "20269999-999999")  # 匹配命名格式但不是合法日期
    old_ts = datetime(2026, 8, 1).timestamp()
    for item in [weird, weird / "summary.json"]:
        os.utime(item, (old_ts, old_ts))

    result = eval_run.cleanup_old_runs(runs_dir, 30, now=NOW)

    assert result["deleted"] == ["20269999-999999"]
    assert not weird.exists()


def test_cleanup_keeps_dir_within_retention(tmp_path):
    runs_dir = tmp_path / "runs"
    _make_run(runs_dir, NEW_NAME)
    result = eval_run.cleanup_old_runs(runs_dir, 30, now=NOW)
    assert result["deleted"] == []
    assert (runs_dir / NEW_NAME).is_dir()


# --- main() 集成：启动时清理过期批次；--runs-retention-days<=0 关闭 ---

GOLDEN = {"entities": [{"type": "T", "name": "n1"}], "relations": []}


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "eval_ws"
    ontology = ws / "g" / "ontology"
    ontology.mkdir(parents=True, exist_ok=True)
    (ontology / "onto.puml").write_text("@startuml\nentity T\n@enduml\n", encoding="utf-8")
    case_dir = ws / "g" / "c1"
    (case_dir / "docs").mkdir(parents=True, exist_ok=True)
    (case_dir / "docs" / "a.txt").write_text("content of c1", encoding="utf-8")
    (case_dir / "extract").mkdir(parents=True, exist_ok=True)
    (case_dir / "extract" / "golden.json").write_text(json.dumps(GOLDEN, ensure_ascii=False), encoding="utf-8")
    return ws


def _backend_ok(**_kw) -> dict:
    return {
        "system": {"status": "skipped"},
        "modules": {
            "parse": {"status": "succeeded", "elapsed_seconds": 1.0},
            "chunk": {"status": "succeeded", "elapsed_seconds": 1.0},
            "extract": {"status": "succeeded", "nodes": 1, "relationships": 0, "metrics": None, "elapsed_seconds": 1.0},
        },
        "markdown": "# doc",
    }


def _run_main(monkeypatch, tmp_path: Path, ws: Path, *argv) -> int:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(eval_run, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(
        sys, "argv", ["run.py", "--workspace", str(ws), "--match-mode", "identity", *argv]
    )
    with patch.object(eval_run, "run_backend_case", side_effect=_backend_ok):
        return eval_run.main()


def test_main_cleans_expired_runs_on_startup(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    runs_dir = tmp_path / "runs"
    _make_run(runs_dir, OLD_NAME)

    code = _run_main(monkeypatch, tmp_path, ws)

    assert code == 0
    assert not (runs_dir / OLD_NAME).exists()
    remaining = [item.name for item in runs_dir.iterdir() if item.is_dir()]
    assert len(remaining) == 1 and remaining[0] != OLD_NAME  # 只有本次新批次


def test_main_retention_cli_zero_disables_cleanup(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    runs_dir = tmp_path / "runs"
    _make_run(runs_dir, OLD_NAME)

    code = _run_main(monkeypatch, tmp_path, ws, "--runs-retention-days", "0")

    assert code == 0
    assert (runs_dir / OLD_NAME).is_dir()


def test_main_skips_cleanup_for_from_run_reference(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    runs_dir = tmp_path / "runs"
    referenced = _make_run(runs_dir, OLD_NAME)
    (referenced / "g" / "c1").mkdir(parents=True, exist_ok=True)
    (referenced / "g" / "c1" / "chunks.json").write_text(
        json.dumps({"data": {"items": [{"text": "hello"}]}}), encoding="utf-8"
    )

    code = _run_main(monkeypatch, tmp_path, ws, "--mode", "extract", "--from-run", OLD_NAME)

    assert code == 0
    assert referenced.is_dir()  # 被 --from-run 引用，过期也跳过
