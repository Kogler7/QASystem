"""Phase 1 Step 4 验收：Entity / Relation 拆分（Two-Pass）流程验证。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md §37 Step 4：
``entity candidate -> resolve -> relation candidate``；端点用稳定 candidate id，
不再依赖自由字符串 name 对齐（§15.2）；关系签名/端点存在性做结构校验（§19、AC-7）。

做法（无需 openai / pytest）：
- 用 gold 构造两段式 LLM 桩：实体 pass 返回 gold 实体；关系 pass 解析 prompt 中
  的实体目录 id，把 gold 关系映射为 from_id / to_id 再返回；
- 对 7 个 benchmark 用例跑完整 extract_split，校验：
  raw 实体/关系集合与 gold 一致、无范围外实体、无悬空关系、证据覆盖率 100%。
  说明：显式使用 strategy="generic" 只验证两段式候选机制本身；
  standard / official_document 策略行为由 step5.py / step6.py 单独验收。

用法：
    python algorithm/knowledge_extract_v2/benchmark/step4.py
"""
from __future__ import annotations

import copy
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


def _paragraph_chunks(case: dict[str, Any]) -> list[dict[str, Any]]:
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", case.get("text") or "")
        if paragraph.strip()
    ]
    return [
        {
            "chunk_id": f"{case['id']}-p{index}",
            "text": text,
            "document_id": case["id"],
            "previous_chunk_id": f"{case['id']}-p{index - 1}" if index > 0 else None,
        }
        for index, text in enumerate(paragraphs)
    ]


def _catalog_index(prompt: str, canonical) -> dict[tuple[str, str], str]:
    """从关系 pass prompt 的目录表解析 (type, canonical_name) -> id。"""
    index: dict[tuple[str, str], str] = {}
    for match in re.finditer(r"\|\s*`(entity-\d+)`\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|", prompt):
        index[(match.group(2), canonical(match.group(3).strip()))] = match.group(1)
    return index


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
            "split_extractor.py",
        ],
    )
    split_extractor = modules["split_extractor"]
    chunk_context = modules["chunk_context"]
    canonical = modules["graph_utils"].canonical_name

    cases = eval_mod.load_dataset(CASES_DIR)
    failures: list[str] = []

    for case in cases:
        gold = copy.deepcopy(case.get("expected") or {"entities": [], "relations": []})
        ontology = (ONTOLOGIES_DIR / case["ontology_file"]).read_text(encoding="utf-8")

        def entity_handler(prompt: str) -> dict[str, Any]:
            return {"entities": gold["entities"]}

        def relation_handler(prompt: str) -> dict[str, Any]:
            index = _catalog_index(prompt, canonical)
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

        result = split_extractor.extract_split(
            ontology,
            chunks=[
                chunk_context.ExtractionChunk.from_dict(payload)
                for payload in _paragraph_chunks(case)
            ],
            llm=call,
            strategy="generic",
            target_types=case.get("target_types") or None,
            include_subclasses=case.get("include_subclasses", True),
            document_type=case.get("document_type", "auto"),
            validation_mode=case.get("validation_mode", "strict"),
        )

        raw = result["raw"]
        gold_entity_keys = {(e["type"], canonical(e["name"])) for e in gold["entities"]}
        pred_entity_keys = {(e["type"], canonical(e["name"])) for e in raw["entities"]}
        gold_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in gold.get("relations", [])
        }
        pred_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in raw["relations"]
        }
        warning_codes = [w.get("code") for w in result["warnings"]]
        entity_rate = result["evidence_coverage"]["entities"]["rate"]

        if pred_entity_keys != gold_entity_keys:
            failures.append(f"[{case['id']}] 实体集合不一致: gold={len(gold_entity_keys)} pred={len(pred_entity_keys)}")
        if pred_relation_keys != gold_relation_keys:
            failures.append(
                f"[{case['id']}] 关系集合不一致: gold={len(gold_relation_keys)} pred={len(pred_relation_keys)}"
            )
        if any(code == "ENTITY_OUT_OF_SCOPE" for code in warning_codes):
            failures.append(f"[{case['id']}] 出现范围外实体（AC-2 失败）")
        if any(code in {"UNKNOWN_RELATION_ENDPOINT_ID", "RELATION_ENDPOINT_TYPE_MISMATCH"} for code in warning_codes):
            failures.append(f"[{case['id']}] 出现悬空/越界关系（AC-7 失败）")
        if entity_rate < 1.0:
            failures.append(f"[{case['id']}] 实体证据覆盖率 {entity_rate:.0%} < 100%")

        print(
            f"{case['id']}: entities {len(pred_entity_keys)}/{len(gold_entity_keys)}, "
            f"relations {len(pred_relation_keys)}/{len(gold_relation_keys)}, "
            f"evidence {entity_rate:.0%}, graph {len(result['graph']['nodes'])} nodes / "
            f"{len(result['graph']['relationships'])} rels"
        )

    if failures:
        print(f"\nStep 4 验收未通过（{len(failures)} 项）：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"\nStep 4 验收通过：{len(cases)} 个用例两段式抽取与 gold 一致，无范围外/悬空，实体证据覆盖 100%。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
