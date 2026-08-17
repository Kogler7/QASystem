"""Phase 1 Step 8 验收：Mapping Scorer（§17-§19）。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md §37 Step 8：
实现可配置的 attribute + relation + context 评分。

做法（无需 openai / pytest）：
- standard-01 / composite-rule-01 用 gold 桩跑 extract_split（默认注释模式）：
  校验评分不改变抽取结果（raw 与 gold 一致），mapping 摘要含逐候选分数、
  平均分、低置信/拒绝计数、权重与阈值；
- 打印代表性候选的分数分解，确认三维分量与状态判定可见。

用法：
    python algorithm/knowledge_extract_v2/benchmark/step8.py
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
            "composite.py",
            "mapping_scorer.py",
            "standard_strategy.py",
            "split_extractor.py",
        ],
    )
    split_extractor = modules["split_extractor"]
    canonical = modules["graph_utils"].canonical_name

    failures: list[str] = []
    for case_id in ("standard-01", "composite-rule-01"):
        case = next(item for item in eval_mod.load_dataset(CASES_DIR) if item["id"] == case_id)
        expected = case["expected"]
        ontology = (ONTOLOGIES_DIR / case["ontology_file"]).read_text(encoding="utf-8")

        def call(prompt: str) -> str:
            if "实体候选提议" in prompt:
                payload = {"entities": expected["entities"]}
            elif "关系候选提议" in prompt:
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

        result = split_extractor.extract_split(
            ontology,
            text=case["text"],
            llm=call,
            document_type=case.get("document_type", "auto"),
            strategy=("generic" if case.get("category") == "composite_rule" else None),
        )
        gold_entity_keys = {(e["type"], canonical(e["name"])) for e in expected["entities"]}
        pred_entity_keys = {(e["type"], canonical(e["name"])) for e in result["raw"]["entities"]}
        gold_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in expected.get("relations", [])
        }
        pred_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in result["raw"]["relations"]
        }
        mapping = result["mapping"]
        sample = mapping["entities"][0] if mapping["entities"] else {}
        print(
            f"{case_id}：entities {len(pred_entity_keys)}/{len(gold_entity_keys)}，"
            f"relations {len(pred_relation_keys)}/{len(gold_relation_keys)}，"
            f"avg_score={mapping['average_entity_score']}，"
            f"low={mapping['low_confidence']}，rejected={mapping['rejected']}，"
            f"weights={mapping['weights']}"
        )
        if sample:
            print(f"  示例：{sample['type']}::{sample['name']} score={sample['score']} "
                  f"status={sample['status']} breakdown={sample['breakdown']}")
        if pred_entity_keys != gold_entity_keys:
            failures.append(f"[{case_id}] 评分后实体集合与 gold 不一致（注释模式应保持不变）")
        if pred_relation_keys != gold_relation_keys:
            failures.append(f"[{case_id}] 评分后关系集合与 gold 不一致")
        if mapping["average_entity_score"] is None:
            failures.append(f"[{case_id}] 缺少实体平均分")

    if failures:
        print("\nStep 8 验收未通过：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nStep 8 验收通过：注释模式评分不改变抽取结果，mapping 摘要与分数分解完整。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
