"""Freeze existing predictions/golden for isolated judges without extraction."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from eval import run


def freeze(inputs: Path, prediction_run: Path):
    result = {"source_text_reviewed": False, "independent_human_review": False,
              "cases": {}, "sources": {}}
    paths = sorted(prediction_run.glob("*/*/backend_extract.json"))
    if not paths:
        raise ValueError("no saved predictions; do not fall back to extraction")
    for path in paths:
        relative = path.parent.relative_to(prediction_run)
        golden_path = inputs / relative / "extract/golden.json"
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        envelope = json.loads(path.read_text(encoding="utf-8"))
        data = envelope.get("data")
        extraction = data.get("extraction") if isinstance(data, dict) else None
        graph = data.get("graph") if isinstance(data, dict) else None
        if graph is None and isinstance(extraction, dict):
            graph = extraction.get("graph")
        if not isinstance(graph, dict) or not graph:
            raise ValueError(f"saved prediction has no graph: {relative}")
        gold = run.normalize_graph(golden)
        predicted = run.normalize_graph(graph)
        gold_nodes = golden.get("nodes", golden.get("entities", []))
        gold_relations = golden.get("relationships", golden.get("relations", []))
        if len(gold[0]) != len(gold_nodes) or len(gold[1]) != len(gold_relations):
            raise ValueError(f"golden normalization would silently discard malformed rows: {relative}")
        raw_nodes = graph.get("nodes", graph.get("entities", []))
        raw_relations = graph.get("relationships", graph.get("relations", []))
        if len(predicted[0]) != len(raw_nodes) or len(predicted[1]) != len(raw_relations):
            raise ValueError(f"normalization would silently discard malformed rows: {relative}")
        name = "__".join(relative.parts)
        if name in result["cases"]:
            raise ValueError("case key collision")
        result["cases"][name] = {
            collection: {"golden": gold[index], "predicted": predicted[index]}
            for index, collection in enumerate(("entities", "relations"))}
        scores_path = path.parent / "scores.json"
        status = "unknown"
        if scores_path.exists():
            scores = json.loads(scores_path.read_text(encoding="utf-8"))
            status = scores.get("backend", {}).get("modules", {}).get("extract", {}).get("status", "unknown")
        result["sources"][name] = {"case_path": relative.as_posix(), "extraction_status": status,
            "golden_sha256": hashlib.sha256(golden_path.read_bytes()).hexdigest(),
            "prediction_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--prediction-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-cases", type=int, default=5)
    args = parser.parse_args()
    data = freeze(args.inputs, args.prediction_run)
    if len(data["cases"]) != args.expected_cases:
        raise ValueError("saved case coverage differs from expected; wait for completion or explicitly change expected count")
    if args.output.exists():
        raise FileExistsError("frozen artifact already exists")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
