"""Phase 1 Step 9 验收：通用 Coverage Auditor（§22-§23 / AC-8）。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md §37 Step 9：
只发现 gap，不自动修复。

做法（无需 openai / pytest）：
- standard-01 全 gold：通用审计（feature/relation/evidence/structural/
  composite）+ 策略审计均不误报结构性/约束/行动缺失；
- standard-01 空 LLM：AC-8 类别齐全（constraint/action/reference +
  feature/relation/evidence/structural）；
- composite-rule-01 自动组装后：无 composite_membership gap。

用法：
    python algorithm/knowledge_extract_v2/benchmark/step9.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any


BENCHMARK_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = BENCHMARK_DIR.parent
CASES_DIR = BENCHMARK_DIR / "cases"
ONTOLOGIES_DIR = BENCHMARK_DIR / "ontologies"


def _load_eval_module() -> Any:
    spec = importlib.util.spec_from_file_location("_step1_eval", BENCHMARK_DIR / "eval.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def structure_chunks(case: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in (case["text"] or "").splitlines():
        if not line.strip():
            continue
        match = re.match(r"^\s*(\d+(?:\.\d+){0,3})\s+(.+?)\s*$", line)
        heading = bool(match) and not re.fullmatch(r"[\d.\s]+", match.group(2))
        if heading:
            if current is not None:
                chunks.append(current)
            number, title = match.group(1), match.group(2)
            current = {
                "chunk_id": f"{case['id']}-{number}",
                "text": line,
                "document_id": case["id"],
                "clause_number": number,
                "section_title": title,
                "previous_chunk_id": chunks[-1]["chunk_id"] if chunks else None,
            }
        else:
            if current is None:
                current = {"chunk_id": f"{case['id']}-preamble", "text": "", "document_id": case["id"]}
            current["text"] = (current["text"] + "\n" if current["text"] else "") + line
    if current is not None:
        chunks.append(current)
    return chunks


def main(argv: list[str] | None = None) -> int:
    eval_mod = _load_eval_module()
    modules = eval_mod.load_package_modules(
        "knowledge_extract_v2",
        PACKAGE_DIR,
        [
            "config.py",
            "scheduler.py",
            "graph_utils.py",
            "ontology_schema.py",
            "ontology_compiler.py",
            "chunk_context.py",
            "evidence.py",
            "coverage.py",
            "composite.py",
            "standard_strategy.py",
            "split_extractor.py",
        ],
    )
    split_extractor = modules["split_extractor"]
    canonical = modules["graph_utils"].canonical_name
    failures: list[str] = []

    standard = next(item for item in eval_mod.load_dataset(CASES_DIR) if item["id"] == "standard-01")
    standard_ontology = (ONTOLOGIES_DIR / standard["ontology_file"]).read_text(encoding="utf-8")
    standard_gold = standard["expected"]

    def gold_stub(expected: dict, with_relations: bool):
        def call(prompt: str) -> str:
            if "实体候选提议" in prompt:
                payload = {"entities": expected["entities"]}
            elif "关系候选提议" in prompt:
                if not with_relations:
                    return json.dumps({"relations": []}, ensure_ascii=False)
                index = {}
                for match in re.finditer(r"\|\s*`(entity-\d+)`\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|", prompt):
                    index[(match.group(2), canonical(match.group(3).strip()))] = match.group(1)
                mapped = []
                for relation in expected.get("relations", []):
                    from_id = index.get((relation["from_type"], canonical(relation["from_name"])))
                    to_id = index.get((relation["to_type"], canonical(relation["to_name"])))
                    if from_id is None or to_id is None:
                        continue
                    mapped.append(
                        {
                            "from_id": from_id,
                            "rel_type": relation["rel_type"],
                            "to_id": to_id,
                            "evidence": {"quote": (relation.get("evidence") or {}).get("quote") or ""},
                        }
                    )
                payload = {"relations": mapped}
            else:
                raise AssertionError("无法识别的 prompt 段落")
            return json.dumps(payload, ensure_ascii=False)

        return call

    gold_result = split_extractor.extract_split(
        standard_ontology,
        chunks=[
            modules["chunk_context"].ExtractionChunk.from_dict(payload)
            for payload in structure_chunks(standard)
        ],
        llm=gold_stub(standard_gold, with_relations=True),
        document_type="standard",
    )
    gold_gap_types = {gap["gap_type"] for gap in gold_result["coverage_gaps"]}
    print(f"standard-01 全 gold：gap 类型 {sorted(gold_gap_types) or '无'}")
    for forbidden in ("structural_coverage", "constraint_coverage", "action_coverage", "composite_membership"):
        if forbidden in gold_gap_types:
            failures.append(f"[standard-01 gold] 误报 {forbidden} gap")
    if "evidence_missing" not in gold_gap_types:
        failures.append("[standard-01 gold] 未检测到关系 evidence 缺失（gold 关系多无 quote）")

    empty_result = split_extractor.extract_split(
        standard_ontology,
        chunks=[
            modules["chunk_context"].ExtractionChunk.from_dict(payload)
            for payload in structure_chunks(standard)
        ],
        llm=gold_stub({"entities": [], "relations": []}, True),
        document_type="standard",
    )
    empty_gap_types = {gap["gap_type"] for gap in empty_result["coverage_gaps"]}
    print(f"standard-01 空 LLM：gap 类型 {sorted(empty_gap_types)}")
    for required in (
        "constraint_coverage",
        "action_coverage",
        "reference_coverage",
        "feature_coverage",
        "relation_coverage",
    ):
        if required not in empty_gap_types:
            failures.append(f"[standard-01 空 LLM] 未检测到 {required} gap")

    composite = next(item for item in eval_mod.load_dataset(CASES_DIR) if item["id"] == "composite-rule-01")
    composite_ontology = (ONTOLOGIES_DIR / composite["ontology_file"]).read_text(encoding="utf-8")
    composite_result = split_extractor.extract_split(
        composite_ontology,
        text=composite["text"],
        llm=gold_stub(composite["expected"], with_relations=False),
        document_type=composite.get("document_type", "general"),
    )
    composite_gap_types = {gap["gap_type"] for gap in composite_result["coverage_gaps"]}
    print(f"composite-rule-01 自动组装：gap 类型 {sorted(composite_gap_types) or '无'}")
    if "composite_membership" in composite_gap_types:
        failures.append("[composite-rule-01] 自动组装后仍报告复合成员缺失")

    if failures:
        print("\nStep 9 验收未通过：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nStep 9 验收通过：通用覆盖审计只报告真实 gap，AC-8 类别齐全，gold 无漏报/误报。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
