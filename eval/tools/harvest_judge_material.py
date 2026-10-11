"""Harvest frozen historical graphs; old judge decisions are proposals, never labels."""
from __future__ import annotations

import argparse
import collections
import datetime
import difflib
import hashlib
import json
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval import run as evaluator


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def log(message):
    print(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def clean(items):
    return evaluator._strip_evidence(items)


def item_key(item, relation=False):
    fields = ("rel_type", "from_type", "from_name", "to_type", "to_name") if relation else ("type", "name")
    return tuple(item.get(k, "") for k in fields)


def harvest(roots, golden_roots, output):
    output.mkdir(parents=True, exist_ok=True)
    goldens = collections.defaultdict(list)
    for root in golden_roots:
        for path in sorted(root.rglob("golden.json")):
            try:
                data = path.read_bytes()
                goldens[hashlib.sha256(data).hexdigest()].append(path)
            except OSError:
                continue
    log(f"indexed {sum(map(len, goldens.values()))} golden files / {len(goldens)} unique snapshots")
    registry = []
    pool = {}
    excluded = []
    scanned = 0
    for root in roots:
        for path in sorted(root.rglob("backend_extract.json")):
            scanned += 1
            # Attempts and copied frozen runs are deduplicated by content below.
            case_dir = path.parent
            if case_dir.parent.name == "attempts":
                case_dir = case_dir.parent.parent
            case = f"{case_dir.parent.name}/{case_dir.name}"
            run_dir = next((p for p in case_dir.parents if (p / "manifest.json").exists()), None)
            manifest_case = None
            if run_dir:
                try:
                    manifest_case = next((c for c in read(run_dir / "manifest.json").get("cases", []) if c.get("id") == case), None)
                except (OSError, ValueError):
                    pass
            sha = (manifest_case or {}).get("golden", {}).get("sha256")
            paths = goldens.get(sha, [])
            resolution = "historical_manifest_hash"
            if not paths:
                # The task evaluates the relation to a given reference, not
                # reference truth. Legacy runs may be explicitly re-referenced;
                # never misrepresent this as their original golden snapshot.
                alternatives = sorted((f for files in goldens.values() for f in files
                                       if "/".join(f.parts[-4:-2]) == case),
                                      key=lambda f: ("eval-v2" not in str(f), str(f)))
                if not alternatives:
                    excluded.append({"path": str(path), "case": case, "reason": "no_case_reference_available", "golden_sha256": sha})
                    continue
                paths = alternatives[:1]
                sha = hashlib.sha256(paths[0].read_bytes()).hexdigest()
                resolution = "explicit_case_reference_not_original_snapshot"
            golden_path = paths[0]
            try:
                ge, gr = evaluator.normalize_graph(read(golden_path))
                pe, pr = evaluator.normalize_graph(evaluator.extract_graph_from_envelope(read(path)))
                ge, gr, pe, pr = map(clean, (ge, gr, pe, pr))
                gr = evaluator._relations_with_entity_context(gr, ge)
                pr = evaluator._relations_with_entity_context(pr, pe)
            except (OSError, ValueError, TypeError) as exc:
                excluded.append({"path": str(path), "case": case, "reason": type(exc).__name__})
                continue
            provenance = {"prediction_path": str(path), "prediction_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "golden_path": str(golden_path), "golden_sha256": sha, "case": case,
                          "reference_resolution": resolution,
                          "document_sources": (manifest_case or {}).get("sources", [])}
            sid = digest({"prediction": provenance["prediction_sha256"], "golden": sha, "case": case})[:20]
            provenance["source_id"] = sid
            registry.append({**provenance, "entities": [len(ge), len(pe)], "relations": [len(gr), len(pr)]})
            historical = {"entity": set(), "relation": set()}
            details_path = case_dir / "match_details.json"
            if details_path.exists():
                try:
                    detail = read(details_path).get("backend_extract", {})
                    for kind, field in [("entity", "entities"), ("relation", "relations")]:
                        for match in detail.get(field, {}).get("matched", []):
                            historical[kind].add((item_key(match.get("golden", {}), kind == "relation"), item_key(match.get("predicted", {}), kind == "relation")))
                except (OSError, ValueError, TypeError):
                    pass
            for kind, gs, ps in [("entity", ge, pe), ("relation", gr, pr)]:
                relation = kind == "relation"
                buckets = collections.defaultdict(list)
                for gi, g in enumerate(gs):
                    buckets[tuple(evaluator._type_identity(g.get(k)) for k in (("rel_type", "from_type", "to_type") if relation else ("type",)))].append((gi, g))
                for pi, pred in enumerate(ps):
                    typekey = tuple(evaluator._type_identity(pred.get(k)) for k in (("rel_type", "from_type", "to_type") if relation else ("type",)))
                    choices = []
                    for gi, gold in buckets[typekey]:
                        auto = bool(evaluator._auto_candidate_pairs([gold], [pred], relation=relation))
                        old = (item_key(gold, relation), item_key(pred, relation)) in historical[kind]
                        name_fields = ("from_name", "to_name") if relation else ("name",)
                        sim = sum(difflib.SequenceMatcher(None, str(gold.get(f, "")), str(pred.get(f, ""))).ratio() for f in name_fields) / len(name_fields)
                        choices.append((auto, old, sim, gi, gold))
                    choices.sort(key=lambda c: (-c[0], -c[1], -c[2], c[3]))
                    chosen = choices[:3]
                    if len(choices) > 3:
                        rng = random.Random(digest({"source": sid, "kind": kind, "pi": pi}))
                        chosen += rng.sample(choices[3:], min(2, len(choices)-3))
                    # Close-name cross-type cases exercise the production hard constraint.
                    crossed = [(gi,g) for gi,g in enumerate(gs) if not evaluator._compatible_types(g,pred,relation=relation) and
                               item_key(g,relation)[-1] == item_key(pred,relation)[-1]]
                    chosen += [(False,False,1,gi,g) for gi,g in crossed[:1]]
                    for auto, old, sim, gi, gold in chosen:
                        key = digest({"case": case,"kind":kind,"golden":gold,"predicted":pred})
                        row = pool.setdefault(key, {"id": "jp-"+key[:20], "document_id":case,"kind":kind,
                                                   "golden":gold,"predicted":pred,"origins":[],
                                                   "proposal":{"local_auto_match":auto,"historical_match":old,"name_similarity":round(sim,4)}})
                        row["proposal"]["historical_match"] |= old
                        origin={"source_id":sid,"golden_index":gi,"predicted_index":pi}
                        if origin not in row["origins"]:row["origins"].append(origin)
            if scanned % 25 == 0:
                log(f"scanned {scanned}, paired {len(registry)}, distinct candidate pairs {len(pool)}")
    (output/"sources.json").write_text(json.dumps(registry,ensure_ascii=False,indent=2),encoding="utf8")
    (output/"excluded_sources.json").write_text(json.dumps(excluded,ensure_ascii=False,indent=2),encoding="utf8")
    with (output/"candidates.jsonl").open("w",encoding="utf8") as handle:
        for row in sorted(pool.values(),key=lambda r:(r["document_id"],r["kind"],r["id"])):
            handle.write(json.dumps(row,ensure_ascii=False)+"\n")
    summary={"scanned_prediction_files":scanned,"paired_prediction_files":len(registry),"unique_prediction_golden_pairs":len({r['source_id'] for r in registry}),
             "unresolved_prediction_files":len(excluded),"unique_candidates":len(pool),"documents":sorted({r["document_id"] for r in pool.values()}),
             "golden_snapshots":len(goldens)}
    (output/"harvest_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf8")
    log(json.dumps(summary,ensure_ascii=False))
    return summary


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots",nargs="+",type=Path,required=True)
    parser.add_argument("--golden-roots",nargs="+",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    harvest(args.roots,args.golden_roots,args.output)
