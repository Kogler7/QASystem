"""Phase 1 Step 2 验收：编译 benchmark 本体，输出 ExtractionPlan + Schema QA。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md §37 Step 2
（Ontology Compiler + TargetScope：class hierarchy / properties / relations /
target expansion / schema QA；不改 LLM 流程）。

用法（无需 openai / pytest）：
    python algorithm/knowledge_extract_v2/benchmark/step2.py
    python algorithm/knowledge_extract_v2/benchmark/step2.py --strict

``--strict`` 在出现 error 级 Schema QA 时以非零码退出（默认只报告）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


BENCHMARK_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = BENCHMARK_DIR.parent
ONTOLOGIES_DIR = BENCHMARK_DIR / "ontologies"
CASES_DIR = BENCHMARK_DIR / "cases"


def _load_eval_module() -> Any:
    spec = importlib.util.spec_from_file_location("_step1_eval", BENCHMARK_DIR / "eval.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _cases_by_ontology(eval_mod: Any) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for case in eval_mod.load_dataset(CASES_DIR):
        grouped.setdefault(case["ontology_file"], []).append(case)
    return grouped


def _render_warnings(warnings: list[Any]) -> str:
    if not warnings:
        return "无"
    counts: Counter[str] = Counter(warning.code for warning in warnings)
    severity_counts = Counter(warning.severity for warning in warnings)
    detail = "; ".join(f"{code} x{count}" for code, count in sorted(counts.items()))
    severities = "; ".join(f"{severity} {count}" for severity, count in sorted(severity_counts.items()))
    return f"{severities}（{detail}）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1 Step 2 Ontology Compiler 验收")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="存在 error 级 Schema QA 时以非零码退出",
    )
    args = parser.parse_args(argv)

    eval_mod = _load_eval_module()
    modules = eval_mod.load_package_modules(
        "knowledge_extract_v2",
        PACKAGE_DIR,
        ["graph_utils.py", "ontology_schema.py", "ontology_compiler.py"],
    )
    compiler = modules["ontology_compiler"]
    cases_by_ontology = _cases_by_ontology(eval_mod)

    error_count = 0
    for ontology_path in sorted(ONTOLOGIES_DIR.glob("*.puml")):
        source = ontology_path.read_text(encoding="utf-8")
        related_cases = cases_by_ontology.get(ontology_path.name, [])
        target_types = related_cases[0].get("target_types") or [] if related_cases else []
        plan = compiler.compile_plan(source, target_types=target_types)
        summary = plan.to_dict()
        print(f"\n=== {ontology_path.name}（用例：{', '.join(c['id'] for c in related_cases) or '无'}）===")
        print(f"ontology_id: {summary['ontology_id']}")
        print(f"classes: {summary['classes']} | relations: {summary['relations']} | relation_ids: {', '.join(summary['relation_ids'])}")
        print(f"target_scope: {json.dumps(summary['target_scope'], ensure_ascii=False)}")
        print(f"schema_warnings: {_render_warnings(plan.schema_warnings)}")
        for warning in plan.schema_warnings:
            print(f"  [{warning.severity}] {warning.code}: {warning.message}（{warning.path}）")
            if warning.severity == "error":
                error_count += 1

    print(f"\nStep 2 验收完成：{len(list(ONTOLOGIES_DIR.glob('*.puml')))} 个本体编译成功，error 级 QA {error_count} 个。")
    return 1 if (args.strict and error_count) else 0


if __name__ == "__main__":
    raise SystemExit(main())
