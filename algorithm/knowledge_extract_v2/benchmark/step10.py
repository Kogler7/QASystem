"""Phase 1 Step 10 验收：GraphAssembler + Compat（§26-§28）。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md §37 Step 10：
GraphAssembler + Compat，保留现有 API。

做法（无需 openai / pytest）：
- 对 7 个 benchmark 用例用 gold 桩跑 extract_split（strategy=generic，
  两段式机制 + 图组装），校验：
  - 返回契约：保留 raw/graph/warnings/attempts/profile/error_code，
    附带 triples/intermediate_model/evidence_coverage/coverage_gaps/mapping；
  - raw 与 gold 一致（实体/关系键集合）；
  - graph：无悬空/重复 uid；node uid 可由 (type, name) 复算（§27）；
  - triples 与 relationships 一一对应；
  - 无 UNKNOWN_* / 悬空类 schema 告警（AC-1 / AC-7）。

用法：
    python algorithm/knowledge_extract_v2/benchmark/step10.py
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
            "composite.py",
            "graph_assembler.py",
            "split_extractor.py",
        ],
    )
    split_extractor = modules["split_extractor"]
    assembler = modules["graph_assembler"].GraphAssembler()
    canonical = modules["graph_utils"].canonical_name
    entity_uid = modules["graph_utils"].entity_uid

    LEGACY_KEYS = ("raw", "graph", "warnings", "attempts", "profile", "error_code")
    NEW_KEYS = ("triples", "intermediate_model", "evidence_coverage", "coverage_gaps", "mapping")
    UNKNOWN_CODES = {
        "UNKNOWN_ENTITY_TYPE",
        "UNKNOWN_PROPERTY",
        "UNKNOWN_RELATION_ENDPOINT_TYPE",
        "UNKNOWN_RELATION_TYPE",
        "UNKNOWN_RELATION_ENDPOINT_ID",
        "RELATION_ENDPOINT_TYPE_MISMATCH",
        "ENTITY_OUT_OF_SCOPE",
    }

    failures: list[str] = []
    for case in eval_mod.load_dataset(CASES_DIR):
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
            strategy="generic",
        )
        missing_legacy = [key for key in LEGACY_KEYS if key not in result]
        missing_new = [key for key in NEW_KEYS if key not in result]
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
        graph = result["graph"]
        validate_issues = assembler.validate(graph)
        node_uids = {node["uid"] for node in graph["nodes"]}
        uid_mismatch = [
            entity["name"]
            for entity in result["raw"]["entities"]
            if entity_uid(entity["type"], entity["name"], None) not in node_uids
        ]
        triples_ok = {
            (t["head"], t["relation"], t["tail"]) for t in result["triples"]
        } == {
            (r["from_uid"], r["type"], r["to_uid"]) for r in graph["relationships"]
        }
        warning_codes = {w.get("code") for w in result["warnings"]}
        unknown_hits = sorted(warning_codes & UNKNOWN_CODES)
        print(
            f"{case['id']}：entities {len(pred_entity_keys)}/{len(gold_entity_keys)}，"
            f"relations {len(pred_relation_keys)}/{len(gold_relation_keys)}，"
            f"nodes {len(graph['nodes'])}，rels {len(graph['relationships'])}，"
            f"triples_ok={triples_ok}"
        )
        if missing_legacy or missing_new:
            failures.append(f"[{case['id']}] 契约键缺失 legacy={missing_legacy} new={missing_new}")
        if pred_entity_keys != gold_entity_keys:
            failures.append(f"[{case['id']}] 实体集合与 gold 不一致")
        if pred_relation_keys != gold_relation_keys:
            failures.append(f"[{case['id']}] 关系集合与 gold 不一致")
        if validate_issues:
            failures.append(f"[{case['id']}] 图结构问题 {validate_issues}")
        if uid_mismatch:
            failures.append(f"[{case['id']}] uid 不可复算: {uid_mismatch}")
        if not triples_ok:
            failures.append(f"[{case['id']}] triples 与 relationships 不一致")
        if unknown_hits:
            failures.append(f"[{case['id']}] 出现 schema 告警 {unknown_hits}")

    if failures:
        print("\nStep 10 验收未通过：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nStep 10 验收通过：7 个用例图组装与 gold 一致，契约保留现有 API，UID/triples/无悬空校验通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
