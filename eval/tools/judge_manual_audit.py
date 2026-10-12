"""Build reproducible review worksheets from saved graphs; never reads source text."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from eval.run import normalize_graph, _simple_entities, _simple_relations


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def build_case(golden_path: Path, prediction_path: Path, details_paths: dict[str, Path]):
    golden = normalize_graph(load(golden_path))
    envelope = load(prediction_path)
    prediction = normalize_graph(envelope.get("data", {}).get("graph", {}))
    result = {
        "scope": "agent review of prediction versus golden; source text not reviewed",
        "independent_human_review": False,
        "hashes": {"golden": hashlib.sha256(golden_path.read_bytes()).hexdigest(),
                   "prediction": hashlib.sha256(prediction_path.read_bytes()).hexdigest()},
        "collections": {}, "judges": {},
    }
    for index, name in enumerate(("entities", "relations")):
        simplify = _simple_entities if index == 0 else _simple_relations
        result["collections"][name] = {"golden": simplify(golden[index]), "predicted": simplify(prediction[index])}
    for name, path in details_paths.items():
        if path.is_file():
            result["judges"][name] = load(path)["backend_extract"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--api-run", type=Path, required=True)
    parser.add_argument("--clef-run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for path in sorted(args.api_run.glob("*/*/backend_extract.json")):
        case = path.parent.relative_to(args.api_run)
        judges = {"api": path.parent / "match_details.json"}
        if args.clef_run:
            judges["clef"] = args.clef_run / case / "match_details.json"
        worksheet = build_case(args.inputs / case / "extract/golden.json", path, judges)
        destination = args.output / ("__".join(case.parts) + ".json")
        destination.write_text(json.dumps(worksheet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
