"""Versioned judge calibration corpus: reference-relative labels, no model voting."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = "judge-corpus-v1"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",", ":")).encode()).hexdigest()


def read_jsonl(path):
    with Path(path).open(encoding="utf-8-sig") as handle:
        for number,line in enumerate(handle,1):
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError as exc:
                    raise ValueError(f"{path}:{number}: invalid JSON") from exc


def write_jsonl(path, rows):
    with Path(path).open("w",encoding="utf8",newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+"\n")


def validate(directory):
    directory=Path(directory)
    manifest=json.loads((directory/"manifest.json").read_text(encoding="utf8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported corpus schema")
    for name,sha in manifest["files"].items():
        if Path(name).name != name or hashlib.sha256((directory/name).read_bytes()).hexdigest()!=sha:
            raise ValueError(f"corpus hash mismatch: {name}")
    sources={row["source_id"] for row in json.loads((directory/"sources.json").read_text(encoding="utf8"))}
    ids=set();input_hashes=set();documents={};families={};n=0
    for track in ["pairs", "windows"]:
        for row in read_jsonl(directory/f"{track}.jsonl"):
            n+=1
            if row.get("id") in ids:raise ValueError("duplicate sample id")
            ids.add(row["id"])
            if row.get("split") not in ["calibration","test"]:raise ValueError("invalid split")
            doc=row["document_id"]
            if doc in documents and documents[doc]!=row["split"]:raise ValueError("document leakage across splits")
            documents[doc]=row["split"]
            family=row.get("document_family",doc)
            if family in families and families[family]!=row["split"]:raise ValueError("document family leakage across splits")
            families[family]=row["split"]
            if not row.get("label",{}).get("rationale"):raise ValueError("missing adjudication rationale")
            if row["label"].get("source") not in ["contract_rule","agent_review","human_review"]:raise ValueError("untrusted label source")
            if not row.get("capabilities"):raise ValueError("missing capability")
            if not row.get("origins") or any(o["source_id"] not in sources for o in row["origins"]):raise ValueError("unresolved provenance")
            if row["kind"] not in ["entity","relation"]:raise ValueError("invalid task kind")
            if track=="pairs":
                if type(row.get("expected_match")) is not bool:raise ValueError("pair label must be boolean")
                content=fingerprint({k:row[k] for k in ["kind","golden","predicted"]})
                if content in input_hashes:raise ValueError("duplicate pair content, possible leakage")
                input_hashes.add(content)
            else:
                gs,ps=row["golden_items"],row["predicted_items"]
                if not gs or not ps:raise ValueError("empty window")
                pairs=row["expected_pairs"]
                if len(pairs)!=len({tuple(p) for p in pairs}):raise ValueError("duplicate expected window edge")
                if any(len(p)!=2 or type(p[0]) is not int or type(p[1]) is not int or not 0<=p[0]<len(gs) or not 0<=p[1]<len(ps) for p in pairs):raise ValueError("invalid expected edge")
                if len(row.get("cell_labels",[]))!=len(gs)*len(ps):raise ValueError("window must adjudicate every cell")
                coords={(c["golden_index"],c["predicted_index"]) for c in row["cell_labels"]}
                if coords!={(i,j) for i in range(len(gs)) for j in range(len(ps))}:raise ValueError("incomplete window labels")
                positive={(c["golden_index"],c["predicted_index"]) for c in row["cell_labels"] if c["expected_match"]}
                if positive!={tuple(p) for p in pairs}:raise ValueError("cell/edge label mismatch")
    return {"samples":n,"documents":len(documents),"pairs":len(input_hashes),"schema_version":SCHEMA_VERSION}


def classification(rows):
    """Failures affect end-to-end correctness, never masquerade as negatives."""
    valid=[r for r in rows if r.get("status")=="succeeded"]
    tp=sum(r["expected"] and r["predicted"] for r in valid)
    tn=sum(not r["expected"] and not r["predicted"] for r in valid)
    fp=sum(not r["expected"] and r["predicted"] for r in valid)
    fn=sum(r["expected"] and not r["predicted"] for r in valid)
    div=lambda a,b:a/b if b else None
    recall=div(tp,tp+fn);specificity=div(tn,tn+fp)
    return {"total":len(rows),"valid":len(valid),"failed":len(rows)-len(valid),"coverage":div(len(valid),len(rows)),
            "tp":tp,"tn":tn,"fp":fp,"fn":fn,"accuracy_valid":div(tp+tn,len(valid)),"accuracy_end_to_end":div(tp+tn,len(rows)),
            "precision":div(tp,tp+fp),"recall":recall,"specificity":specificity,
            "balanced_accuracy":(recall+specificity)/2 if recall is not None and specificity is not None else None,
            "f1":div(2*tp,2*tp+fp+fn)}
