#!/usr/bin/env python3
"""Reproducible, paired V2/V3 benchmark runner.

The runner deliberately discovers only Markdown chunks and keeps golden data on
the evaluator side of the extraction boundary.  It has no SDK dependency;
provider and algorithm imports happen only when a case is actually run.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import statistics
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parent
while not (ROOT / ".git").exists() or not (ROOT / "pytest.ini").is_file():
    ROOT = ROOT.parent
    if ROOT == ROOT.parent:
        raise RuntimeError("无法定位仓库根目录")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
V2_BENCHMARK = ROOT / "test_data" / "eval-v2"
DOC_SUFFIXES = {".md", ".markdown"}
ONTOLOGY_SUFFIXES = {".puml", ".uml", ".plantuml"}
DEFAULT_DATASETS = ("official", "standard", "simple", "tmp_problem")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def atomic_json_write(path: Path, value: Any) -> None:
    """Write a checkpoint/metadata file so interruption cannot leave bad JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def snapshot_code(paths: Iterable[Path], output_dir: Path, version: str) -> str:
    """Copy hashed source files into an immutable, reviewable code snapshot."""
    seen: set[Path] = set()
    root = output_dir / "code" / version
    entries = []
    for path in paths:
        path = path.resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            rel = path.relative_to(ROOT)
        except ValueError:
            rel = Path("external") / path.name
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = sha256_file(path)
        if target.exists() and sha256_file(target) != digest:
            raise RuntimeError(f"CODE_SNAPSHOT_CHANGED:{version}/{rel}")
        if not target.exists():
            shutil.copy2(path, target)
        entries.append(f"{rel}:{digest}\n")
    return sha256_bytes("".join(sorted(entries)).encode())


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError(f"UNREADABLE_TEXT:{path.name}")


def _case(group: str, path: Path) -> dict[str, Any] | None:
    if not (path / "docs").is_dir() or not (path / "ontology").is_dir():
        return None
    golden = path / "extract" / "golden.json"
    return {"group": group, "case": path.name, "path": path,
            "docs_dir": path / "docs", "ontology_dir": path / "ontology",
            "golden_file": golden if golden.is_file() else None}


def discover_cases(benchmark_dir: Path = V2_BENCHMARK,
                   datasets: Iterable[str] = DEFAULT_DATASETS) -> list[dict[str, Any]]:
    """Use V2's group/case layout, including direct ``simple`` style groups."""
    result = []
    for group in datasets:
        group_dir = benchmark_dir / group
        if not group_dir.is_dir():
            continue
        direct = _case(group, group_dir)
        if direct:
            result.append(direct)
            continue
        for child in sorted(group_dir.iterdir()):
            if child.is_dir() and not child.name.startswith((".", "_")):
                item = _case(group, child)
                if item:
                    result.append(item)
    return result


def doc_files(case: dict[str, Any]) -> list[Path]:
    if not case['docs_dir'].is_dir():
        return []
    direct = sorted(p for p in case["docs_dir"].iterdir()
                    if p.is_file() and p.suffix.lower() in DOC_SUFFIXES)
    return direct or sorted(p for p in case["docs_dir"].rglob("*")
                            if p.is_file() and p.suffix.lower() in DOC_SUFFIXES)


def ontology_files(case: dict[str, Any]) -> list[Path]:
    return sorted(p for p in case["ontology_dir"].rglob("*")
                  if p.is_file() and p.suffix.lower() in ONTOLOGY_SUFFIXES)


def load_golden(case: dict[str, Any]) -> dict[str, Any]:
    path = case.get("golden_file")
    if not path:
        return {"entities": [], "relations": []}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return data if isinstance(data, dict) else {"entities": [], "relations": []}


def freeze_case(case: dict[str, Any], frozen_root: Path) -> dict[str, Any]:
    """Capture source, ontology and golden hashes and copy source inputs."""
    files = doc_files(case) + ontology_files(case)
    golden = case.get("golden_file")
    all_hashes = {str(p.relative_to(case["path"])): sha256_file(p) for p in files}
    if golden:
        all_hashes[str(golden.relative_to(case["path"]))] = sha256_file(golden)
    source_hash = sha256_bytes("".join(f"{k}\0{v}\n" for k, v in sorted(all_hashes.items())).encode())
    target = frozen_root / case["group"] / case["case"]
    target.mkdir(parents=True, exist_ok=True)
    for p in files + ([golden] if golden else []):
        out = target / p.relative_to(case["path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() and sha256_file(out) != sha256_file(p):
            raise RuntimeError(f"FROZEN_INPUT_CHANGED:{out.relative_to(frozen_root)}")
        if out.exists():
            continue
        shutil.copy2(p, out)
    return {"group": case["group"], "case": case["case"],
            "source_hash": source_hash, "files": all_hashes,
            "has_markdown": bool(doc_files(case)), "has_golden": bool(golden),
            "doc_count": len(doc_files(case)), "ontology_count": len(ontology_files(case))}


def _text(v: Any) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    return "" if v is None else str(v).strip()


def entity_key(v: Any) -> tuple[str, str] | None:
    if not isinstance(v, dict): return None
    k = (_text(v.get("type")), _text(v.get("name")))
    return k if all(k) else None


def relation_key(v: Any) -> tuple[str, str, str, str, str] | None:
    if not isinstance(v, dict): return None
    k = (_text(v.get("from_type")), _text(v.get("from_name", v.get("from"))),
         _text(v.get("rel_type", v.get("rel"))), _text(v.get("to_type")),
         _text(v.get("to_name", v.get("to"))))
    return k if all(k) else None  # type: ignore[return-value]


def _items(values: Any, key: Callable[[Any], Any]) -> set[Any]:
    return {k for value in (values or []) if (k := key(value)) is not None}


def metric(tp: int, fp: int, fn: int) -> dict[str, Any]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(p, 6),
            "recall": round(r, 6), "f1": round(2*p*r/(p+r), 6) if p+r else 0.0}


def property_metrics(gold: list[dict[str, Any]], pred: list[dict[str, Any]]) -> dict[str, Any]:
    """Identity aligned property/value triples; duplicates cannot overwrite facts."""
    def normalize(value):
        if isinstance(value, (list,dict)):
            return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        if isinstance(value,float) and value.is_integer():
            return str(int(value))
        return re.sub(r'\s+',' ',_text(value))
    def triples(items):
        return {(entity_key(item), key, normalize(value)) for item in items if entity_key(item)
                for key,value in (item.get('properties') or {}).items() if key != 'name'}
    gv,pv=triples(gold),triples(pred)
    gk,pk={x[:2] for x in gv},{x[:2] for x in pv}
    return {'values':metric(len(gv&pv),len(pv-gv),len(gv-pv)),
            'keys':metric(len(gk&pk),len(pk-gk),len(gk-pk))}


def score_case(golden: dict[str, Any], predicted: dict[str, Any], *, failed: bool = False) -> dict[str, Any]:
    ge, gr = _items(golden.get("entities"), entity_key), _items(golden.get("relations"), relation_key)
    pe, pr = _items(predicted.get("entities"), entity_key), _items(predicted.get("relations"), relation_key)
    if failed: pe, pr = set(), set()
    result = {"entities": {"golden": len(ge), "predicted": len(pe), **metric(len(ge & pe), len(pe-ge), len(ge-pe))},
              "relations": {"golden": len(gr), "predicted": len(pr), **metric(len(gr & pr), len(pr-gr), len(gr-pr))},
              "property_values": property_metrics(golden.get("entities") or [],
                  [] if failed else (predicted.get("entities") or []))}
    values = [value for entity in predicted.get('entities',[]) for value in (entity.get('properties') or {}).values()]
    result['quality'] = {
        'empty_property_values':sum(value is None or isinstance(value,str) and not value.strip() or isinstance(value,(list,dict)) and not value for value in values),
        'property_values':len(values),
        'dangling_relations':sum((r[0],r[1]) not in pe or (r[3],r[4]) not in pe for r in pr),
        'invalid_relation_records':sum(relation_key(r) is None for r in predicted.get('relations',[])),
        'duplicate_entities':len(predicted.get('entities',[]))-len(pe),
        'duplicate_relations':len(predicted.get('relations',[]))-len(pr),
    }
    return result


def aggregate(scores: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for name in ("entities", "relations"):
        c = {k: sum(s[name][k] for s in scores) for k in ("golden", "predicted", "tp", "fp", "fn")}
        out[name] = {"golden": c["golden"], "predicted": c["predicted"], **metric(c["tp"], c["fp"], c["fn"])}
    out["case_macro"] = {name: round(sum(s[name]["f1"] for s in scores) / len(scores), 6) if scores else 0.0
                         for name in ("entities", "relations")}
    out['property_values'] = metric(*(sum(s['property_values']['values'][key] for s in scores) for key in ('tp','fp','fn')))
    out['quality'] = {key:sum(s.get('quality',{}).get(key,0) for s in scores) for key in ('empty_property_values','property_values','dangling_relations','invalid_relation_records','duplicate_entities','duplicate_relations')}
    return out


def run_statistics(rows):
    per_run = {str(number):aggregate([row['score'] for row in rows if row['run']==number and row.get('score')]) for number in sorted({row['run'] for row in rows})}
    means = {}
    for name in ('entities','relations','property_values'):
        means[name] = {}
        for key in ('precision','recall','f1'):
            values = [run[name][key] for run in per_run.values()]
            means[name][key] = {'mean':statistics.mean(values) if values else 0,'stdev':statistics.stdev(values) if len(values)>1 else None}
    return {'per_run':per_run,'statistics':means}


def render_summary(summary):
    lines=['# V2 / V3 benchmark', '',
           '固定输入上的开发集结果；identity 匹配，不评测 evidence。低分事实照常计入 FP。属性指标排除重复的 name 字段。', '',
           '|版本|可评分运行数|实体 Recall|实体 F1|关系 Recall|关系 F1|属性 F1|',
           '|---|---:|---:|---:|---:|---:|---:|']
    for version, rows in summary['results'].items():
        stats=summary['summary'][version]
        cells=[version,str(sum(bool(row.get('score')) for row in rows))]
        cells += [f"{stats[part][key]:.2%}" for part,key in [('entities','recall'),('entities','f1'),('relations','recall'),('relations','f1'),('property_values','f1')]]
        lines.append('|'+ '|'.join(cells)+'|')
    lines += ['', '主表计入两版返回的有效部分结果；strict_summary 将未完整成功的运行记为零预测。失败样本保留在分母。', '',
              f"缺 Markdown：{len(summary['manifest']['missing_markdown'])}；缺 golden：{len(summary['manifest']['missing_golden'])}。未提供的新 holdout 及原始文件解析质量不在本次结论内。"]
    for version,rows in summary['results'].items():
        states={state:sum(row['status']==state for row in rows) for state in sorted({row['status'] for row in rows})}
        calls=[call for row in rows for call in row.get('calls',[])]
        tokens=sum((call.get('usage') or {}).get('total_tokens',0) for call in calls)
        lines += ['', f'## {version}', '', f'状态：{states}；调用数：{len(calls)}；tokens：{tokens}。',
                  f"输出检查：{summary['summary'][version]['quality']}"]
    return '\n'.join(lines)+'\n'


def aggregate_by_domain(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Micro metrics grouped by V2 dataset group, plus case macro per group."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("group", "unknown")), []).append(row["score"])
    return {domain: aggregate(scores) for domain, scores in sorted(grouped.items())}


def cache_key(*, version: str, source_hash: str, config: dict[str, Any],
              version_source_hash: str) -> str:
    """Stable resume key covering model options, frozen input and implementation."""
    payload = {"version": version, "source_hash": source_hash,
               "version_source_hash": version_source_hash, "config": config}
    return sha256_bytes(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":")).encode())


def _safe_error(exc: BaseException) -> str:
    return getattr(exc, "code", None) or type(exc).__name__


def _chunks(case: dict[str, Any]) -> list[Any]:
    from algorithm.knowledge_extract.v2.chunk_context import ExtractionChunk
    chunks = []
    for i, path in enumerate(doc_files(case), 1):
        text = read_text(path)
        if text.strip():
            chunks.append(ExtractionChunk(chunk_id=f"{case['case']}-d{i:02d}", text=text,
                document_id=path.name, metadata={"source_file": path.name, "source_kind": "markdown"}))
    return chunks


def run_case(case: dict[str, Any], version: str, client: Any) -> dict[str, Any]:
    try:
        ontology = "\n\n".join(read_text(p) for p in ontology_files(case)).strip()
        chunks = _chunks(case)
        if version == "v2":
            from algorithm.knowledge_extract.v2.harness_extractor import extract_harness
            from algorithm.knowledge_extract.v2.result_contract import to_public_result
            raw = extract_harness(ontology=ontology, chunks=chunks, llm=client)
            if raw.get('status','succeeded') == 'succeeded' and not raw.get('error_code'):
                result = to_public_result(raw)
            else:
                accepted = raw.get('raw') or {}
                result = {
                    'status':'incomplete' if accepted.get('entities') else 'failed',
                    'entities':[{'type':e.get('type'),'name':e.get('name'),'properties':{k:v for k,v in (e.get('properties') or {}).items() if k!='name'}} for e in accepted.get('entities',[])],
                    'relations':[{key:r.get(key) for key in ('from_type','from_name','rel_type','to_type','to_name')} for r in accepted.get('relations',[])],
                    'diagnostics':[{'code':raw.get('error_code') or 'V2_INCOMPLETE'}],
                }
        else:
            from algorithm.knowledge_extract.v3 import extract_detailed
            result = extract_detailed(chunks, ontology, llm=client)
        if not isinstance(result, dict) or result.get("status") not in (None, "succeeded", "success", "incomplete", "failed"):
            raise RuntimeError("EXTRACTION_FAILED")
        return {"status": str(result.get("status") or "succeeded"), "prediction": {"entities": result.get("entities") or [], "relations": result.get("relations") or []},
                "diagnostics": result.get("diagnostics") or [], "stats": result.get("stats") or {}}
    except Exception as exc:
        return {"status": "failed", "error_code": _safe_error(exc), "prediction": {"entities": [], "relations": []}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--versions", nargs="+", choices=("v2", "v3"), default=["v2", "v3"])
    ap.add_argument("--cases", nargs="*", help="group/case names; default discovers standard V2 groups")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--output-dir", required=True, type=Path)
    ap.add_argument("--api-config", type=Path, default=Path("eval_api.md"))
    ap.add_argument("--freeze-only", action="store_true")
    args = ap.parse_args(argv)
    cases = discover_cases(datasets=DEFAULT_DATASETS)
    if args.cases:
        wanted = set(args.cases)
        cases = [c for c in cases if f"{c['group']}/{c['case']}" in wanted or c["case"] in wanted]
    if not cases:
        ap.error('no matching benchmark cases')
    out = args.output_dir; out.mkdir(parents=True, exist_ok=True)
    frozen = [freeze_case(c, out / "frozen") for c in cases]
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "cases": frozen,
                "missing_markdown": [f"{x['group']}/{x['case']}" for x in frozen if not x["has_markdown"]],
                "missing_golden": [f"{x['group']}/{x['case']}" for x in frozen if not x["has_golden"]]}
    manifest_path = out / "manifest.json"
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if [(x.get("group"), x.get("case"), x.get("source_hash")) for x in old.get("cases", [])] != [(x.get("group"), x.get("case"), x.get("source_hash")) for x in frozen]:
            raise RuntimeError("FROZEN_MANIFEST_CHANGED: use a new output directory")
        manifest = old
    else:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.freeze_only: return 0
    from algorithm.knowledge_extract.v3.client import JSONClient, ProviderConfig
    config = ProviderConfig.from_file(args.api_config)
    code_hashes = {}
    for version in args.versions:
        version_dir = ROOT / "algorithm" / "knowledge_extract" / f"v{version}"
        paths = sorted(version_dir.glob('*.py'))
        paths += [version_dir / 'client.py', Path(__file__)]
        if version == 'v3':
            paths += sorted((ROOT / "algorithm" / "knowledge_extract" / "v2").glob('*.py'))
        code_hashes[version] = snapshot_code(paths, out, version)
    atomic_json_write(out / "run_metadata.json", {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "options": vars(args) | {"output_dir": str(args.output_dir), "api_config": str(args.api_config)},
        "provider": config.safe_dict(), "code_hashes": code_hashes,
        "manifest_hash": sha256_file(manifest_path),
    })
    results = {v: [] for v in args.versions}
    for version in args.versions:
        for run_number in range(max(1, args.runs)):
            def do_case(case):
                started = time.time()
                case_frozen = dict(case)
                case_frozen["path"] = out / "frozen" / case["group"] / case["case"]
                case_frozen["docs_dir"] = case_frozen["path"] / "docs"
                case_frozen["ontology_dir"] = case_frozen["path"] / "ontology"
                gf = case_frozen["path"] / "extract" / "golden.json"
                case_frozen["golden_file"] = gf if gf.exists() else None
                checkpoint = out / "checkpoints" / version / f"{case['group']}__{case['case']}__{run_number+1}.json"
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                entry = next(x for x in frozen if x['group'] == case['group'] and x['case'] == case['case'])
                fingerprint = cache_key(version=version, source_hash=entry['source_hash'], config=config.safe_dict(), version_source_hash=code_hashes[version])
                if checkpoint.exists():
                    cached = json.loads(checkpoint.read_text(encoding="utf-8"))
                    if cached.get('fingerprint') != fingerprint:
                        raise RuntimeError('CHECKPOINT_CONFIGURATION_CHANGED: use a new output directory')
                    return cached
                if not doc_files(case_frozen) or not ontology_files(case_frozen) or not case_frozen['golden_file']:
                    run = {"status": "unavailable", "prediction": {"entities": [], "relations": []}, "diagnostics": ["MISSING_GOLDEN" if not case_frozen['golden_file'] else "MISSING_INPUT"]}
                    run.update(group=case["group"], case=case["case"], run=run_number + 1, score=None, elapsed_seconds=0.0, fingerprint=fingerprint)
                    atomic_json_write(checkpoint, run)
                    return run
                local_client = JSONClient(config, trace_dir=out / 'calls' / version / f"{case['group']}__{case['case']}__{run_number+1}")
                print(f"[{datetime.now().isoformat(timespec='seconds')}] {version} run={run_number+1} {case['group']}/{case['case']} started", flush=True)
                run = run_case(case_frozen, version, local_client)
                score = score_case(load_golden(case_frozen), run["prediction"], failed=run["status"] == "failed")
                strict_score = score_case(load_golden(case_frozen),run['prediction'],failed=run['status'] not in {'success','succeeded'})
                run.update(group=case["group"], case=case["case"], run=run_number + 1, score=score,
                           strict_score=strict_score, elapsed_seconds=round(time.time()-started, 3), fingerprint=fingerprint, calls=local_client.records)
                atomic_json_write(checkpoint, run)
                return run
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                for run in pool.map(do_case, cases):
                    results[version].append(run)
                    print(f"[{version}] {run['group']}/{run['case']} status={run['status']}", flush=True)
    summary = {"options": vars(args) | {"output_dir": str(args.output_dir), "api_config": str(args.api_config)},
               "manifest": manifest, "results": results,
               "summary": {v: aggregate([r["score"] for r in rs if r.get("score")]) for v, rs in results.items()},
               "domain_summary": {v: aggregate_by_domain([r for r in rs if r.get("score")]) for v, rs in results.items()},
               "run_statistics":{v:run_statistics(rows) for v,rows in results.items()},
               "strict_summary":{v:aggregate([r['strict_score'] for r in rows if r.get('strict_score')]) for v,rows in results.items()},
               "provider":config.safe_dict(), "code_hashes":code_hashes,
               "cost": {"records": [call for rows in results.values() for row in rows for call in row.get('calls',[])]}}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out / "summary.md").write_text(render_summary(summary), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
