"""Freeze parsing/chunking once, then run extractor/judge combinations via run.py."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from eval import run
from eval.mini_eval import select_cases


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare_inputs(workspace, cases, destination, params):
    """Fail on partial parsing/chunk fallback; never silently change inputs."""
    prepared = []
    for case in cases:
        relative = case.relative_to(workspace)
        output = destination / relative
        if output.exists():
            raise ValueError(f"Input preparation output already exists: {relative}")
        shutil.copytree(case, output, ignore=shutil.ignore_patterns("chunks.json", "attempts", "ontology"))
        ontology = run.resolve_ontology_path(workspace, case.parent, case, params.get("groups") or {})
        # Resolve case-specific ontology now so all runs use identical bytes.
        if ontology.is_dir():
            shutil.copytree(ontology, output / "ontology")
        else:
            (output / "ontology").mkdir()
            shutil.copy2(ontology, output / "ontology" / ontology.name)
        markdown = ""
        cached = case / "document.md"
        if cached.is_file():
            markdown = cached.read_text(encoding="utf-8")
            parse = {"status": "reused", "source": str(cached)}
        else:
            markdown, parse = run._parse_sources(params["backend_url"], run.collect_source_files(case / "docs"),
                                                  params.get("options") or {}, params, output)
        if parse["status"] not in {"succeeded", "reused"} or not markdown.strip():
            raise RuntimeError(f"Incomplete parsing: {relative}")
        run.write_text(output / "document.md", markdown)
        items, chunk = run._chunk_markdown(params["backend_url"], markdown, case.name,
                                            params.get("options") or {}, params, output)
        if chunk["status"] != "succeeded" or not items:
            raise RuntimeError(f"Incomplete chunking: {relative}")
        record = {"case": relative.as_posix(), "parse": parse, "chunk": chunk,
                  "chunks_sha256": hashlib.sha256((output / "chunks.json").read_bytes()).hexdigest(),
                  "chunk_count": len(items)}
        prepared.append(record)
        save(destination / "prepared.json", prepared)
    return prepared


def build_command(output, workspace, cases, params, extractor, judge, reuse=None):
    # Isolate each run directory even when subprocesses begin simultaneously.
    launcher = "import sys;from pathlib import Path;from eval import run;run.RUNS_DIR=Path(sys.argv.pop(1));sys.exit(run.main())"
    command = [sys.executable, "-c", launcher, str(output / "runs"),
               *(str(workspace / case) for case in cases), "--workspace", str(workspace),
               *run.runtime_cli_args(params), "--stage", "backend", "--mode", "extract",
               "--from-run", str(workspace), "--match-mode", "llm", "--case-attempts", "1",
               "--runs-retention-days", "0", "--require-scored", "--fail-on-empty",
               "--judge-adapter", judge["adapter"], "--judge-model", judge["model"],
               "--judge-base-url", judge["base_url"]]
    if judge.get("key_env"):
        command += ["--judge-key-env", judge["key_env"]]
    if reuse:
        command += ["--reuse-extract-from", str(reuse)]
    else:
        command += ["--extract-version", extractor.get("version", "v3"),
                    "--extract-provider", extractor["provider"], "--extract-model", extractor["model"],
                    "--extract-request-timeout", str(extractor.get("request_timeout", 180)),
                    "--extract-document-timeout", str(extractor.get("document_timeout", 900))]
    return command


def execute(output, workspace, cases, params, extractor, judge, reuse=None):
    output.mkdir(parents=True, exist_ok=False)
    if not reuse and extractor.get("health_url"):
        started = time.monotonic()
        run.log(f"Waiting for model service: {extractor['id']}")
        while True:
            try:
                status, _, _ = run.http_request(extractor["health_url"], timeout=5)
                if status == 200:
                    break
            except OSError:
                pass
            if time.monotonic() - started > 900:
                record = {"extractor": extractor["id"], "judge": judge["id"], "exit_code": 1,
                          "run_dir": None, "summary": None, "error": "model_service_not_ready"}
                save(output / "result.json", record)
                return record
            time.sleep(5)
    command = build_command(output, workspace, cases, params, extractor, judge, reuse)
    # CLI contains no credentials. Secrets are loaded only from named env vars.
    save(output / "command.json", command)
    run.log(f"Matrix start: {extractor['id']} / {judge['id']}")
    with (output / "run.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
    summaries = list((output / "runs").glob("*/summary.json"))
    summary = json.loads(summaries[0].read_text(encoding="utf-8")) if len(summaries) == 1 else None
    record = {"extractor": extractor["id"], "judge": judge["id"], "exit_code": result.returncode,
              "run_dir": str(summaries[0].parent) if summary else None, "summary": summary}
    save(output / "result.json", record)
    run.log(f"Matrix finished: {extractor['id']} / {judge['id']} exit={result.returncode}")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=run.DEFAULT_WORKSPACE)
    run.add_runtime_arguments(parser)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--prepared", type=Path, help="Reuse an already frozen matrix input directory")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args(argv)
    if "tmp" not in args.output.resolve().parts or args.workers < 1:
        parser.error("output must be under tmp/ and workers must be positive")
    params = run.params_from_args(args)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    identifiers = [entry["id"] for entry in plan["extractors"]] + [plan["judge"]["id"]]
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in identifiers) or len({e["id"] for e in plan["extractors"]}) != len(plan["extractors"]):
        parser.error("extractor/judge ids must be unique safe directory names")
    workspace = args.workspace.resolve()
    per_type = {}
    for item in args.mini_per_type.split(","):
        if not item.strip(): continue
        name, count = item.split("=", 1)
        per_type[name.strip()] = int(count)
    cases = [workspace / label for label in plan["cases"]] if plan.get("cases") else select_cases(workspace, {"cases_per_type": args.mini_cases_per_type, "per_type": per_type, "selection": args.mini_selection, "seed": args.mini_seed})[0]
    if any(not c.resolve().is_relative_to(workspace) or not c.is_dir() for c in cases):
        parser.error("cases must be existing directories within workspace")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    frozen = args.prepared.resolve() if args.prepared else output / "inputs"
    if not args.prepared:
        prepare_inputs(workspace, cases, frozen, params)
    labels = [case.relative_to(workspace).as_posix() for case in cases]
    if any(not (frozen / label / "chunks.json").is_file() for label in labels):
        parser.error("prepared inputs lack required chunks.json")
    save(output / "plan.resolved.json", {**plan, "cases": labels, "inputs": str(frozen)})
    if args.prepare_only:
        return 0
    evaluation_params = deepcopy(params)
    evaluation_params["options"].pop("extraction", None)
    save(output / "params.resolved.json", evaluation_params)
    results = []
    lock = threading.Lock()
    def one(extractor):
        result = execute(output / extractor["id"] / plan["judge"]["id"], frozen, labels,
                         evaluation_params, extractor, plan["judge"])
        with lock:
            results.append(result)
            save(output / "results.json", results)
        return result
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(one, plan["extractors"]))
    comparison = plan.get("comparison")
    if comparison:
        source = next(r for r in results if r["extractor"] == comparison["extractor"])
        if not source["run_dir"]:
            raise RuntimeError("API comparison source has no completed run artifacts")
        extractor = next(e for e in plan["extractors"] if e["id"] == comparison["extractor"])
        available = [label for label in labels if (Path(source["run_dir"]) / label / "backend_extract.json").is_file()]
        if not available:
            raise RuntimeError("API comparison source has no saved predictions; will not re-extract")
        save(output / "comparison.coverage.json", {"included": available, "missing_predictions": sorted(set(labels) - set(available))})
        result = execute(output / extractor["id"] / comparison["judge"]["id"], frozen, available,
                         evaluation_params, extractor, comparison["judge"], Path(source["run_dir"]))
        results.append(result)
        save(output / "results.json", results)
    return 0 if all(r["exit_code"] == 0 for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
