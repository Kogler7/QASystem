from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from eval import run as eval_run
from eval.report import exit_code_for, render_report


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(eval_run, "RETRY_SLEEP_SECONDS", 0)


def _side(p: float, r: float, f1: float, *, golden: int = 10, predicted: int = 8) -> dict:
    return {
        "golden": golden,
        "predicted": predicted,
        "precision": p,
        "recall": r,
        "f1": f1,
        "accuracy": f1,
        "tp": 6,
        "fp": 2,
        "fn": 4,
    }


def _metrics(
    p: float,
    r: float,
    f1: float,
    *,
    match_mode: str = "identity",
    judge_error: str | None = None,
    golden: int = 10,
    predicted: int = 8,
) -> dict:
    payload = {
        "entities": _side(p, r, f1, golden=golden, predicted=predicted),
        "relations": _side(p, r, f1, golden=5, predicted=4),
        "match_mode": match_mode,
    }
    if judge_error:
        payload["judge_error"] = judge_error
    return payload


def _case(
    case: str,
    *,
    status: str = "succeeded",
    error: str | None = None,
    scored: bool = True,
    elapsed: float = 12.0,
    parse: float = 2.0,
    chunk: float = 1.0,
    extract: float = 8.0,
    metrics: dict | None = None,
    version: str = "latest",
    empty: bool = False,
    skipped_after_retries: bool = False,
    attempts: int = 1,
) -> dict:
    block = {
        "status": "succeeded",
        "nodes": 8,
        "relationships": 4,
        "metrics": metrics if metrics is not None else _metrics(0.8, 0.7, 0.75),
        "elapsed_seconds": extract,
        "extract_version": version,
    }
    record = {
        "group": "g",
        "case": case,
        "files": ["a.txt"],
        "scored": scored,
        "status": status,
        "error": error,
        "attempts": attempts,
        "elapsed_seconds": elapsed,
        "empty_extraction": empty,
        "extract_version": version,
        "backend": {
            "system": {"status": "skipped"},
            "modules": {
                "parse": {"status": "succeeded", "elapsed_seconds": parse},
                "chunk": {"status": "succeeded", "elapsed_seconds": chunk},
                "extract": block,
            },
        },
        "plugin": None,
    }
    if skipped_after_retries:
        record["skipped_after_retries"] = True
    if not scored:
        record["unscored_reason"] = "缺少 extract/golden.json"
    return record


def _summary() -> dict:
    cases = [
        _case("ok", elapsed=12.0, extract=8.0),
        _case(
            "empty",
            status="failed",
            error="empty_extraction",
            empty=True,
            elapsed=6.0,
            metrics=_metrics(0.0, 0.0, 0.0, golden=10, predicted=0),
        ),
        _case(
            "slow",
            elapsed=95.0,
            extract=90.0,
            metrics=_metrics(0.6, 0.5, 0.55, match_mode="identity_fallback", judge_error="judge HTTP 500: boom"),
        ),
    ]
    return {
        "run_id": "20260930-101010",
        "started_at": "2026-09-30T10:10:10",
        "finished_at": "2026-09-30T10:12:05",
        "elapsed_seconds": 115.0,
        "workspace": "D:/ws",
        "backend_url": "http://127.0.0.1:8006",
        "mcp_url": "http://127.0.0.1:8006/mcp",
        "stage": "backend",
        "modes": ["chunk-extract"],
        "match_mode": "identity",
        "groups": [{"name": "g", "cases": 3}],
        "cases": cases,
        "succeeded": 2,
        "failed": 1,
        "skipped": 0,
        "unscored": 0,
        "extract_versions": ["latest"],
        "overall": {
            "backend_extract": {
                "entities": _side(0.7, 0.6, 0.65, golden=30, predicted=20),
                "relations": _side(0.5, 0.4, 0.44, golden=15, predicted=10),
            },
            "backend_system": {
                "entities": _side(0.0, 0.0, 0.0, golden=0, predicted=0),
                "relations": _side(0.0, 0.0, 0.0, golden=0, predicted=0),
            },
            "plugin_extract": {
                "entities": _side(0.0, 0.0, 0.0, golden=0, predicted=0),
                "relations": _side(0.0, 0.0, 0.0, golden=0, predicted=0),
            },
        },
        "gate": {
            "empty_failures": ["g/empty"],
            "budget_violations": [
                {"case": "g/slow", "stage": "extract", "elapsed": 90.0, "budget": 60.0},
                {"case": "g/slow", "stage": "case", "elapsed": 95.0, "budget": 60.0},
            ],
            "budgets": {"case_seconds": 60.0, "stage_seconds": {"extract": 60.0}},
            "min_f1": {"threshold": 0.7, "below": [{"case": "g/empty", "f1": 0.0}, {"case": "g/slow", "f1": 0.55}]},
            "passed": False,
        },
    }


def test_render_report_header_and_gate():
    md = render_report(_summary())
    assert md.startswith("# Benchmark 评测报告 20260930-101010")
    assert "开始：2026-09-30T10:10:10" in md
    assert "结束：2026-09-30T10:12:05" in md
    assert "Backend：http://127.0.0.1:8006" in md
    assert "评分口径：identity" in md
    assert "1 个 case judge 失败降级 identity_fallback" in md
    assert "抽取版本：latest" in md
    # 有失败 case → 退出码 2 优先于门禁 4
    assert "退出码：2（有 case 失败" in md
    assert "门禁判定：未过" in md
    assert "`g/empty`" in md
    assert "实体 F1 阈值：0.7（已评分 case 实际最低 0.000）" in md
    assert "case≤60s" in md and "extract≤60s" in md
    assert "违规 2 项" in md


def test_render_report_overview_table():
    md = render_report(_summary())
    assert "## 总览" in md
    lines = [line for line in md.splitlines() if line.startswith("| 后台抽取 |")]
    assert len(lines) == 2  # 实体 + 关系
    assert "| 后台抽取 | 实体 | 30 | 20 | 0.700 | 0.600 | 0.650 |" in lines
    assert "| 后台抽取 | 关系 | 15 | 10 | 0.500 | 0.400 | 0.440 |" in lines
    # backend_system / plugin_extract 全零不列出
    assert "沉淀 knowledge-ingestion |" not in md
    assert "插件 kg_extract_v2 |" not in md


def test_render_report_per_case_rows_and_notes():
    md = render_report(_summary())
    assert "## 逐 case 明细" in md
    assert "| `g/ok` | 成功 | 0.800 / 0.700 / 0.750 |" in md
    # 失败 case 标出原因
    empty_row = next(line for line in md.splitlines() if line.startswith("| `g/empty` |"))
    assert "| 失败 |" in empty_row
    assert "empty_extraction" in empty_row
    # 各阶段耗时与总耗时
    ok_row = next(line for line in md.splitlines() if line.startswith("| `g/ok` |"))
    assert "| 2.0s | 1.0s | 8.0s | 12.0s | latest |" in ok_row
    # judge 降级备注
    slow_row = next(line for line in md.splitlines() if line.startswith("| `g/slow` |"))
    assert "judge 失败降级 identity" in slow_row


def test_render_report_issues_and_slow_top():
    md = render_report(_summary())
    assert "## 问题清单" in md
    assert "**失败 case（1）**" in md
    assert "**空抽取（1）**" in md
    assert "**judge 降级 / 错误（1）**" in md
    assert "judge HTTP 500: boom" in md
    assert "**耗时超预算（2）**" in md
    assert "`g/slow` extract 1m30.0s > 预算 1m00.0s" in md
    assert "**实体 F1 低于阈值 0.7（2）**" in md
    # 慢 case 排序：g/slow 第一
    slow_section = md.split("## 最慢 case Top 5", 1)[1]
    first_row = next(line for line in slow_section.splitlines() if line.startswith("| 1 |"))
    assert "`g/slow`" in first_row
    assert "1m35.0s" in first_row


def test_render_report_interrupted_and_empty_summary():
    md = render_report({"run_id": "20260930-120000", "interrupted": True, "cases": []})
    assert "130（用户中断" in md
    assert "（无 case）" in md
    assert "门禁：未配置" in md


def test_exit_code_for_semantics():
    assert exit_code_for({"failed": 1, "gate": {"passed": False}}) == 2
    assert exit_code_for({"failed": 0, "gate": {"passed": False}}) == 4
    assert exit_code_for({"failed": 0, "gate": {"passed": True}}) == 0
    assert exit_code_for({"failed": 0, "gate": {"passed": None}}) == 0
    assert exit_code_for({}) == 0


# --- main() 集成：批次结束落盘 report.md ---

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
            "extract": {
                "status": "succeeded",
                "nodes": 1,
                "relationships": 0,
                "metrics": {
                    "entities": {
                        "golden": 1,
                        "predicted": 1,
                        "precision": 1.0,
                        "recall": 1.0,
                        "f1": 1.0,
                        "accuracy": 1.0,
                        "tp": 1,
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
                },
                "elapsed_seconds": 1.0,
            },
        },
        "markdown": "# doc",
    }


def test_main_writes_report_md(monkeypatch, tmp_path):
    ws = _workspace(tmp_path)
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(eval_run, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(
        sys, "argv", ["run.py", "--workspace", str(ws), "--match-mode", "identity", "--min-f1", "0.5"]
    )
    with patch.object(eval_run, "run_backend_case", side_effect=_backend_ok):
        code = eval_run.main()
    assert code == 0
    run_dirs = [item for item in runs_dir.iterdir() if item.is_dir()]
    assert len(run_dirs) == 1
    report = run_dirs[0] / "report.md"
    assert report.is_file()
    md = report.read_text(encoding="utf-8")
    assert md.startswith("# Benchmark 评测报告")
    assert "退出码：0（全部通过）" in md
    assert "## 总览" in md and "## 逐 case 明细" in md and "## 问题清单" in md
    assert "`g/c1`" in md
    # summary.json 与报告同批落盘，且 gate 带配置的预算块
    summary = json.loads((run_dirs[0] / "summary.json").read_text(encoding="utf-8"))
    assert summary["gate"]["budgets"] == {"case_seconds": None, "stage_seconds": {}}
    assert summary["gate"]["min_f1"]["threshold"] == 0.5
