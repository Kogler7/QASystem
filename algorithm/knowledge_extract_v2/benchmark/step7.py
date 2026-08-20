"""Phase 1 Step 7 验收：复合元素（CompositeRule 示例，§21 / AC-6）。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md §37 Step 7：
优先满足需求文档中的 CompositeRule 示例——member extraction、membership
relation、combination type 全部正确；不得把复合规则只保存成一段字符串。

做法（无需 openai / pytest）：
- composite-rule-01 用 gold 实体做 LLM 桩、关系 pass 返回空：校验
  CompositeElementHandler 能按 triggerCondition + action 支撑自动补建
  hasSubRule，关系集合与 gold 一致，combinationType=AND 合法；
- 全 gold（实体+关系）跑一遍：校验与 gold 精确一致、无误报。

用法：
    python algorithm/knowledge_extract_v2/benchmark/step7.py
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
            "split_extractor.py",
        ],
    )
    split_extractor = modules["split_extractor"]
    canonical = modules["graph_utils"].canonical_name
    case = next(item for item in eval_mod.load_dataset(CASES_DIR) if item["id"] == "composite-rule-01")
    ontology = (ONTOLOGIES_DIR / case["ontology_file"]).read_text(encoding="utf-8")
    expected = case["expected"]

    def make_stub(with_relations: bool):
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
                for relation in expected["relations"]:
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

    gold_relation_keys = {
        (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
        for r in expected["relations"]
    }
    failures: list[str] = []
    for label, with_relations, expected_auto in (
        ("自动组装（关系 pass 空）", False, 2),
        ("全 gold（关系 pass 完整）", True, 0),
    ):
        result = split_extractor.extract_split(
            ontology,
            text=case["text"],
            llm=make_stub(with_relations),
            document_type=case.get("document_type", "general"),
        )
        pred_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in result["raw"]["relations"]
        }
        auto = result["profile"]["composite_auto_memberships"]
        composite_entity = next(e for e in result["raw"]["entities"] if e["type"] == "CompositeRule")
        print(
            f"{label}：hasSubRule {len(pred_relation_keys & gold_relation_keys)}/{len(gold_relation_keys)}，"
            f"auto_memberships={auto}，combinationType={composite_entity['properties'].get('combinationType')}"
        )
        if pred_relation_keys != gold_relation_keys:
            failures.append(f"[{label}] 关系集合与 gold 不一致")
        if auto != expected_auto:
            failures.append(f"[{label}] 自动成员关系数量 {auto} != {expected_auto}")
        composite_warnings = {
            w["code"]
            for w in result["warnings"]
            if w["code"] in {"INSUFFICIENT_COMPOSITE_MEMBERS", "MISSING_COMBINATION_TYPE", "INVALID_COMBINATION_TYPE"}
        }
        if composite_warnings:
            failures.append(f"[{label}] 出现复合校验告警 {composite_warnings}")

    if failures:
        print("\nStep 7 验收未通过：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nStep 7 验收通过：CompositeRule 成员关系自动组装与 gold 一致，combinationType 校验通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
