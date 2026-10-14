"""Export a saved V3.1 aggregate without model calls or API contract changes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from algorithm.knowledge_extract.v3_1 import export_artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--calls", type=Path, help="Optional original transport captures with assistant responses")
    parser.add_argument("--output", type=Path, required=True, help="New directory; existing paths are not overwritten")
    args = parser.parse_args()
    result = json.loads(args.result.read_text(encoding="utf-8"))
    calls = json.loads(args.calls.read_text(encoding="utf-8")) if args.calls else None
    paths = export_artifacts(result, args.output, calls=calls)
    print(json.dumps(paths, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
