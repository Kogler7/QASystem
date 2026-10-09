"""Pure quality gates and conditional score summaries for evaluation records.

No score is imputed for an unscored or failed request. Coverage accompanies every
aggregate; a failed case with valid metrics (for example an empty extraction)
still contributes its observed score. Coverage's ``failed`` and ``unscored``
counts can overlap other categories and are not a partition of ``expected``.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


MODULES = ("backend_extract", "backend_system", "plugin_extract")
SIDES = ("entities", "relations")
SCORES = ("precision", "recall", "f1")
COUNTS = ("golden", "predicted", "tp", "fp", "fn")


def expected_modules(summary: Mapping[str, Any]) -> tuple[str, ...]:
    """Resolve requested scoring modules, including ones that never ran."""
    stage = summary.get("stage") or "backend"
    modes = summary.get("modes") or ["chunk-extract"]
    if isinstance(modes, str):
        modes = [modes]
    expected = []
    if stage in {"backend", "all"}:
        if any(mode in {"extract", "chunk-extract"} for mode in modes):
            expected.append("backend_extract")
        if "pipeline" in modes:
            expected.append("backend_system")
    if stage in {"plugin", "all"}:
        expected.append("plugin_extract")
    return tuple(expected)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _blocks(record: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    backend = _mapping(record.get("backend"))
    modules = _mapping(backend.get("modules"))
    return {
        "backend_extract": _mapping(modules.get("extract")),
        "backend_system": _mapping(backend.get("system")),
        "plugin_extract": _mapping(record.get("plugin")),
    }


def _case_label(record: Mapping[str, Any]) -> str:
    return f"{record.get('group') or '?'}/{record.get('case') or '?'}"


def _number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


def _invalid_fields(metrics: Mapping[str, Any]) -> list[str]:
    invalid = []
    for side in SIDES:
        values = _mapping(metrics.get(side))
        for key in (*SCORES, "accuracy", "jaccard"):
            if key in {"accuracy", "jaccard"} and key not in values:
                continue
            value = values.get(key)
            if not _number(value) or not 0 <= value <= 1:
                invalid.append(f"{side}.{key}")
        for key in COUNTS:
            value = values.get(key)
            if not _number(value) or value < 0 or int(value) != value:
                invalid.append(f"{side}.{key}")
    return invalid


def _mode_matches(metrics: Mapping[str, Any], requested: str) -> bool:
    actual = metrics.get("match_mode")
    # Historical identity records did not include the scoring mode. An LLM
    # result must identify itself explicitly; unknown provenance cannot pass.
    if requested == "identity":
        return actual in (None, "identity")
    return actual == requested


def build_quality_gate(
    summary: Mapping[str, Any],
    *,
    min_relation_f1: float = 0.0,
    require_scored: bool = False,
) -> dict[str, Any]:
    """Validate scorer provenance, score integrity, coverage and relation F1.

    This gate supplements, rather than replaces, execution and latency gates.
    ``require_scored`` requires annotation and metrics for *each* selected
    module. Invalid metrics and non-LLM scores in an LLM run always fail.
    """
    if not _number(min_relation_f1) or not 0 <= min_relation_f1 <= 1:
        raise ValueError("min_relation_f1 must be finite and between 0 and 1")
    cases = list(summary.get("cases") or [])
    expected = expected_modules(summary)
    match_mode = str(summary.get("match_mode") or "identity")
    issues: list[dict[str, Any]] = []
    below: list[dict[str, Any]] = []
    if require_scored and not cases:
        issues.append({"case": None, "module": None, "reason": "no_cases"})
    if require_scored and not expected:
        issues.append({"case": None, "module": None, "reason": "no_expected_modules"})
    if require_scored:
        for reused in _mapping(summary.get("manifest")).get("reused_predictions") or []:
            if reused.get("input_verified") is not True:
                issues.append({"case": reused.get("id"), "module": "backend_extract",
                               "reason": "reused_prediction_input_unverified"})
    for record in cases:
        label = _case_label(record)
        if require_scored and not record.get("scored"):
            issues.append({"case": label, "module": None, "reason": "unscored_case"})
        for module, block in _blocks(record).items():
            metrics = _mapping(block.get("metrics"))
            if not metrics:
                if require_scored and module in expected:
                    issues.append({"case": label, "module": module, "reason": "missing_metrics"})
                continue
            invalid = _invalid_fields(metrics)
            if invalid:
                issues.append({"case": label, "module": module, "reason": "invalid_metrics", "fields": invalid})
            if match_mode == "llm" and metrics.get("match_mode") != "llm":
                issues.append({
                    "case": label,
                    "module": module,
                    "reason": "scorer_mismatch",
                    "expected_match_mode": "llm",
                    "actual_match_mode": metrics.get("match_mode"),
                })
            if invalid or not _mode_matches(metrics, match_mode):
                continue
            relations = metrics["relations"]
            # A genuinely relation-free case says nothing about relation
            # recall. Hallucinated relations do count, including a zero gold.
            if (
                min_relation_f1 > 0
                and (relations["golden"] > 0 or relations["predicted"] > 0)
                and relations["f1"] < min_relation_f1
            ):
                below.append({"case": label, "module": module, "f1": float(relations["f1"])})
    return {
        "issues": issues,
        "min_relation_f1": {"threshold": float(min_relation_f1), "below": below},
        "passed": not issues and not below,
    }


def _aggregate(
    cases: list[Mapping[str, Any]], module: str, match_mode: str,
) -> dict[str, Any]:
    coverage = {
        "expected": len(cases), "scored": 0, "failed": 0, "missing": 0,
        "fallback": 0, "invalid": 0, "mode_mismatch": 0, "unscored": 0,
    }
    valid: list[Mapping[str, Any]] = []
    for record in cases:
        block = _blocks(record)[module]
        metrics = _mapping(block.get("metrics"))
        if record.get("status") in {"failed", "partial"} or block.get("status") in {"failed", "error", "partial"}:
            coverage["failed"] += 1
        if not record.get("scored"):
            coverage["unscored"] += 1
        if not metrics:
            coverage["missing"] += 1
        elif metrics.get("match_mode") == "identity_fallback":
            coverage["fallback"] += 1
        elif not _mode_matches(metrics, match_mode):
            coverage["mode_mismatch"] += 1
        elif _invalid_fields(metrics):
            coverage["invalid"] += 1
        else:
            coverage["scored"] += 1
            valid.append(metrics)
    coverage["scored_ratio"] = round(coverage["scored"] / len(cases), 6) if cases else None
    micro: dict[str, Any] = {}
    macro: dict[str, Any] = {}
    for side in SIDES:
        values = [metrics[side] for metrics in valid]
        counts = {key: sum(int(value[key]) for value in values) for key in COUNTS}
        tp, fp, fn = (counts[key] for key in ("tp", "fp", "fn"))
        informative = [value for value in values if value["golden"] > 0 or value["predicted"] > 0]
        has_items = bool(informative)
        micro[side] = {
            **counts,
            "precision": round(tp / (tp + fp), 6) if tp + fp else (0.0 if has_items else None),
            "recall": round(tp / (tp + fn), 6) if tp + fn else (0.0 if has_items else None),
            "f1": round(2 * tp / (2 * tp + fp + fn), 6) if 2 * tp + fp + fn else (0.0 if has_items else None),
            "jaccard": round(tp / (tp + fp + fn), 6) if tp + fp + fn else None,
            # Historical reports used "accuracy" for this intersection-over-
            # union ratio. Keep that alias, without implying true negatives.
            "accuracy": round(tp / (tp + fp + fn), 6) if tp + fp + fn else None,
        }
        macro[side] = {
            "cases": len(informative),
            "empty_cases": len(values) - len(informative),
            **{
                key: round(sum(float(value[key]) for value in informative) / len(informative), 6)
                if informative else None
                for key in SCORES
            },
        }
    return {"coverage": coverage, "micro": micro, "case_macro": macro}


def build_quality_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Return module/group micro and case macro scores with explicit coverage.

    Scores are conditional on valid metrics from the requested scorer, not
    estimates of end-to-end performance for missing cases. Both-empty sides
    have no defined quality score and are excluded from case macro averages.
    """
    cases = list(summary.get("cases") or [])
    match_mode = str(summary.get("match_mode") or "identity")
    expected = expected_modules(summary)
    modules: dict[str, Any] = {}
    for module in MODULES:
        # Preserve supplementary metrics in old summaries even when their
        # original stage/mode metadata is incomplete.
        selected = [record for record in cases if module in expected or _blocks(record)[module].get("metrics")]
        if module not in expected and not selected:
            continue
        result = _aggregate(selected, module, match_mode)
        groups = sorted({str(record.get("group") or "?") for record in selected})
        result["by_group"] = {
            group: _aggregate(
                [record for record in selected if str(record.get("group") or "?") == group],
                module,
                match_mode,
            )
            for group in groups
        }
        result["group_macro"] = {}
        for side in SIDES:
            group_scores = [
                group["case_macro"][side]
                for group in result["by_group"].values()
                if group["case_macro"][side]["cases"] > 0
            ]
            result["group_macro"][side] = {
                "groups": len(group_scores),
                **{
                    key: round(sum(group[key] for group in group_scores) / len(group_scores), 6)
                    if group_scores else None
                    for key in SCORES
                },
            }
        modules[module] = result
    return {"score_scope": "conditional_on_valid_scores", "match_mode": match_mode, "modules": modules}
