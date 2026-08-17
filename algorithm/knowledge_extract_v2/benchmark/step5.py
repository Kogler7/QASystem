"""Phase 1 Step 5 验收：Standard Strategy（确定性抽取 + 覆盖审计）。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md §37 Step 5：
structure / metadata / constraint / action / reference coverage。

做法（无需 openai / pytest）：
- 对 standard-01 用例按标题行构造携带结构元数据的 chunk（模拟上游切片）；
- gold 桩驱动两段式 LLM，跑 extract_split（document_type=standard）：
  校验 raw 与 gold 一致，且无 structural / constraint / action 覆盖 gap；
- 再以空 LLM 跑一遍，展示覆盖审计能发现 constraint / action / reference /
  characteristic gap（只报告、不修复，§22/§23.2）。

用法：
    python algorithm/knowledge_extract_v2/benchmark/step5.py
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
            "standard_strategy.py",
            "split_extractor.py",
        ],
    )
    split_extractor = modules["split_extractor"]
    chunk_context = modules["chunk_context"]
    canonical = modules["graph_utils"].canonical_name

    case = next(
        item
        for item in eval_mod.load_dataset(CASES_DIR)
        if item["id"] == "standard-01"
    )
    ontology = (ONTOLOGIES_DIR / case["ontology_file"]).read_text(encoding="utf-8")
    gold = case["expected"]
    chunks = [chunk_context.ExtractionChunk.from_dict(payload) for payload in structure_chunks(case)]

    def make_stub(use_gold: bool):
        def entity_handler(prompt: str) -> dict[str, Any]:
            return {"entities": gold["entities"]} if use_gold else {"entities": []}

        def relation_handler(prompt: str) -> dict[str, Any]:
            index = {}
            for match in re.finditer(r"\|\s*`(entity-\d+)`\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|", prompt):
                index[(match.group(2), canonical(match.group(3).strip()))] = match.group(1)
            relations = []
            for relation in gold.get("relations", []):
                from_id = index.get((relation["from_type"], canonical(relation["from_name"])))
                to_id = index.get((relation["to_type"], canonical(relation["to_name"])))
                if from_id is None or to_id is None:
                    continue
                relations.append(
                    {
                        "from_id": from_id,
                        "rel_type": relation["rel_type"],
                        "to_id": to_id,
                        "evidence": {"quote": (relation.get("evidence") or {}).get("quote") or ""},
                    }
                )
            return {"relations": relations}

        def call(prompt: str) -> str:
            if "实体候选提议" in prompt:
                payload = entity_handler(prompt)
            elif "关系候选提议" in prompt:
                payload = relation_handler(prompt)
            else:
                raise AssertionError("无法识别的 prompt 段落")
            return json.dumps(payload, ensure_ascii=False)

        return call

    gold_result = split_extractor.extract_split(
        ontology,
        chunks=chunks,
        llm=make_stub(use_gold=True),
        document_type="standard",
    )
    gold_entity_keys = {(e["type"], canonical(e["name"])) for e in gold["entities"]}
    pred_entity_keys = {(e["type"], canonical(e["name"])) for e in gold_result["raw"]["entities"]}
    gold_relation_keys = {
        (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
        for r in gold["relations"]
    }
    pred_relation_keys = {
        (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
        for r in gold_result["raw"]["relations"]
    }
    gap_types = {gap["gap_type"] for gap in gold_result["coverage_gaps"]}
    print(f"standard-01（gold 桩）：entities {len(pred_entity_keys)}/{len(gold_entity_keys)}，"
          f"relations {len(pred_relation_keys)}/{len(gold_relation_keys)}，"
          f"coverage_gaps={gap_types or '无'}")
    print(f"  Standard 确定性元数据：{json.dumps([e['properties'] for e in gold_result['raw']['entities'] if e['type'] == 'Standard'][0], ensure_ascii=False)}")
    print(f"  结构关系：{sum(1 for r in gold_result['raw']['relations'] if r['rel_type'] in {'hasStructuralElement', 'hasSubClause'})} 条")

    failures: list[str] = []
    if pred_entity_keys != gold_entity_keys:
        failures.append(f"实体集合不一致: gold={len(gold_entity_keys)} pred={len(pred_entity_keys)}")
    if pred_relation_keys != gold_relation_keys:
        failures.append(f"关系集合不一致: gold={len(gold_relation_keys)} pred={len(pred_relation_keys)}")
    for forbidden in ("structural_coverage", "constraint_coverage", "action_coverage"):
        if forbidden in gap_types:
            failures.append(f"gold 全覆盖场景仍报告 {forbidden} gap")

    empty_result = split_extractor.extract_split(
        ontology,
        chunks=chunks,
        llm=make_stub(use_gold=False),
        document_type="standard",
    )
    empty_gap_types = {gap["gap_type"] for gap in empty_result["coverage_gaps"]}
    print(f"standard-01（空 LLM）：检测到覆盖 gap {empty_gap_types}")
    for required in ("constraint_coverage", "action_coverage"):
        if required not in empty_gap_types:
            failures.append(f"空 LLM 场景未检测到 {required} gap")

    if failures:
        print("\nStep 5 验收未通过：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nStep 5 验收通过：确定性元数据/结构抽取与 gold 一致，覆盖审计可发现/不误报。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
