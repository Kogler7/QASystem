"""Run resumable controlled diagnostics or complete single-pair scoring."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import time

from eval.judge_protocols import ChoiceJudge, compatible_candidates, controlled_input, score_complete


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def execute(dataset, job):
    payload, target = controlled_input(dataset["cases"][job["case"]], job["collection"], job["g"], job["p"], job["mode"])
    kind = job.get("kind", "clef")
    judge = ChoiceJudge(kind=kind, base_url="http://clef:8188" if kind == "clef" else "https://api.deepseek.com/v1",
        model="clef-flash" if kind == "clef" else "deepseek-flash",
        api_key="" if kind == "clef" else os.environ["DEEPSEEK_API_KEY"])
    result = judge.classify(payload)
    return {**job, **result, "decision": result["choices"].get(target),
            "request_sha256": hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest(),
            "state_chars": len(payload["state"]), "question_count": len(payload["questions"]),
            "target_question": target}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", choices=("full", "diagnostic"), required=True)
    parser.add_argument("--cases", help="Comma separated case names for stratified full scoring")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--diagnostic-modes", default="local_pair,context_original_one,context_first_one,context_original_all")
    parser.add_argument("--skip-api-control", action="store_true")
    args = parser.parse_args()
    dataset = read(args.dataset)
    fingerprint = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {"dataset_sha256": fingerprint, "task": args.task, "cases": args.cases,
                "workers": args.workers, "label_scope": "agent-reviewed provisional, source not reviewed"}
    if args.task == "diagnostic":
        manifest.update(diagnostic_modes=args.diagnostic_modes, skip_api_control=args.skip_api_control)
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists() and read(manifest_path) != manifest:
        raise ValueError("resume manifest differs; choose a fresh output directory")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if args.task == "full":
        names = args.cases.split(",") if args.cases else list(dataset["cases"])
        jobs = []
        for name in names:
            for collection in ("entities", "relations"):
                graph = dataset["cases"][name][collection]
                for g, p in compatible_candidates(graph["golden"], graph["predicted"], relation=collection == "relations"):
                    jobs.append({"id": f"{name}/{collection}/{g}/{p}", "case": name,
                                 "collection": collection, "g": g, "p": p, "mode": "local_pair", "kind": "clef"})
    else:
        jobs = []
        for pair in dataset["pairs"]:
            for mode in args.diagnostic_modes.split(","):
                for repetition in range(3):
                    jobs.append({**pair, "id": f"{pair['id']}/{mode}/{repetition}", "pair_id": pair["id"],
                                 "mode": mode, "repetition": repetition, "kind": "clef"})
            for repetition in range(0 if args.skip_api_control else 3):
                jobs.append({**pair, "id": f"{pair['id']}/api_local/{repetition}", "pair_id": pair["id"],
                             "mode": "local_pair", "repetition": repetition, "kind": "api"})
    path = args.output / "calls.jsonl"
    existing = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    cache = {row["id"]: row for row in existing}
    pending = [job for job in jobs if job["id"] not in cache or (args.retry_failed and cache[job["id"]]["status"] != "succeeded")]
    print(json.dumps({"phase": "start", "expected": len(jobs), "cached": len(jobs) - len(pending), "pending": len(pending), "time": time.strftime("%Y-%m-%d %H:%M:%S")}), flush=True)
    started = time.perf_counter()
    with path.open("a", encoding="utf-8") as stream:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for count, row in enumerate(pool.map(lambda job: execute(dataset, job), pending), 1):
                cache[row["id"]] = row
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                if count % 20 == 0 or row["status"] != "succeeded" or count == len(pending):
                    print(json.dumps({"phase": row["case"], "completed_new": count, "pending": len(pending), "status": row["status"], "time": time.strftime("%Y-%m-%d %H:%M:%S")}), flush=True)
    rows = [cache[job["id"]] for job in jobs]
    result = {"wall_seconds_this_session": round(time.perf_counter() - started, 3),
              "expected": len(jobs), "actual_calls_all_sessions": len(existing) + len(pending),
              "answered": sum(row["status"] == "succeeded" for row in rows),
              "failed": sum(row["status"] != "succeeded" for row in rows)}
    if args.task == "full":
        result["cases"] = {name: score_complete(dataset["cases"][name], [row for row in rows if row["case"] == name]) for name in names}
    else:
        result["labels"] = dataset["pairs"]
    (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
