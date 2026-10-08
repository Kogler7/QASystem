"""Content manifests and conservative, paired evaluation baseline comparisons.

This module reads only explicitly supplied files. It never discovers credentials,
loads application code, calls a service, or changes its input dictionaries. The
caller must pass a safe, resolved settings snapshot rather than a whole config.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
_SUCCESS = {"succeeded", "reused"}


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file(path: Path | None, root: Path) -> dict[str, Any]:
    if path is None:
        return {"path": None, "sha256": None, "missing": True}
    path = Path(path).resolve()
    record: dict[str, Any] = {"path": Path(os.path.relpath(path, root)).as_posix()}
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        record.update(sha256=digest.hexdigest(), missing=False)
    except OSError:
        # A smoke run may legitimately lack annotations. Keep that evidence in
        # its manifest; a strict baseline comparison will reject it later.
        record.update(sha256=None, missing=True)
    return record


def _ontology(path: Path | None, root: Path) -> dict[str, Any]:
    """Follow run.read_ontology's top-level-first .puml/.uml selection."""
    if path is None or not Path(path).is_dir():
        return _file(path, root)
    directory = Path(path).resolve()
    suffixes = {".puml", ".uml"}
    try:
        paths = sorted(item for item in directory.iterdir() if item.is_file() and item.suffix.lower() in suffixes)
        if not paths:
            paths = sorted(item for item in directory.rglob("*") if item.is_file() and item.suffix.lower() in suffixes)
        files = [_file(item, directory) for item in paths]
    except OSError:
        files = []
    missing = not files or any(item["missing"] for item in files)
    return {"path": Path(os.path.relpath(directory, root)).as_posix(), "kind": "directory", "files": files,
            "sha256": None if missing else _digest(files), "missing": missing}


def _scoring(settings: Mapping[str, Any]) -> dict[str, Any]:
    modes = settings.get("modes") or ["chunk-extract"]
    if isinstance(modes, str):
        modes = [modes]
    return {
        "stage": settings.get("stage") or "backend",
        "modes": sorted(set(modes)),
        "match_mode": settings.get("match_mode") or "identity",
        "judge": settings.get("judge") or {},
    }


def _source_manifest(root: Path | None) -> dict[str, Any]:
    if root is None:
        return {}
    try:
        payload = json.loads((Path(root) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        return {}
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("dataset_sha256") != _digest(payload["cases"]):
        return {}
    return payload


def build_manifest(
    *,
    workspace: Path,
    cases: list[dict[str, Any]],
    reuse_dir: Path | None,
    settings: dict[str, Any],
    evaluator_paths: list[Path],
    reuse_extract_dir: Path | None = None,
) -> dict[str, Any]:
    """Hash annotations, source bytes, actual reused inputs and evaluator code.

    ``cases`` contains group/case, sources, golden and ontology paths. Paths in
    the resulting manifest are relative, so moving a complete workspace does
    not change its fingerprint. ``reuse_dir`` and ``reuse_extract_dir`` are run
    roots with a ``group/case`` layout. Missing optional reuse Markdown falls
    back to source input, matching the runner; extract-only chunks are required.

    Prediction reuse is recorded separately from the dataset hash: predictions
    are the measured outputs and may differ across candidate algorithms.
    Extractor model/options are recorded in settings but are intentionally not
    part of the scoring contract. Code hashes include uncommitted file changes.
    """
    root = Path(workspace).resolve()
    snapshot = json.loads(json.dumps(settings, ensure_ascii=False, allow_nan=False))
    contract = _scoring(snapshot)
    modes = contract["modes"]
    backend = contract["stage"] in {"backend", "all"}
    entries: list[dict[str, Any]] = []
    predictions: list[dict[str, Any]] = []
    prediction_manifest = _source_manifest(reuse_extract_dir)
    for case in cases:
        group, name = str(case["group"]), str(case["case"])
        label = f"{group}/{name}"
        reused: list[dict[str, Any]] = []
        inputs: dict[str, str] = {}
        source_dir = Path(reuse_dir) / group / name if reuse_dir is not None else None
        if backend and "pipeline" in modes:
            inputs["backend_system"] = "source_documents"
        if backend and "extract" in modes:
            inputs["backend_extract"] = "reused_chunks"
            chunks = source_dir / "chunks.json" if source_dir is not None else None
            reused.append({"kind": "chunks", **_file(chunks, Path(reuse_dir).resolve() if reuse_dir else root)})
        elif backend and "chunk-extract" in modes:
            markdown = source_dir / "document.md" if source_dir is not None else None
            if markdown is not None and markdown.is_file():
                inputs["backend_extract"] = "reused_markdown"
                reused.append({"kind": "markdown", **_file(markdown, Path(reuse_dir).resolve())})
            else:
                inputs["backend_extract"] = "source_documents"
        if contract["stage"] in {"plugin", "all"}:
            inputs["plugin_extract"] = "reused_markdown" if inputs.get("backend_extract") == "reused_markdown" else "source_documents"
        sources = sorted((_file(path, root) for path in case.get("sources", [])), key=lambda item: item["path"] or "")
        entry = {
            "id": label, "group": group, "case": name,
            "sources": sources, "golden": _file(case.get("golden"), root),
            "ontology": _ontology(case.get("ontology"), root),
            "input_mode": inputs, "reused_inputs": reused,
        }
        entries.append(entry)
        if reuse_extract_dir is not None and "backend_extract" in inputs:
            prediction_dir = Path(reuse_extract_dir) / group / name
            prediction_path = prediction_dir / "backend_extract.json"
            if prediction_path.is_file():
                source_chunks = _file(prediction_dir / "chunks.json", Path(reuse_extract_dir).resolve())
                selected_chunks = next((item for item in reused if item["kind"] == "chunks"), {})
                source_case = next((item for item in prediction_manifest.get("cases") or []
                                    if isinstance(item, dict) and item.get("id") == label), {})
                source_ontology = source_case.get("ontology") or {}
                if not isinstance(source_ontology, dict):
                    source_ontology = {}
                ancestors = prediction_manifest.get("reused_predictions") or []
                ancestor_verified = isinstance(ancestors, list) and all(
                    not isinstance(item, dict) or item.get("id") != label or item.get("input_verified") is True
                    for item in ancestors
                )
                predictions.append({
                    "id": label,
                    "prediction": _file(prediction_path, Path(reuse_extract_dir).resolve()),
                    "source_scores": _file(prediction_dir / "scores.json", Path(reuse_extract_dir).resolve()),
                    "source_chunks": source_chunks,
                    "source_manifest": _file(Path(reuse_extract_dir) / "manifest.json", Path(reuse_extract_dir).resolve()),
                    "source_ontology": source_ontology,
                    "input_verified": bool(
                        source_chunks.get("sha256") and source_chunks["sha256"] == selected_chunks.get("sha256")
                        and source_ontology.get("sha256") and source_ontology["sha256"] == entry["ontology"].get("sha256")
                        and ancestor_verified
                    ),
                })
    entries.sort(key=lambda item: item["id"])
    predictions.sort(key=lambda item: item["id"])
    paths = [Path(path).resolve() for path in evaluator_paths]
    code_root = Path(os.path.commonpath([str(path.parent) for path in paths])) if paths else root
    code = sorted((_file(path, code_root) for path in paths), key=lambda item: item["path"] or "")
    return {
        "schema_version": SCHEMA_VERSION,
        "hash_algorithm": "sha256",
        "cases": entries,
        "dataset_sha256": _digest(entries),
        "evaluator": {"files": code, "sha256": _digest(code)},
        "settings": snapshot,
        "scoring_contract": contract,
        "reused_predictions": predictions,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _blocks(record: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    backend = _mapping(record.get("backend"))
    modules = _mapping(backend.get("modules"))
    return {
        "backend_parse": _mapping(modules.get("parse")),
        "backend_chunk": _mapping(modules.get("chunk")),
        "backend_extract": _mapping(modules.get("extract")),
        "backend_system": _mapping(backend.get("system")),
        "plugin_extract": _mapping(record.get("plugin")),
    }


def _expected(contract: Mapping[str, Any]) -> set[str]:
    modules: set[str] = set()
    stage, modes = contract.get("stage"), contract.get("modes") or []
    if stage in {"backend", "all"}:
        if "extract" in modes or "chunk-extract" in modes:
            modules.add("backend_extract")
        if "pipeline" in modes:
            modules.add("backend_system")
    if stage in {"plugin", "all"}:
        modules.add("plugin_extract")
    return modules


def compare_baseline(
    current: dict[str, Any],
    baseline: dict[str, Any],
    *,
    max_f1_drop: float,
    max_latency_increase: float,
) -> dict[str, Any]:
    """Compare matched cases/modules after verifying data and scorer parity.

    F1 tolerances are absolute (0.02 means two percentage points); latency
    tolerances are relative (0.20 means 20%). Only stage API elapsed_seconds are
    compared, never case or score wall clocks. Reused parse/chunk stages have
    no new API duration and are skipped; reused predictions require measured
    original inference duration. Missing evidence fails closed.
    """
    if not _number(max_f1_drop) or not 0 <= max_f1_drop <= 1:
        raise ValueError("max_f1_drop must be finite and between 0 and 1")
    if not _number(max_latency_increase) or max_latency_increase < 0:
        raise ValueError("max_latency_increase must be finite and nonnegative")
    issues: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []

    def issue(reason: str, **context: Any) -> None:
        issues.append({"reason": reason, **context})

    def result() -> dict[str, Any]:
        return {"passed": not issues, "issues": issues, "comparisons": comparisons,
                "max_f1_drop": float(max_f1_drop), "max_latency_increase": float(max_latency_increase)}

    manifests: dict[str, Mapping[str, Any]] = {}
    case_maps: dict[str, dict[str, Mapping[str, Any]]] = {}
    for side, summary in (("current", current), ("baseline", baseline)):
        manifest = _mapping(summary.get("manifest"))
        manifests[side] = manifest
        if not manifest:
            issue("missing_manifest", run=side)
            continue
        if manifest.get("schema_version") != SCHEMA_VERSION:
            issue("unsupported_manifest_schema", run=side)
        entries = manifest.get("cases")
        evaluator = _mapping(manifest.get("evaluator"))
        contract = _mapping(manifest.get("scoring_contract"))
        if not isinstance(entries, list) or not entries:
            issue("missing_manifest_cases", run=side)
            entries = []
        if manifest.get("dataset_sha256") != _digest(entries):
            issue("invalid_dataset_fingerprint", run=side)
        code = evaluator.get("files")
        if not isinstance(code, list) or not code or any(_mapping(item).get("missing") or not _mapping(item).get("sha256") for item in code):
            issue("missing_evaluator_files", run=side)
        elif evaluator.get("sha256") != _digest(code):
            issue("invalid_evaluator_fingerprint", run=side)
        if not contract or contract != _scoring(_mapping(manifest.get("settings"))):
            issue("invalid_scoring_contract", run=side)
        # Top-level run metadata must not contradict its manifest.
        for key in ("stage", "modes", "match_mode"):
            if key in summary:
                actual = sorted(set(summary[key])) if key == "modes" and isinstance(summary[key], list) else summary[key]
                if actual != contract.get(key):
                    issue("summary_contract_mismatch", run=side, field=key)
        ids: list[str] = []
        for raw in entries:
            entry = _mapping(raw)
            label = entry.get("id")
            ids.append(str(label))
            files = list(entry.get("sources") or []) + [entry.get("golden"), entry.get("ontology")] + list(entry.get("reused_inputs") or [])
            missing = [item.get("path") for raw_file in files if not (item := _mapping(raw_file)).get("sha256") or item.get("missing")]
            if not entry.get("sources") or missing:
                issue("missing_dataset_files", run=side, case=label, files=missing)
        if len(ids) != len(set(ids)):
            issue("duplicate_manifest_cases", run=side)
        for raw_prediction in manifest.get("reused_predictions") or []:
            prediction = _mapping(raw_prediction)
            entry = next((_mapping(item) for item in entries if _mapping(item).get("id") == prediction.get("id")), {})
            selected_chunks = next((_mapping(item) for item in entry.get("reused_inputs") or [] if _mapping(item).get("kind") == "chunks"), {})
            source_chunks = _mapping(prediction.get("source_chunks"))
            source_ontology = _mapping(prediction.get("source_ontology"))
            if (prediction.get("input_verified") is not True or not source_chunks.get("sha256")
                    or source_chunks.get("sha256") != selected_chunks.get("sha256")
                    or not _mapping(prediction.get("source_manifest")).get("sha256")
                    or not source_ontology.get("sha256")
                    or source_ontology.get("sha256") != _mapping(entry.get("ontology")).get("sha256")):
                issue("reused_prediction_input_unverified", run=side, case=prediction.get("id"))
        records = summary.get("cases") or []
        index: dict[str, Mapping[str, Any]] = {}
        for raw in records:
            record = _mapping(raw)
            label = f"{record.get('group')}/{record.get('case')}"
            if label in index:
                issue("duplicate_cases", run=side, case=label)
            index[label] = record
        case_maps[side] = index
        if set(index) != set(ids):
            issue("manifest_case_mismatch", run=side)
        if not index:
            issue("no_cases", run=side)
        if summary.get("interrupted") or summary.get("failed"):
            issue("failed_or_interrupted_run", run=side)
    if len(case_maps) != 2:
        return result()
    left, right = manifests["current"], manifests["baseline"]
    if set(case_maps["current"]) != set(case_maps["baseline"]):
        issue("case_set_mismatch")
    for field in ("dataset_sha256", "scoring_contract"):
        if left.get(field) != right.get(field):
            issue(f"{field}_mismatch")
    if _mapping(left.get("evaluator")).get("sha256") != _mapping(right.get("evaluator")).get("sha256"):
        issue("evaluator_mismatch")
    if issues:
        return result()

    contract = _mapping(left.get("scoring_contract"))
    expected = _expected(contract)
    if not expected:
        issue("no_expected_modules")
    match_mode = contract.get("match_mode")
    for label in sorted(case_maps["current"]):
        records = {side: case_maps[side][label] for side in ("current", "baseline")}
        blocks = {side: _blocks(record) for side, record in records.items()}
        for side, record in records.items():
            if record.get("status") != "succeeded":
                issue("unsuccessful_case", run=side, case=label, status=record.get("status"))
            if not record.get("scored"):
                issue("unscored_case", run=side, case=label)
        for module in sorted(expected):
            values: dict[str, Mapping[str, Any]] = {}
            for side in ("current", "baseline"):
                block = blocks[side][module]
                metrics = _mapping(block.get("metrics"))
                if block.get("status") not in _SUCCESS:
                    issue("unsuccessful_module", run=side, case=label, module=module, status=block.get("status"))
                if not metrics:
                    issue("missing_metrics", run=side, case=label, module=module)
                    continue
                actual_mode = metrics.get("match_mode")
                if actual_mode != match_mode:
                    issue("scorer_mismatch", run=side, case=label, module=module, actual=actual_mode, expected=match_mode)
                    continue
                values[side] = metrics
            for metric_side in ("entities", "relations"):
                f1 = {side: _mapping(values.get(side, {}).get(metric_side)).get("f1") for side in ("current", "baseline")}
                if any(not _number(value) or not 0 <= value <= 1 for value in f1.values()):
                    issue("missing_or_invalid_f1", case=label, module=module, metric=metric_side)
                    continue
                drop = f1["baseline"] - f1["current"]
                comparison = {"case": label, "module": module, "metric": f"{metric_side}.f1", **f1,
                              "drop": round(drop, 12), "passed": drop <= max_f1_drop + 1e-12}
                comparisons.append(comparison)
                if not comparison["passed"]:
                    issue("f1_regression", **comparison)
        # Include parse/chunk when they actually made API calls, and all scored
        # modules even when missing, so a missing timing cannot vanish silently.
        timed = expected | {module for module in ("backend_parse", "backend_chunk")
                            if any(blocks[side][module].get("status") in _SUCCESS for side in blocks)}
        for module in sorted(timed):
            current_block, baseline_block = (blocks[side][module] for side in ("current", "baseline"))
            statuses = (current_block.get("status"), baseline_block.get("status"))
            if module in {"backend_parse", "backend_chunk"}:
                if statuses == ("reused", "reused"):
                    continue
                if statuses != ("succeeded", "succeeded"):
                    issue("stage_execution_mismatch", case=label, module=module, current=statuses[0], baseline=statuses[1])
                    continue
            durations = {"current": current_block.get("elapsed_seconds"), "baseline": baseline_block.get("elapsed_seconds")}
            if any(not _number(value) or value < 0 for value in durations.values()):
                issue("missing_or_invalid_latency", case=label, module=module)
                continue
            base, now = durations["baseline"], durations["current"]
            # A zero baseline does not provide a usable relative latency scale.
            if base <= 0:
                issue("nonpositive_baseline_latency", case=label, module=module)
                continue
            if now <= 0:
                issue("nonpositive_current_latency", case=label, module=module)
                continue
            increase = now / base - 1
            comparison = {"case": label, "module": module, "metric": "api_elapsed_seconds", **durations,
                          "relative_increase": round(increase, 12), "passed": increase <= max_latency_increase + 1e-12}
            comparisons.append(comparison)
            if not comparison["passed"]:
                issue("latency_regression", **comparison)
    return result()
