"""Live V3.1 routing diagnostic over frozen benchmark inputs (no extraction/judge).

Reuse eval.run discovery/ontology/request contracts and the production V3.1
selection/assembly stage. Expected labels never enter model messages. The fixed
loopback CFuse transport handles credentials outside this diagnostic.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from eval.run import (DEFAULT_PARAMS, collect_source_files, discover_cases, discover_types,
                      extraction_item_payloads, read_ontology,
                      resolve_ontology_path, write_json)
from eval.tools.cfuse_eval_proxy import MODEL
from algorithm.knowledge_extract.v3_1.chunk_context import ExtractionChunk
from algorithm.knowledge_extract.v3_1.llm_client import create_llm_client
from algorithm.knowledge_extract.v3_1.ontology_compiler import compile_plan
from algorithm.knowledge_extract.v3_1.prompt_router import resolve_document_route
from algorithm.knowledge_extract.v3_1.skill_planner import SkillAssemblyPlanner
from algorithm.knowledge_extract.v3_1.skill_registry import SkillRegistry

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / "test_data/eval"
RUNS = ROOT / "tmp/eval/runs"


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_request(path: Path) -> list[ExtractionChunk]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    items = payload.get("data", {}).get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(f"Missing frozen chunks: {path.relative_to(ROOT)}")
    if any(not isinstance(item, dict) or not isinstance(item.get("text"), str) for item in items):
        raise ValueError(f"Invalid frozen chunks: {path.relative_to(ROOT)}")
    # Same text/range/metadata contract as the HTTP evaluator; metadata is not
    # supplied to the selection model by the production selector.
    return [ExtractionChunk(chunk_id=f"chunk-{i:04d}", text=item["text"],
            metadata=item.get("metadata") or {})
            for i, item in enumerate(extraction_item_payloads(items))]


def discover_requests() -> list[dict]:
    expectations = json.loads((WORKSPACE / "skill-routing.expected.json").read_text())
    groups = DEFAULT_PARAMS.get("groups") or {}
    requests = []
    for domain in discover_types(WORKSPACE):
        for case in discover_cases(domain):
            if domain.name not in expectations:
                raise ValueError(f"Missing routing expectation: {domain.name}")
            ontology_path = resolve_ontology_path(WORKSPACE, domain, case, groups)
            ontology = read_ontology(ontology_path)
            sources = collect_source_files(case / "docs")
            if not sources:
                raise ValueError(f"No source documents: {case.name}")
            # COSEC: IDs below are local report data; never model input or paths
            # returned by the model. Ref resolution stays in the fixed registry.
            units = [("case", None, case / "chunks.json")]
            units.extend(("document", source, case / "chunks" / f"{source.stem}.json")
                         for source in sources)
            for scope, source, chunks_path in units:
                chunks = load_request(chunks_path)
                requests.append({"id": f"request-{len(requests):03d}", "scope": scope,
                    "case": f"{domain.name}/{case.name}",
                    "document": source.name if source else None,
                    "documents": [s.name for s in sources] if source is None else [source.name],
                    "expected": deepcopy(expectations[domain.name]), "ontology": ontology,
                    "ontology_sha256": sha(ontology.encode()),
                    "chunks_path": str(chunks_path.relative_to(ROOT)),
                    "chunks_sha256": sha(chunks_path.read_bytes()), "chunks": chunks,
                    "source_sha256": {s.name: sha(s.read_bytes()) for s in
                                       (sources if source is None else [source])}})
    return requests


def evaluate(request: dict, registry: SkillRegistry, repeat: int) -> dict:
    llm = create_llm_client(base_url="http://127.0.0.1:18089/v1", model=MODEL,
        api_key="not-needed", max_tokens=2048, disable_thinking=True, request_timeout=120)
    llm.deadline = time.monotonic() + 180
    planner = SkillAssemblyPlanner(registry)
    plan = compile_plan(request["ontology"], mode="permissive")
    route = resolve_document_route(plan, request["chunks"]).to_dict()
    attempts, calls = [], []
    start = time.perf_counter()
    decision = planner.select_for_request(route=route, ontology=request["ontology"], plan=plan,
        chunks=request["chunks"], llm=llm, use_scheduler=True, attempts=attempts,
        call_metrics=calls, max_attempts=2, retry_base_seconds=1)
    audit = planner.audit()[0]
    actual = sorted(entry["skill_id"] for entry in audit["read_skills"])
    expected = request["expected"]
    matched = decision["status"] == expected["status"] and actual == sorted(expected["skills"])
    # Check that selection also loads exactly those rules in every supported stage.
    stage_checks = {}
    for stage, selections in audit["stages"].items():
        stage_checks[stage] = sorted(s["skill_id"] for s in selections) == actual
    return {"id": request["id"], "repeat": repeat, "scope": request["scope"],
        "case": request["case"], "document": request["document"], "expected": expected,
        "actual_skills": actual, "matched": matched and all(stage_checks.values()),
        "stage_checks": stage_checks, "elapsed_seconds": round(time.perf_counter() - start, 3),
        "document_route": route, "selection": decision, "audit": audit,
        "attempts": attempts, "calls": calls}


def summary(results: list[dict], requests: list[dict], *, executed: bool, repeats: int = 1) -> dict:
    scopes = {}
    for scope in ("document", "case"):
        rows = [r for r in results if r["scope"] == scope]
        valid = [r for r in rows if r["selection"]["status"] in {"selected", "no_match"}]
        scopes[scope] = {"requests": sum(r["scope"] == scope for r in requests),
            "runs": len(rows), "valid_decisions": len(valid),
            "matched": sum(r["matched"] for r in rows),
            "accuracy": sum(r["matched"] for r in rows) / len(rows) if rows else None}
    complete = executed and bool(requests) and len(results) == len(requests) * repeats
    return {"mode": "routing-only", "executed": executed, "complete": complete,
        "planned_runs": len(requests) * repeats, "model": MODEL, "scopes": scopes,
        "passed": all(r["matched"] for r in results) if complete else None,
        "failed": [{k: r[k] for k in ("id", "repeat", "case", "document", "actual_skills", "expected")}
                   for r in results if not r["matched"]],
        "extraction_quality_gate": None, "http_pipeline_verified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--case", action="append", default=[], help="Optional exact group/case filter")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    # COSEC: Write only fresh runs beneath the evaluation output boundary; no
    # arbitrary paths, symlinks out of tmp, or overwrite of benchmark/golden.
    if not output.is_relative_to(RUNS.resolve()) or output == RUNS.resolve():
        parser.error("output must be a new directory beneath tmp/eval/runs")
    if not 1 <= args.repeats <= 10 or not 1 <= args.workers <= 4:
        parser.error("repeats must be 1..10; workers must be 1..4")
    requests = discover_requests()
    if args.case:
        unknown = set(args.case) - {r["case"] for r in requests}
        if unknown:
            parser.error("unknown case filter")
        requests = [r for r in requests if r["case"] in args.case]
    registry = SkillRegistry()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "mode": "routing-only",
        "input_source": "benchmark frozen per-document and case chunks; no reparse/rechunk",
        "model": MODEL, "repeats": args.repeats, "catalog": registry.selection_catalog(),
        "catalog_sha256": registry.snapshot_sha256,
        "code_sha256": {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in
            (ROOT / "algorithm/knowledge_extract/v3_1").glob("*.py")},
        "requests": [{k: v for k, v in r.items() if k not in {"chunks", "ontology"}} for r in requests]}
    write_json(output / "manifest.json", manifest)
    for r in requests:
        write_json(output / r["id"] / "input.json", {"ontology": r["ontology"],
                   "chunks": [c.to_dict() for c in r["chunks"]]})
    results = []
    if not args.dry_run:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            pending = {pool.submit(evaluate, r, registry, repeat): (r, repeat)
                       for r in requests for repeat in range(1, args.repeats + 1)}
            for future in as_completed(pending):
                r, repeat = pending[future]
                try:
                    result = future.result()
                except Exception as exc:
                    # COSEC: Keep exception type only; transport errors can
                    # contain URLs or credential-bearing response details.
                    result = {"id": r["id"], "repeat": repeat, "scope": r["scope"],
                        "case": r["case"], "document": r["document"], "expected": r["expected"],
                        "matched": False, "actual_skills": [],
                        "selection": {"status": "failed", "reason": type(exc).__name__}}
                results.append(result)
                write_json(output / r["id"] / f"result-{repeat}.json", result)
                write_json(output / "summary.json", summary(results, requests, executed=True, repeats=args.repeats))
                print(json.dumps({k: result[k] for k in
                    ("id", "repeat", "scope", "case", "document", "matched", "actual_skills")},
                    ensure_ascii=False), flush=True)
    results.sort(key=lambda r: (r["id"], r["repeat"]))
    final = summary(results, requests, executed=not args.dry_run, repeats=args.repeats)
    write_json(output / "summary.json", final)
    lines = ["# Benchmark Skill 路由检查", "", "仅验证真实 Skill 选择及规则装配；不执行解析、切片、实体抽取或 judge，未验证 HTTP 队列链路。",
             "", f"模型：{MODEL}；重复次数：{args.repeats}；目录 SHA-256：{registry.snapshot_sha256}", "",
             "| 请求 | 文档 | 次数 | 预期 | 实际 | 通过 |", "| --- | --- | --- | --- | --- | --- |"]
    for r in results:
        cells = [r["case"], r["document"] or "完整 case", str(r["repeat"]),
                 ", ".join(r["expected"]["skills"]) or "no_match",
                 ", ".join(r["actual_skills"]) or r["selection"]["status"], str(r["matched"])]
        lines.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
    if args.dry_run:
        lines.extend(["", "仅完成本地输入检查；模型未调用，准确率与通过状态为空。"])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(final, ensure_ascii=False), flush=True)
    return 0 if args.dry_run or final["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
