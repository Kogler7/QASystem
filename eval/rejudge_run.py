#!/usr/bin/env python3
"""仅对已有 backend_extract.json 重跑 LLM-judge / identity（B 方案窗口扫图）。

用法:
  python -m eval.rejudge_run \\
    --workspace test_data/eval-v2 \\
    --from-run tmp/eval/runs/eval-v2-extracts-20261003 \\
    --match-mode llm
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from eval.run import (
        RUNS_DIR,
        add_metrics,
        build_judge_client,
        emit_metrics,
        empty_metrics,
        extract_graph_from_envelope,
        finalize_metrics,
        iter_metrics_lines,
        add_runtime_arguments,
        params_from_args,
        log,
        maybe_score,
        normalize_eval_dir,
        resolve_reuse_dir,
        split_match_details,
        write_json,
    )
except ImportError:
    from run import (  # type: ignore
        RUNS_DIR,
        add_metrics,
        build_judge_client,
        emit_metrics,
        empty_metrics,
        extract_graph_from_envelope,
        finalize_metrics,
        iter_metrics_lines,
        add_runtime_arguments,
        params_from_args,
        log,
        maybe_score,
        normalize_eval_dir,
        resolve_reuse_dir,
        split_match_details,
        write_json,
    )


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def discover_cases(
    from_run: Path,
    *,
    include: set[str] | None = None,
    cases_per_type: int | None = None,
) -> list[tuple[str, str, Path]]:
    rows: list[tuple[str, str, Path]] = []
    for extract in sorted(from_run.glob("*/*/backend_extract.json")):
        case_dir = extract.parent
        group, case = case_dir.parent.name, case_dir.name
        key = f"{group}/{case}"
        if include is not None and key not in include and case not in include:
            continue
        rows.append((group, case, case_dir))
    if cases_per_type is not None and cases_per_type > 0:
        by_group: dict[str, list[tuple[str, str, Path]]] = {}
        for row in rows:
            by_group.setdefault(row[0], []).append(row)
        limited: list[tuple[str, str, Path]] = []
        for group in sorted(by_group):
            limited.extend(by_group[group][:cases_per_type])
        return limited
    return rows


def resolve_golden(workspace: Path, group: str, case: str) -> Path | None:
    for path in (
        workspace / group / case / "extract" / "golden.json",
        workspace / "tmp_problem" / case / "extract" / "golden.json",
        workspace / "rule" / case / "extract" / "golden.json",
    ):
        if path.is_file():
            return path
    return None


def _fmt_item(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    if "name" in item and "type" in item:
        return f"{item.get('type')}::{item.get('name')}"
    if item.get("rel_type") is not None:
        return (
            f"{item.get('from_type')}::{item.get('from_name')} "
            f"-[{item.get('rel_type')}]-> "
            f"{item.get('to_type')}::{item.get('to_name')}"
        )
    return json.dumps(item, ensure_ascii=False)


def _section_list(title: str, rows: list[Any], *, limit: int = 80) -> list[str]:
    lines = [f"### {title}（{len(rows)}）"]
    if not rows:
        lines.append("- （无）")
        return lines
    for index, row in enumerate(rows[:limit], start=1):
        if isinstance(row, dict) and "golden" in row and "predicted" in row:
            lines.append(
                f"{index}. 对上：`{_fmt_item(row.get('golden'))}` ↔ `{_fmt_item(row.get('predicted'))}`"
            )
        else:
            lines.append(f"{index}. `{_fmt_item(row)}`")
    if len(rows) > limit:
        lines.append(f"- … 另有 {len(rows) - limit} 条，见 match_details.json")
    return lines


def write_case_judge_view(
    out_dir: Path,
    *,
    group: str,
    case: str,
    metrics: dict[str, Any],
    details: dict[str, Any] | None,
) -> None:
    lines = [
        f"# {group}/{case} judge 结果（B：窗口扫图 + 最大匹配）",
        "",
        f"- match_mode: `{metrics.get('match_mode')}`",
        "",
        "## 指标",
        "",
        "```",
        *iter_metrics_lines(metrics),
        "```",
        "",
    ]
    details = details or {}
    for kind, label in (("entities", "实体"), ("relations", "关系")):
        block = details.get(kind) if isinstance(details.get(kind), dict) else {}
        lines.append(f"## {label}")
        lines.append("")
        lines.extend(_section_list("对上 matched", list(block.get("matched") or [])))
        lines.append("")
        lines.extend(_section_list("漏抽 missing", list(block.get("missing") or [])))
        lines.append("")
        lines.extend(_section_list("多抽 extra", list(block.get("extra") or [])))
        lines.append("")
    (out_dir / "judge_view.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_run_report(out_root: Path, case_rows: list[dict[str, Any]], summary: dict[str, Any]) -> Path:
    lines = [
        "# Rejudge 汇总（B 方案）",
        "",
        f"- scored: {summary.get('scored_cases')}/{summary.get('total_cases')}",
        f"- from_run: `{summary.get('from_run')}`",
        f"- workspace: `{summary.get('workspace')}`",
        f"- judge: window sweep + maximum matching",
        "",
        "## 总体指标",
        "",
        "```",
        *iter_metrics_lines(summary),
        "```",
        "",
        "| case | mode | 实体F1 | 关系F1 | 实体对上/多抽/漏抽 | 关系对上/多抽/漏抽 |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ]
    for row in case_rows:
        ent, rel = row.get("entities") or {}, row.get("relations") or {}
        lines.append(
            f"| {row['group']}/{row['case']} | {row.get('match_mode')} | "
            f"{ent.get('f1')} | {rel.get('f1')} | "
            f"{ent.get('tp')}/{ent.get('fp')}/{ent.get('fn')} | "
            f"{rel.get('tp')}/{rel.get('fp')}/{rel.get('fn')} |"
        )
    lines.append("")
    lines.append("每 case 目录有 `judge_view.md`（对上/多抽/漏抽）与 `match_details.json`。")
    path = out_root / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="复用 backend_extract 只重跑 B 方案评分")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--from-run", required=True)
    add_runtime_arguments(parser)
    parser.add_argument("--judge-model")
    parser.add_argument("--judge-base-url")
    parser.add_argument("--judge-adapter", choices=("auto", "chat", "systemone"))
    parser.add_argument("--judge-key-env")
    parser.add_argument("--match-mode", choices=("llm", "identity"), default="llm")
    parser.add_argument("--out-run", default=None)
    parser.add_argument(
        "--include",
        default=None,
        help="逗号分隔 group/case 或 case 名；与 --cases-per-type 可并用（先 include 再按类截断）",
    )
    parser.add_argument(
        "--cases-per-type",
        type=int,
        default=None,
        help="每类最多取 N 个 case（按目录名排序取前 N）",
    )
    args = parser.parse_args()

    workspace = normalize_eval_dir(Path(args.workspace).expanduser().resolve())
    from_run = resolve_reuse_dir(workspace, args.from_run)
    params = params_from_args(args)
    eval_cfg = params.get("eval") or {}

    score_ctx: dict[str, Any] = {
        "match_mode": args.match_mode,
        "judge_client": None,
        "batch_size": int(eval_cfg.get("judge_batch_size") or 20),
        "golden_batch_size": int(eval_cfg.get("judge_golden_batch_size") or 40),
        "max_concurrency": int(eval_cfg.get("judge_max_concurrency") or 1),
    }
    if args.match_mode == "llm":
        score_ctx["judge_client"] = build_judge_client(params)
        log(
            f"rejudge B-judge model={score_ctx['judge_client'].model} "
            f"base={score_ctx['judge_client'].base_url} "
            f"batch={score_ctx['batch_size']}/{score_ctx['golden_batch_size']} "
            f"concurrency={score_ctx['max_concurrency']}"
        )

    out_name = args.out_run or datetime.now().strftime("%Y%m%d-%H%M%S")
    out_root = (RUNS_DIR / out_name).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    include = {x.strip() for x in str(args.include).split(",") if x.strip()} if args.include else None
    write_json(
        out_root / "params.resolved.json",
        {
            "workspace": str(workspace),
            "from_run": str(from_run),
            "match_mode": args.match_mode,
            "judge_scheme": "B-window-maximum-matching",
            "include": sorted(include) if include else None,
            "cases_per_type": args.cases_per_type,
            "mode": "rejudge",
            "started_at": datetime.now().isoformat(timespec="seconds"),
        },
    )

    cases = discover_cases(from_run, include=include, cases_per_type=args.cases_per_type)
    if not cases:
        log(f"未在 {from_run} 发现可用 case（include={include} cases_per_type={args.cases_per_type}）")
        return 1
    log("rejudge cases: " + ", ".join(f"{g}/{c}" for g, c, _ in cases))

    totals = empty_metrics()
    scored = 0
    case_rows: list[dict[str, Any]] = []
    for group, case, src_dir in cases:
        golden_path = resolve_golden(workspace, group, case)
        out_dir = out_root / group / case
        out_dir.mkdir(parents=True, exist_ok=True)
        if golden_path is None:
            log(f"跳过 {group}/{case}：缺少 golden")
            continue
        envelope = _load_json(src_dir / "backend_extract.json")
        write_json(out_dir / "backend_extract.json", envelope)
        golden = _load_json(golden_path)
        graph = extract_graph_from_envelope(envelope)
        raw = maybe_score(golden, graph, score_ctx)
        details = raw.get("details") if isinstance(raw, dict) else None
        metrics = split_match_details(out_dir, "backend_extract", raw)
        if not metrics:
            log(f"{group}/{case} 无评分结果")
            continue
        write_json(out_dir / "scores.json", {"backend_extract": metrics})
        if details is None:
            md_path = out_dir / "match_details.json"
            if md_path.is_file():
                try:
                    details = _load_json(md_path).get("backend_extract")
                except json.JSONDecodeError:
                    details = None
        write_case_judge_view(out_dir, group=group, case=case, metrics=metrics, details=details)
        add_metrics(totals, metrics)
        scored += 1
        ent, rel = metrics.get("entities") or {}, metrics.get("relations") or {}
        case_rows.append(
            {
                "group": group,
                "case": case,
                "match_mode": metrics.get("match_mode"),
                "entities": ent,
                "relations": rel,
            }
        )
        log(
            f"{group}/{case} mode={metrics.get('match_mode')} "
            f"ent_f1={ent.get('f1')} rel_f1={rel.get('f1')} "
            f"实体对上/多抽/漏抽={ent.get('tp')}/{ent.get('fp')}/{ent.get('fn')} "
            f"关系对上/多抽/漏抽={rel.get('tp')}/{rel.get('fp')}/{rel.get('fn')}"
        )
        emit_metrics(f"{group}/{case} 明细", metrics)

    summary = finalize_metrics(totals)
    summary.update(
        {
            "scored_cases": scored,
            "total_cases": len(cases),
            "workspace": str(workspace),
            "from_run": str(from_run),
            "judge_scheme": "B-window-maximum-matching",
            "cases": [
                {
                    "group": row["group"],
                    "case": row["case"],
                    "match_mode": row["match_mode"],
                    "entities": {k: row["entities"].get(k) for k in ("f1", "tp", "fp", "fn")},
                    "relations": {k: row["relations"].get(k) for k in ("f1", "tp", "fp", "fn")},
                }
                for row in case_rows
            ],
        }
    )
    write_json(out_root / "summary.json", summary)
    report = write_run_report(out_root, case_rows, summary)
    emit_metrics("rejudge summary", summary)
    log(f"rejudge 完成 → {out_root} scored={scored}/{len(cases)} report={report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
