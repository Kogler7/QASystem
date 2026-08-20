"""Phase 1 Step 6 验收：OfficialDocument 公文策略（§12）。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md §37 Step 6：
DocumentUnit / OperationalAgent / OperationalObject / OperationalActivity /
OperationalInformation / OperationalExchange / OperationalConstraint /
EVIDENCED_BY。

做法（无需 openai / pytest）：
- official-document-01：gold 桩跑 extract_split（document_type=official_document），
  校验实体/关系与 gold 精确一致；审计不对已 EVIDENCED_BY 的实体误报；
- official-document-02：校验 gold operational 实体/关系子集全部命中；
- 打印确定性元数据、DocumentUnit 数量与 EVIDENCED_BY 覆盖统计。

用法：
    python algorithm/knowledge_extract_v2/benchmark/step6.py
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
            "coverage.py",
            "standard_strategy.py",
            "official_document_strategy.py",
            "split_extractor.py",
        ],
    )
    split_extractor = modules["split_extractor"]
    official = modules["official_document_strategy"]
    canonical = modules["graph_utils"].canonical_name
    ontology = (ONTOLOGIES_DIR / "official_document_ontology.puml").read_text(encoding="utf-8")

    def make_stub(expected: dict):
        def call(prompt: str) -> str:
            if "实体候选提议" in prompt:
                payload = {"entities": expected["entities"]}
            elif "关系候选提议" in prompt:
                index = {}
                for match in re.finditer(r"\|\s*`(entity-\d+)`\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|", prompt):
                    index[(match.group(2), canonical(match.group(3).strip()))] = match.group(1)
                relations = []
                for relation in expected.get("relations", []):
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
                payload = {"relations": relations}
            else:
                raise AssertionError("无法识别的 prompt 段落")
            return json.dumps(payload, ensure_ascii=False)

        return call

    failures: list[str] = []
    for case_id, exact in (("official-document-01", True), ("official-document-02", False)):
        case = next(item for item in eval_mod.load_dataset(CASES_DIR) if item["id"] == case_id)
        expected = case["expected"]
        result = split_extractor.extract_split(
            ontology,
            text=case["text"],
            llm=make_stub(expected),
            document_type="official_document",
        )
        gold_entity_keys = {(e["type"], canonical(e["name"])) for e in expected["entities"]}
        pred_entity_keys = {(e["type"], canonical(e["name"])) for e in result["raw"]["entities"]}
        gold_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in expected["relations"]
        }
        pred_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in result["raw"]["relations"]
        }
        operational_types = set(official.OPERATIONAL_TYPES)
        operational = [e for e in result["raw"]["entities"] if e["type"] in operational_types]
        evidenced = sum(
            1
            for entity in operational
            if any(
                canonical(r["from_name"]) == canonical(entity["name"])
                and r["rel_type"] == "EVIDENCED_BY"
                and r["to_type"] == "DocumentUnit"
                for r in result["raw"]["relations"]
            )
        )
        covered_keys = {
            (r["from_type"], canonical(r["from_name"]))
            for r in expected["relations"]
            if r["rel_type"] == "EVIDENCED_BY"
        }
        gap_entities = {
            (gap["target_type"], canonical(gap["evidence"]))
            for gap in result["coverage_gaps"]
            if gap["gap_type"] == "evidence_coverage"
        }
        structural = {gap["gap_type"] for gap in result["coverage_gaps"]} & {
            "structural_coverage"
        }
        print(
            f"{case_id}：entities {len(pred_entity_keys)}/{len(gold_entity_keys)}，"
            f"relations {len(pred_relation_keys)}/{len(gold_relation_keys)}，"
            f"operational {len(operational)}（EVIDENCED_BY {evidenced}），"
            f"structural_gaps={structural or '无'}"
        )

        if exact:
            if pred_entity_keys != gold_entity_keys:
                failures.append(f"[{case_id}] 实体集合不一致")
            if pred_relation_keys != gold_relation_keys:
                failures.append(f"[{case_id}] 关系集合不一致")
        else:
            gold_op_keys = {
                (e["type"], canonical(e["name"]))
                for e in expected["entities"]
                if e["type"] in operational_types
            }
            if not gold_op_keys.issubset(pred_entity_keys):
                failures.append(f"[{case_id}] gold operational 实体未全部命中")
        if structural:
            failures.append(f"[{case_id}] 出现结构性覆盖 gap")
        if gap_entities & covered_keys:
            failures.append(f"[{case_id}] 审计对已 EVIDENCED_BY 的实体误报")
        if not any(e["type"] == "DocumentUnit" for e in result["raw"]["entities"]):
            failures.append(f"[{case_id}] 未建立 DocumentUnit")

    if failures:
        print("\nStep 6 验收未通过：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nStep 6 验收通过：确定性元数据/DocumentUnit 抽取与 gold 对齐，EVIDENCED_BY 审计无误报。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
