"""Phase 1 Step 10 单元测试：GraphAssembler + Compat（§26-§28）。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract_v2/tests/test_graph_assembler.py

覆盖：
- §26 O2：O1 objects/relations -> nodes / relationships / triples；
- §27 UID 稳定性：普通属性不影响 UID，同 identity 合并为同一节点；
- §28 compat_raw：与现有 raw 契约一致（entities/relations 字段形态）；
- AC-7 代理：validate 能发现悬空关系/重复 uid；
- 与 extract_split 集成：返回契约保留现有 API 并附带新字段。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
BENCHMARK_DIR = PACKAGE_DIR / "benchmark"
ONTOLOGIES_DIR = BENCHMARK_DIR / "ontologies"
CASES_DIR = BENCHMARK_DIR / "cases"


def _load_package_modules(package_name: str, directory: Path, stems: list[str]) -> dict[str, types.ModuleType]:
    parent = types.ModuleType(package_name)
    parent.__path__ = [str(directory)]
    parent.__package__ = package_name
    sys.modules[package_name] = parent
    loaded: dict[str, types.ModuleType] = {}
    for stem in stems:
        module_name = f"{package_name}.{stem}"
        spec = importlib.util.spec_from_file_location(module_name, directory / f"{stem}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        loaded[stem] = module
    return loaded


MODULES = _load_package_modules(
    "knowledge_extract_v2",
    PACKAGE_DIR,
    [
        "config",
        "scheduler",
        "graph_utils",
        "ontology_schema",
        "ontology_compiler",
        "chunk_context",
        "evidence",
        "coverage",
        "composite",
        "graph_assembler",
        "split_extractor",
    ],
)
GA = MODULES["graph_assembler"]
GU = MODULES["graph_utils"]
SE = MODULES["split_extractor"]


OBJECTS = [
    {
        "id": "entity-0",
        "type": "CompositeRule",
        "identity": "CR-001",
        "properties": {"ruleId": "CR-001", "combinationType": "AND"},
        "evidence": [{"span_id": "chunk-0:0:10", "quote": "示例"}],
        "mapping": {"score": 0.95, "status": "accepted"},
    },
    {
        "id": "entity-1",
        "type": "SimpleRule",
        "identity": "R-001",
        "properties": {"ruleId": "R-001"},
        "evidence": [],
        "mapping": {"score": 0.9, "status": "accepted"},
    },
]

RELATIONS = [
    {
        "id": "relation-0",
        "type": "hasSubRule",
        "from": "entity-0",
        "to": "entity-1",
        "evidence": [{"span_id": "chunk-0:0:10", "quote": "示例"}],
    }
]


class GraphAssemblerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assembler = GA.GraphAssembler()

    def test_build_graph_nodes_and_relationships(self) -> None:
        graph = self.assembler.build_graph(OBJECTS, RELATIONS)
        self.assertEqual(len(graph["nodes"]), 2)
        self.assertEqual(graph["nodes"][0]["label"], "CompositeRule")
        self.assertEqual(
            graph["nodes"][0]["uid"],
            GU.entity_uid("CompositeRule", "CR-001", None),
        )
        self.assertEqual(len(graph["relationships"]), 1)
        relationship = graph["relationships"][0]
        self.assertEqual(relationship["type"], "hasSubRule")
        self.assertEqual(
            relationship["from_uid"],
            GU.entity_uid("CompositeRule", "CR-001", None),
        )
        self.assertEqual(
            relationship["to_uid"],
            GU.entity_uid("SimpleRule", "R-001", None),
        )
        self.assertEqual(self.assembler.validate(graph), [])

    def test_uid_stable_regardless_of_properties(self) -> None:
        objects = [
            {"id": "a", "type": "SimpleRule", "identity": "R-001", "properties": {"action": "A"}},
            {"id": "b", "type": "SimpleRule", "identity": "R-001", "properties": {"action": "B"}},
        ]
        graph = self.assembler.build_graph(objects, [])
        self.assertEqual(len(graph["nodes"]), 1)
        self.assertEqual(graph["nodes"][0]["uid"], GU.entity_uid("SimpleRule", "R-001", None))

    def test_triples(self) -> None:
        graph = self.assembler.build_graph(OBJECTS, RELATIONS)
        triples = self.assembler.build_triples(graph)
        self.assertEqual(len(triples), 1)
        self.assertEqual(triples[0]["relation"], "hasSubRule")
        self.assertEqual(triples[0]["head"], graph["relationships"][0]["from_uid"])
        self.assertEqual(triples[0]["tail"], graph["relationships"][0]["to_uid"])

    def test_validate_detects_dangling_and_duplicate(self) -> None:
        graph = {
            "nodes": [
                {"uid": "u1", "label": "A", "properties": {}},
                {"uid": "u1", "label": "B", "properties": {}},
            ],
            "relationships": [{"from_uid": "u1", "to_uid": "missing", "type": "R"}],
        }
        issues = self.assembler.validate(graph)
        codes = {issue["code"] for issue in issues}
        self.assertIn("DUPLICATE_NODE_UID", codes)
        self.assertIn("DANGLING_RELATIONSHIP", codes)

    def test_compat_raw_shape(self) -> None:
        raw = self.assembler.compat_raw(OBJECTS, RELATIONS)
        entity = raw["entities"][0]
        self.assertEqual(
            set(entity),
            {"type", "name", "properties", "candidate_id", "evidence"},
        )
        self.assertEqual(entity["type"], "CompositeRule")
        self.assertEqual(entity["name"], "CR-001")
        self.assertEqual(entity["candidate_id"], "entity-0")
        relation = raw["relations"][0]
        self.assertEqual(
            set(relation),
            {"from_id", "from_type", "from_name", "rel_type", "to_type", "to_name", "to_id", "evidence"},
        )
        self.assertEqual(relation["rel_type"], "hasSubRule")


class IntegrationTest(unittest.TestCase):
    def test_result_contract_and_graph_consistency(self) -> None:
        case = json.loads((CASES_DIR / "06_composite_rule.json").read_text(encoding="utf-8"))
        expected = case["expected"]
        ontology = (ONTOLOGIES_DIR / case["ontology_file"]).read_text(encoding="utf-8")
        canonical = GU.canonical_name

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

        result = SE.extract_split(
            ontology,
            text=case["text"],
            llm=call,
            document_type=case.get("document_type", "general"),
            strategy="generic",
        )
        # 保留现有 API + 新字段
        for key in ("raw", "graph", "warnings", "attempts", "profile", "error_code"):
            self.assertIn(key, result)
        for key in ("triples", "intermediate_model", "evidence_coverage", "coverage_gaps", "mapping"):
            self.assertIn(key, result)
        # raw 与 gold 一致
        gold_entity_keys = {(e["type"], canonical(e["name"])) for e in expected["entities"]}
        pred_entity_keys = {(e["type"], canonical(e["name"])) for e in result["raw"]["entities"]}
        self.assertEqual(pred_entity_keys, gold_entity_keys)
        # graph 校验：无悬空、uid 可复算、triples 一致
        assembler = GA.GraphAssembler()
        self.assertEqual(assembler.validate(result["graph"]), [])
        node_uids = {node["uid"] for node in result["graph"]["nodes"]}
        for entity in result["raw"]["entities"]:
            self.assertIn(
                GU.entity_uid(entity["type"], entity["name"], None),
                node_uids,
            )
        relationships = result["graph"]["relationships"]
        self.assertEqual(len(result["triples"]), len(relationships))
        self.assertEqual(
            {(t["head"], t["relation"], t["tail"]) for t in result["triples"]},
            {(r["from_uid"], r["type"], r["to_uid"]) for r in relationships},
        )
        # 兼容既有批量合并消费者：关系端点全部在节点集内
        kept = GU.filter_relationships_with_known_nodes(relationships, node_uids)
        self.assertEqual(len(kept), len(relationships))


if __name__ == "__main__":
    unittest.main(verbosity=2)
