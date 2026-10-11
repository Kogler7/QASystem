"""Read-only, content-free extraction trace audit; no provider calls."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from eval.tools.repetition_causal import expansion_trajectory
from eval.tools.repetition_study import metrics


def read(path):
    return json.loads(Path(path).read_text(encoding="utf8"))


def attempt_category(attempt):
    message = str(attempt.get("message") or "")
    if message == "Output split call budget exhausted":
        return "work_budget_exhausted"
    if message == "Document deadline exhausted":
        return "document_deadline_unstarted"
    if "queue wait budget exhausted" in message:
        return "queue_budget_exhausted"
    if attempt.get("type") == "incomplete" and "length" in message:
        return "response_length"
    return str(attempt.get("type") or "unknown")


def http_category(http):
    error = str(http.get("error_type") or "")
    if error == "input_limit":
        return "local_input_limit"
    transmitted = float(http.get("http_seconds") or 0) > 0
    if transmitted:
        return "http_timeout" if "timeout" in error.lower() else ("http_error" if error else "http_returned")
    return "local_timeout" if "timeout" in error.lower() else "local_other_failure"


def audit_trace(directory):
    directory = Path(directory)
    manifest = read(directory / "manifest.json")
    result = read(directory / "result.json") if (directory / "result.json").exists() else {}
    http = {h.get("call_id"): h for h in (read(f) for f in directory.glob("*-http.json"))}
    stages = defaultdict(Counter)
    responses = []
    for file in sorted(directory.glob("*-call-response.json")):
        call_id = file.name.removesuffix("-response.json")
        call = read(directory / (call_id + ".json"))
        response = read(file)
        stage = str(call.get("context", {}).get("pass_name") or "unknown")
        counts = stages[stage]
        counts["generation_entries"] += 1
        category = http_category(http[call_id]) if call_id in http else "http_record_missing"
        counts[category] += 1
        counts["actual_http"] += int(category in {"http_returned", "http_timeout", "http_error"})
        finish = response.get("finish_reason")
        if finish in ("stop", "length"):
            counts["finish_" + finish] += 1
        text = response.get("text") or ""
        m, trajectory = metrics(text), expansion_trajectory(text)
        counts["complete_records"] += m["records"]
        counts["exact_duplicate_records"] += m["records"] - m["unique_records"]
        severe = trajectory["sustained_zero_novel_stop_record"] is not None
        counts["severe_repeat_responses"] += int(severe)
        usage = response.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if isinstance(usage.get(key), (int, float)):
                counts[key] += usage[key]
        # Intentionally exclude text, schema, arbitrary exception messages and URLs.
        responses.append({"call_id": call_id, "stage": stage, "http_category": category,
                          "finish_reason": finish if finish in ("stop", "length") else None,
                          "elapsed_seconds": response.get("elapsed_seconds"),
                          "complete_records": m["records"], "unique_records": m["unique_records"],
                          "max_exact_multiplicity": m["max_exact_multiplicity"],
                          "zero_novel_tail_records": trajectory["zero_novel_tail_records"],
                          "severe_repeat": severe})
    returned_ids = {r["call_id"] for r in responses}
    for file in sorted(directory.glob("*-call.json")):
        call_id = file.stem
        if call_id in returned_ids:
            continue
        call = read(file)
        stage = str(call.get("context", {}).get("pass_name") or "unknown")
        stages[stage]["generation_entries"] += 1
        stages[stage]["response_artifact_missing"] += 1
        category = http_category(http[call_id]) if call_id in http else "http_record_missing"
        stages[stage][category] += 1
        stages[stage]["actual_http"] += int(category in {"http_returned", "http_timeout", "http_error"})
        responses.append({"call_id": call_id, "stage": stage, "http_category": category,
                          "response_artifact_missing": True, "finish_reason": None})
    terminals = []
    for attempt in result.get("attempts", []):
        if attempt.get("terminal"):
            category = attempt_category(attempt)
            stages[str(attempt.get("pass") or "unknown")]["terminal_" + category] += 1
            terminals.append({"stage": attempt.get("pass"), "category": category,
                              "pending_items": attempt.get("pending_items")})
    totals = Counter()
    for counts in stages.values():
        totals.update(counts)
    run = result.get("run_trace") or {}
    return {"trace_id": manifest.get("trace_id"), "result_present": bool(result),
            "algorithm_status": result.get("status"), "algorithm_error_code": result.get("error_code"),
            "elapsed_seconds": run.get("elapsed_seconds"), "totals": dict(totals),
            "stages": {k: dict(v) for k, v in stages.items()}, "terminal_attempts": terminals,
            "responses": responses,
            "notes": ["Actual transport inferred from positive http_seconds; a http artifact alone is insufficient.",
                      "Severe repetition is offline >=40 records and last20 zero-new exact objects, not server cancellation.",
                      "Missing response artifacts are unfinished or interrupted, not inferred model timeouts or loops.",
                      "Complete object counts and deduplication are not semantic quality labels."]}


def audit_tree(trace_root):
    root = Path(trace_root)
    result = {}
    for path in sorted(root.rglob("manifest.json")):
        tid = read(path).get("trace_id")
        if not tid or not list(path.parent.glob("*-call.json")):
            continue
        item = audit_trace(path.parent)
        previous = result.get(tid)
        # Local directories may contain an older snapshot of the same trace.
        if previous is None or (item["result_present"], item["totals"].get("generation_entries", 0)) > (
                previous["result_present"], previous["totals"].get("generation_entries", 0)):
            result[tid] = item
    return result


def audit_evaluation(root, traces, trace_map=None):
    """Join public status and numeric scores without copying graph or errors."""
    root = Path(root)
    mapped = trace_map or {}
    cases = []
    numeric = ("golden", "predicted", "tp", "fp", "fn", "precision", "recall", "f1")
    for file in sorted(root.rglob("scores.json")):
        if "attempts" in file.relative_to(root).parts:
            continue
        score = read(file)
        extraction = score.get("backend", {}).get("modules", {}).get("extract", {})
        backend = file.parent / "backend_extract.json"
        quality = ((read(backend).get("data") or {}).get("quality") or {}) if backend.exists() else {}
        key = file.parent.relative_to(root).as_posix()
        tid = quality.get("trace_id") or mapped.get(key)
        metrics = extraction.get("metrics") or {}
        cases.append({"case_path": key, "trace_id": tid,
                      "runner_status": score.get("status"), "public_status": quality.get("status"),
                      "public_error_code": quality.get("error_code"), "backend_result_present": backend.exists(),
                      "elapsed_seconds": extraction.get("elapsed_seconds"),
                      "numeric_metrics": {kind: {k: value[k] for k in numeric if k in value}
                                          for kind in ("entities", "relations")
                                          if isinstance((value := metrics.get(kind)), dict)},
                      "trace": traces.get(tid)})
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evaluation-root", type=Path)
    parser.add_argument("--trace-map", type=Path, help="Explicit case_path to trace_id mapping for failed HTTP envelopes")
    args = parser.parse_args()
    # Keep derived artifacts under tmp, including on Windows.
    target = args.output.resolve()
    if not target.is_relative_to(Path("tmp").resolve()):
        parser.error("--output must be under repository tmp/")
    result = audit_tree(args.trace_root)
    output = {"traces": result}
    if args.evaluation_root:
        output["cases"] = audit_evaluation(args.evaluation_root, result, read(args.trace_map) if args.trace_map else {})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({"traces": len(result), "output": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
