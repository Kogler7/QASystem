from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from eval import run as eval_run
from eval.report import exit_code_for, render_report


def test_report_keeps_gate_out_of_markdown():
    summary = _summary()
    summary["failed"] = 0
    summary["gate"] = {"passed": False, "timing_unknown": ["g/unfinished"]}
    report = render_report(summary)
    assert "## 门禁" not in report
    assert "原抽取耗时记录缺失" not in report
    assert exit_code_for(summary) == 4


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


def test_render_report_matches_reference_sections():
    md = render_report(_summary())
    assert md.startswith("# 知识抽取评测报告（eval 数据集）")
    assert [line for line in md.splitlines() if line.startswith("## ")] == [
        "## 0. 报告信息", "## 1. 评测目的与范围", "## 2. 评测口径",
        "## 3. 数据集总览", "## 4. 分数据集说明与评测数据", "## 5. 横向对比与结论",
    ]
    assert "| 抽取算法版本 | latest |" in md
    assert "| 后端环境 | http://127.0.0.1:8006 |" in md
    assert "本批次评分口径：**identity**" in md
    assert "1 个 case judge 失败降级 identity_fallback" in md
    assert "## 门禁" not in md
    assert "待补充" in md


def test_report_group_summary_and_copyable_case_tables():
    from eval.quality import build_quality_summary
    summary = _summary()
    summary["cases"][0]["group"] = "official"
    summary["cases"][0]["case"] = "official_01"
    summary["cases"][1]["group"] = "rule"
    summary["cases"][1]["case"] = "rule_01"
    md = render_report(summary)
    assert "| 数据集 | 实体 P | 实体 R | 实体 F1 | 关系 P | 关系 R | 关系 F1 | 备注 |" in md
    assert "| case | 状态 | P | R | F1 | 耗时(s) | 备注 |" in md
    assert "| official_01 | 成功 | 0.800 | 0.700 | 0.750 | 12.0 | — |" in md
    assert "| rule_01 | 失败 |" in md
    assert "**小计（micro）**" in md
    assert "后台抽取 · 实体：单 case 指标" in md
    assert "后台抽取 · 关系：单 case 指标" in md
    stats = build_quality_summary(summary)["modules"]["backend_extract"]["by_group"]["official"]
    ent, rel = (stats["micro"][side] for side in ("entities", "relations"))
    assert (f"| official | {ent['precision']:.3f} | {ent['recall']:.3f} | {ent['f1']:.3f} | "
            f"{rel['precision']:.3f} | {rel['recall']:.3f} | {rel['f1']:.3f} | 有效评分 1/1 |") in md
    assert "| g | 待补充 | 1 | TXT | 待补充 | 10 | 5 | §4.1 |" in md


def test_report_fallback_and_missing_not_pasted_as_formal_scores():
    summary = _summary()
    summary["match_mode"] = "llm"
    summary["cases"][0]["backend"]["modules"]["extract"]["metrics"]["match_mode"] = "llm"
    summary["cases"][1]["backend"]["modules"]["extract"].pop("metrics")
    md = render_report(summary)
    assert "| slow | 成功 | — | — | — | 95.0 | judge 降级 identity_fallback，不计入正式汇总 |" in md
    assert "| empty | 失败 | — | — | — | 6.0 | 未评分；空抽取；empty_extraction |" in md
    assert "有效评分 1/3" in md
    assert "本批次评分口径：**llm**" in md
    assert "| g | 待补充 | 3 | TXT | 待补充 | 待补充 | 待补充 |" in md


def test_report_relation_free_and_hallucinated_relations():
    summary = _summary()
    summary["cases"] = [_case("no_relations")]
    relation = summary["cases"][0]["backend"]["modules"]["extract"]["metrics"]["relations"]
    relation.update(golden=0, predicted=0, tp=0, fp=0, fn=0, precision=0, recall=0, f1=0)
    md = render_report(summary)
    assert "| no_relations | 成功 | — | — | — | 12.0 | — |" in md
    relation.update(predicted=2, fp=2)
    md = render_report(summary)
    assert "| no_relations | 成功 | 0.000 | 0.000 | 0.000 | 12.0 | — |" in md


def test_report_pipeline_plugin_and_dataset_counts_not_duplicated():
    summary = _summary()
    summary.update(stage="all", modes=["pipeline"])
    record = _case("multi")
    record["backend"]["modules"].pop("extract")
    record["backend"]["system"] = {"status": "succeeded", "metrics": _metrics(0.8, 0.7, 0.75)}
    record["plugin"] = {"status": "failed"}
    summary["cases"] = [record]
    md = render_report(summary)
    assert "| 是否含插件链路 | 是 |" in md
    assert "| 是否含全流程沉淀 | 是 |" in md
    assert "各数据集结果汇总：后台抽取" not in md
    assert "各数据集结果汇总：沉淀 knowledge-ingestion" in md
    assert "各数据集结果汇总：插件 kg_extract_v2" in md
    assert "| g | 待补充 | 1 | TXT | 待补充 | 10 | 5 |" in md
    assert "有效评分 0/1" in md


def test_report_escapes_cells_and_preserves_unknown_timing():
    summary = _summary()
    record = _case("name|with`markdown", error="bad | error\nnext", elapsed=None)
    record["files"] = ["a|b.xlsx"]
    summary["cases"] = [record]
    md = render_report(summary)
    assert "name&#124;with&#96;markdown" in md
    assert "bad &#124; error next" in md
    assert "a&#124;b.xlsx" in md
    assert "| **小计（micro）** | 成功 1/1 | 0.750 | 0.600 | 0.667 | — |" in md


def test_render_report_interrupted_and_empty_summary():
    md = render_report({"run_id": "20260930-120000", "interrupted": True, "cases": []})
    assert "本批次被 Ctrl+C 中断" in md
    assert "（无文件）" in md
    assert "## 门禁" not in md


def test_exit_code_for_semantics():
    assert exit_code_for({"failed": 1, "gate": {"passed": False}}) == 2
    assert exit_code_for({"failed": 0, "gate": {"passed": False}}) == 4
    assert exit_code_for({"failed": 0, "gate": {"passed": True}}) == 0
    assert exit_code_for({"failed": 0, "gate": {"passed": None}}) == 0
    assert exit_code_for({}) == 0


def test_golden_inventory_survives_missing_metrics():
    summary = _summary()
    record = _case("failed", status="failed")
    record["backend"] = None
    record["golden_counts"] = {"entities": 9, "relations": 3}
    summary["cases"] = [record]
    md = render_report(summary)
    assert "| g | 待补充 | 1 | TXT | 待补充 | 9 | 3 |" in md
    assert "| **合计** | — | **1** | — | — | 9 | 3 | — |" in md
    assert "| failed | 失败 | — | — | — | 12.0 | 未评分 |" in md


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
        sys, "argv", ["run.py", "--workspace", str(ws), "--match-mode", "identity", "--min-f1", "0.5", "--mode", "chunk-extract"]
    )
    with patch.object(eval_run, "run_backend_case", side_effect=_backend_ok):
        code = eval_run.main()
    assert code == 0
    run_dirs = [item for item in runs_dir.iterdir() if item.is_dir()]
    assert len(run_dirs) == 1
    report = run_dirs[0] / "report.md"
    assert report.is_file()
    md = report.read_text(encoding="utf-8")
    assert md.startswith("# 知识抽取评测报告")
    assert "## 0. 报告信息" in md and "## 3. 数据集总览" in md
    assert "### 4.1 g —— 数据集" in md
    assert "| c1 | 成功 | 1.000 | 1.000 | 1.000 |" in md
    assert "| g | 待补充 | 1 | TXT | 待补充 | 1 | 0 |" in md
    assert "## 门禁" not in md
    # summary.json 与报告同批落盘，且 gate 带配置的预算块
    summary = json.loads((run_dirs[0] / "summary.json").read_text(encoding="utf-8"))
    assert summary["cases"][0]["golden_counts"] == {"entities": 1, "relations": 0}
    assert summary["gate"]["budgets"] == {"case_seconds": None, "stage_seconds": {}}
    assert summary["gate"]["min_f1"]["threshold"] == 0.5
