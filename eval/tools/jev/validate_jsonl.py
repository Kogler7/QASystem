"""Validate JEV training JSONL without loading a model.

Usage:
    python3 eval/tools/jev/validate_jsonl.py path/to/records.jsonl
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from algorithm.jev.contract import validate_record


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: python3 eval/tools/jev/validate_jsonl.py records.jsonl", file=sys.stderr)
        return 2
    path = Path(argv[1])
    errors = 0
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            validate_record(json.loads(line))
        except (ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
            errors += 1
            print(f"{path}:{line_number}: {exc}", file=sys.stderr)
    if errors:
        print(f"无效记录 {errors} 条", file=sys.stderr)
        return 1
    print(f"通过: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
