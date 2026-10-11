"""Read-only matrix denominators and RuleType field coverage; never calls models."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

FIELDS = ("RuleID", "RuleName", "RuleTypeBody", "RuleTemplate", "antecedent", "consequent", "connectionSymbol")
VARIANTS = ("json", "jsonl", "jsonl-adaptive")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def nonempty(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple)):
        return bool(value)
    return True  # Explicit 0 and False are values, not missing fields.


def graph_rows(graph):
    """Preserve row counts; reject unsupported/malformed data, never normalize away rows."""
    if not isinstance(graph, dict):
        raise ValueError("graph_not_object")
    if "graph" in graph and not any(k in graph for k in ("nodes", "entities")):
        return graph_rows(graph["graph"])
    keys = ("entities", "relations") if "entities" in graph else ("nodes", "relationships")
    if keys[0] not in graph:
        raise ValueError("graph_has_no_explicit_entity_array")
    rows = [graph.get(k, []) for k in keys]
    if any(not isinstance(v, list) for v in rows):
        raise ValueError("graph_arrays_invalid")
    if any(not isinstance(v, dict) for values in rows for v in values):
        raise ValueError("graph_row_invalid")
    if any(not isinstance(v.get("properties", {}), dict) for v in rows[0]):
        raise ValueError("entity_properties_invalid")
    return rows


def rule_fields(graph):
    nodes, _ = graph_rows(graph)
    rules = [v for v in nodes if (v.get("label") or v.get("type")) == "RuleType"]
    counts = {field: sum(nonempty(v.get("properties", {}).get(field)) for v in rules) for field in FIELDS}
    counts["both"] = sum(nonempty(v.get("properties", {}).get("antecedent"))
                         and nonempty(v.get("properties", {}).get("consequent")) for v in rules)
    return {"nodes": len(nodes), "rule_nodes": len(rules), "counts": counts,
            "ratios": {k: value / len(rules) if rules else None for k, value in counts.items()}}


def saved_graph(envelope):
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    graph = data.get("graph")
    if graph is None and isinstance(data.get("extraction"), dict):
        graph = data["extraction"].get("graph")
    return graph


def metric_error(metrics, golden_counts, predicted_counts):
    if not isinstance(metrics, dict) or metrics.get("match_mode") != "llm":
        return "missing_or_non_llm_metrics"
    for side, gold, pred in zip(("entities", "relations"), golden_counts, predicted_counts):
        m = metrics.get(side)
        if not isinstance(m, dict):
            return "missing_side_metrics"
        values = [m.get(k) for k in ("golden", "predicted", "tp", "fp", "fn")]
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
            return "invalid_counts"
        g, p, tp, fp, fn = values
        if (g, p) != (gold, pred) or p != tp + fp or g != tp + fn:
            return "graph_metric_denominator_mismatch"
        for key in ("precision", "recall", "f1"):
            value = m.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                return "invalid_score"
        expected_f1 = 2 * tp / (g + p) if g + p else 0
        if abs(m["f1"] - expected_f1) > 1e-5:
            return "score_count_mismatch"
    return None


def audit_case(case, case_dir, golden_path):
    result = {"case": case, "classification": "pending", "valid_score": False}
    if not golden_path.exists():
        return {**result, "classification": "invalid", "reason": "golden_missing"}
    try:
        golden = read(golden_path)
        gold_nodes, gold_relations = graph_rows(golden)
        result["golden_fields"] = rule_fields(golden)
        result["golden_counts"] = [len(gold_nodes), len(gold_relations)]
    except (ValueError, TypeError, KeyError):
        return {**result, "classification": "invalid", "reason": "golden_invalid"}
    if case_dir is None or not (case_dir / "scores.json").exists():
        return result  # Do not count an in-flight graph as a completed zero score.
    try:
        score = read(case_dir / "scores.json")
        block = score.get("backend", {}).get("modules", {}).get("extract", {})
        result.update(extraction_status=block.get("status", "unknown"), case_status=score.get("status"),
                      scorer_claims_scored=score.get("scored", False))
        path = case_dir / "backend_extract.json"
        graph = saved_graph(read(path)) if path.exists() else None
        if graph is None:
            return {**result, "classification": "infrastructure_or_no_result",
                    "reason": "no_saved_graph_no_zero_imputation"}
        pred_nodes, pred_relations = graph_rows(graph)
        result["predicted_fields"] = rule_fields(graph)
        result["predicted_counts"] = [len(pred_nodes), len(pred_relations)]
        result["prediction_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        error = metric_error(block.get("metrics"), result["golden_counts"], result["predicted_counts"])
        if error:
            return {**result, "classification": "unscored_result", "reason": error}
        result["valid_score"] = True
        result["metrics"] = {side: block["metrics"][side] for side in ("entities", "relations")}
        if block.get("status") in {"ok", "success", "succeeded", "completed"} and score.get("status") in {"ok", "success", "succeeded", "completed"}:
            result["classification"] = "success"
        elif not pred_nodes and not pred_relations:
            result["classification"] = "observed_empty"
        else:
            result["classification"] = "partial_or_failed_result"
        return result
    except (ValueError, TypeError, KeyError):
        return {**result, "classification": "invalid", "reason": "malformed_saved_result"}


def aggregate(cases):
    valid = [c for c in cases if c["valid_score"]]
    result = {"expected": len(cases), "valid_scored": len(valid),
              "classification_counts": {name: sum(c["classification"] == name for c in cases) for name in
                  ("success", "partial_or_failed_result", "observed_empty", "infrastructure_or_no_result", "unscored_result", "pending", "interrupted", "invalid")},
              "micro": {}, "macro": {}}
    for side in ("entities", "relations"):
        counts = {k: sum(c["metrics"][side][k] for c in valid) for k in ("golden", "predicted", "tp", "fp", "fn")}
        denom = counts["golden"] + counts["predicted"]
        result["micro"][side] = {**counts, "f1": 2 * counts["tp"] / denom if denom else None}
        informative = [c["metrics"][side] for c in valid if c["metrics"][side]["golden"] or c["metrics"][side]["predicted"]]
        result["macro"][side] = {"cases": len(informative), "both_empty_excluded": len(valid) - len(informative),
                                  "f1": sum(c["f1"] for c in informative) / len(informative) if informative else None}
    result["score_scope"] = "conditional_on_valid_saved_results; failures/pending never imputed as zero"
    failures = [c for c in cases if c["classification"] == "infrastructure_or_no_result"]
    unresolved = [c for c in cases if not c["valid_score"] and c not in failures]
    result["failure_as_empty_assumption"] = {
        "scope": "counterfactual completed no-result failures contribute golden only; pending/interrupted excluded",
        "observed_valid_cases": len(valid), "completed_no_result_failures": len(failures),
        "covered_cases": len(valid) + len(failures), "unresolved_cases": len(unresolved),
        "all_expected_covered": not unresolved, "micro": {}}
    for i, side in enumerate(("entities", "relations")):
        counts = {k: result["micro"][side][k] for k in ("golden", "predicted", "tp", "fp", "fn")}
        missing_gold = sum(c["golden_counts"][i] for c in failures)
        counts["golden"] += missing_gold
        counts["fn"] += missing_gold
        denom = counts["golden"] + counts["predicted"]
        result["failure_as_empty_assumption"]["micro"][side] = {**counts, "f1": 2 * counts["tp"] / denom if denom else None}
    result["planned_golden_counts"] = {side: sum(c.get("golden_counts", [0, 0])[i] for c in cases)
                                      for i, side in enumerate(("entities", "relations"))}
    return result


def audit(root, variants=VARIANTS, *, combine_recovery=False, equivalence_note="", interrupted_groups=(), plan_path=None, inputs_root=None, evaluation_root=None):
    plan_path = plan_path or root / "plan.json"
    inputs_root = inputs_root or root / "inputs"
    evaluation_root = evaluation_root or root / "evaluation"
    plan = read(plan_path)
    expected = plan["cases"]
    if any(len(Path(c).parts) != 2 or Path(c).is_absolute() or ".." in Path(c).parts for c in expected):
        raise ValueError("unsafe_case_paths")
    if combine_recovery and not equivalence_note.strip():
        raise ValueError("recovery_combination_requires_explicit_equivalence_note")
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "source_text_reviewed": False,
              "models_called": False, "root": str(root), "inputs_root": str(inputs_root), "plan_path": str(plan_path),
              "evaluation_root": str(evaluation_root), "frozen_source_commit": plan.get("backend_source_commit"), "groups": {}}
    for variant in variants:
        if variant not in VARIANTS:
            raise ValueError("unknown_variant")
        for model in plan["extractors"]:
            model_id = model["id"]
            if Path(model_id).name != model_id or model_id in (".", ".."):
                raise ValueError("unsafe_model_id")
            runs = sorted((evaluation_root / variant / model_id / plan["judge"]["id"] / "runs").glob("*"))
            runs = [p for p in runs if p.is_dir()]
            selected = runs[-1] if runs else None
            cases = [audit_case(case, selected / case if selected else None,
                                inputs_root / variant / case / "extract/golden.json") for case in expected]
            if variant + "/" + model_id in interrupted_groups:
                cases = [{**c, "classification": "interrupted"} if c["classification"] == "pending" else c for c in cases]
            group = {"selected_run": selected.name if selected else None, "other_runs_not_merged": [p.name for p in runs[:-1]],
                     "summary_exists": bool(selected and (selected / "summary.json").exists()),
                     "run_complete": False, "cases": cases, **aggregate(cases)}
            if selected and (selected / "summary.json").exists():
                summary = read(selected / "summary.json")
                reported = summary.get("overall", {}).get("backend_extract", {})
                group["reported_summary_case_count"] = len(summary.get("cases", []))
                group["run_complete"] = (group["reported_summary_case_count"] == len(expected)
                                         and not any(c["classification"] in {"pending", "interrupted"} for c in cases))
                group["reported_quality"] = summary.get("quality", {}).get("modules", {}).get("backend_extract")
                group["summary_micro_differences"] = {side: {k: {"reported": reported.get(side, {}).get(k), "audited": group["micro"][side][k]}
                    for k in ("golden", "predicted", "tp", "fp", "fn") if reported.get(side, {}).get(k) != group["micro"][side][k]}
                    for side in ("entities", "relations")}
            result["groups"][variant + "/" + model_id] = group
    result["recovery_attempts"] = {}
    recovery_roots = sorted(p for p in root.glob("restart-recovery-*") if p.is_dir())
    for recovery in recovery_roots:
        provenance_path = recovery / "provenance.json"
        provenance = read(provenance_path) if provenance_path.exists() else {}
        safe_provenance = {k: provenance[k] for k in ("backend_source_commit", "frozen_source_commit", "restart_at", "second_restart_at",
                            "extractor_code_unchanged", "extraction_algorithm_unchanged", "transport_trace_capture_added", "original_attempts_preserved") if k in provenance}
        for variant in variants:
            for model in plan["extractors"]:
                model_id = model["id"]
                runs = sorted(p for p in (recovery / variant / model_id / plan["judge"]["id"] / "runs").glob("*") if p.is_dir())
                if not runs:
                    continue
                for selected in runs:
                    # Preserve each attempt; never silently overwrite an earlier failure.
                    cases = [audit_case(case, selected / case, inputs_root / variant / case / "extract/golden.json") for case in expected]
                    key = recovery.name + "/" + variant + "/" + model_id + "/" + selected.name
                    result["recovery_attempts"][key] = {"cohort": recovery.name, "variant": variant,
                        "model": model_id, "run": selected.name, "run_complete": (selected / "summary.json").exists(),
                        "provenance": safe_provenance, "scope": "recovery subset; absent cases are not inferred failures", "cases": cases, **aggregate(cases)}
    if combine_recovery:
        result["combination_equivalence_note"] = equivalence_note
        result["combined_groups"] = {}
        for key, primary in result["groups"].items():
            variant, model_id = key.split("/")
            combined = []
            for index, case in enumerate(expected):
                options = [("primary/" + str(primary["selected_run"]), primary["cases"][index])]
                options += [(name, value["cases"][index]) for name, value in result["recovery_attempts"].items()
                            if value["variant"] == variant and value["model"] == model_id]
                valid = [(name, value) for name, value in options if value["valid_score"]]
                selected_name, selected_case = valid[-1] if valid else options[-1]
                combined.append({**selected_case, "selected_attempt": selected_name,
                    "attempt_statuses": [{"attempt": name, "classification": value["classification"], "valid_score": value["valid_score"]}
                                         for name, value in options]})
            result["combined_groups"][key] = {"cases": combined, **aggregate(combined),
                "scope": "explicit latest valid completed case recovery, original attempts preserved"}
    return result


def markdown(result):
    """Safe tables contain counts/status only, no graph text or credentials."""
    def pct(value):
        return "—" if value is None else f"{value * 100:.2f}%"
    lines = ["# 规则字段与评测分母离线快照", "", f"快照 UTC：{result['timestamp_utc']}", "",
             "仅保存图与 golden；不看原文、不调用模型。字段非空不代表语义正确。", "",
             "中断说明：" + (result.get("censor_note") or "未提供人工中断说明；pending 不作零分。"), "",
             "## 原始批次条件指标", "", "|方案/模型|有效评分/计划|成功|部分/失败有图|无结果失败|中断|待完成|实体F1|关系F1|实体gold分母/计划|关系gold分母/计划|",
             "|---|---|---:|---:|---:|---:|---:|---|---|---|---|"]
    for key, g in result["groups"].items():
        c = g["classification_counts"]
        lines.append(f"|{key}|{g['valid_scored']}/{g['expected']}|{c['success']}|{c['partial_or_failed_result']}|{c['infrastructure_or_no_result']}|{c['interrupted']}|{c['pending']}|"
                     f"{pct(g['micro']['entities']['f1'])}|{pct(g['micro']['relations']['f1'])}|{g['micro']['entities']['golden']}/{g['planned_golden_counts']['entities']}|"
                     f"{g['micro']['relations']['golden']}/{g['planned_golden_counts']['relations']}|")
    lines += ["", "## 完成无结果失败视为空抽取：假设口径", "", "这是业务假设，不能冒充实际抽取的零分；只给已完成无结果失败加 golden/FN，不给未启动或中断补零。",
              "", "|方案/模型|覆盖/计划|尚未解决|是否全计划覆盖|实体F1|关系F1|", "|---|---|---:|---|---|---|"]
    for key, g in result["groups"].items():
        x = g["failure_as_empty_assumption"]
        lines.append(f"|{key}|{x['covered_cases']}/{g['expected']}|{x['unresolved_cases']}|{x['all_expected_covered']}|{pct(x['micro']['entities']['f1'])}|{pct(x['micro']['relations']['f1'])}|")
    lines += ["", "## 原始规则结构字段", "", "每一行仅统计 RuleType；非规则组不混入字段覆盖率分母。", "",
              "|方案/模型/案例|状态|Rule节点|ID|Name|Body|Template|前提|结果|连接|前提及结果|", "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for key, g in result["groups"].items():
        for c in g["cases"]:
            f = c.get("predicted_fields")
            if f and f["rule_nodes"]:
                lines.append(f"|{key}/{c['case']}|{c['classification']}|{f['rule_nodes']}|" + "|".join(str(f['counts'][k]) for k in (*FIELDS, "both")) + "|")
    lines += ["", "## 原 summary 与离线计数核对", "", "条件指标可以正确排除没有有效结果的失败，但报告必须同时展示覆盖与计划分母。"]
    for key, g in result["groups"].items():
        if "summary_micro_differences" in g:
            changed = any(g["summary_micro_differences"].values())
            lines.append(f"- {key}：summary案例 {g['reported_summary_case_count']}，计划 {g['expected']}；计数差异 {'有，见JSON明细' if changed else '无'}；整组完成={g['run_complete']}。")
        else:
            lines.append(f"- {key}：无最终 summary，censored，不能声称整组完成。")
    lines += ["", "## 恢复批次（独立attempt）", "", "目录名不代表实际HEAD；各恢复批次实际源码与trace变化见JSON provenance。未出现的恢复子集案例不是失败。"]
    for key, g in result["recovery_attempts"].items():
        lines.append(f"- {key}：有效保存评分 {g['valid_scored']}，final summary={g['run_complete']}，实际源 {g['provenance'].get('backend_source_commit', 'unknown')}。")
    if "combined_groups" in result:
        lines += ["", "## 明确授权的恢复组合视图", "", result["combination_equivalence_note"], "",
                  "各case选最新有效保存评分，原失败/中断仍保留；不称为单一源码单次运行。", "",
                  "|方案/模型|有效/计划|实体F1|关系F1|", "|---|---|---|---|"]
        for key, g in result["combined_groups"].items():
            lines.append(f"|{key}|{g['valid_scored']}/{g['expected']}|{pct(g['micro']['entities']['f1'])}|{pct(g['micro']['relations']['f1'])}|")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--censor-note", default="")
    parser.add_argument("--combine-recovery", action="store_true")
    parser.add_argument("--recovery-equivalence-note", default="")
    parser.add_argument("--interrupted-groups", default="", help="Comma-separated primary variant/model groups interrupted externally")
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--plan-path", type=Path)
    parser.add_argument("--inputs-root", type=Path)
    parser.add_argument("--evaluation-root", type=Path)
    parser.add_argument("--actual-source-commit", default="", help="Caller-recorded actual source; never infer it from a directory label")
    args = parser.parse_args()
    result = audit(args.root, combine_recovery=args.combine_recovery, equivalence_note=args.recovery_equivalence_note,
                   interrupted_groups=tuple(x for x in args.interrupted_groups.split(",") if x), plan_path=args.plan_path,
                   inputs_root=args.inputs_root, evaluation_root=args.evaluation_root)
    result["censor_note"] = args.censor_note
    result["caller_recorded_actual_source_commit"] = args.actual_source_commit or None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown(result), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "groups": {k: {q: v[q] for q in ("expected", "valid_scored", "run_complete", "classification_counts")}
                         for k, v in result["groups"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
