"""Phase 1 Step 10 单元测试：GraphAssembler + Compat（§26-§28）。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract/v2/tests/test_graph_assembler.py

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
import sys
import types
import unittest
from copy import deepcopy
from itertools import permutations
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PACKAGE_DIR / "tests" / "fixtures"
ONTOLOGIES_DIR = FIXTURES_DIR / "ontologies"
CASES_DIR = FIXTURES_DIR / "cases"
TESTS_DIR = PACKAGE_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


def _load_package_modules(package_name: str, directory: Path, stems: list[str]) -> dict[str, types.ModuleType]:
    # pytest 全量运行时多个测试文件共享 sys.modules：已加载的模块直接复用，
    # 避免同名模块被重复 exec 出多个类对象（isinstance/类型一致性污染）。
    parent = sys.modules.get(package_name)
    if parent is None:
        parent = types.ModuleType(package_name)
        parent.__path__ = [str(directory)]
        parent.__package__ = package_name
        sys.modules[package_name] = parent
    loaded: dict[str, types.ModuleType] = {}
    for stem in stems:
        module_name = f"{package_name}.{stem}"
        if module_name in sys.modules:
            loaded[stem] = sys.modules[module_name]
            continue
        spec = importlib.util.spec_from_file_location(module_name, directory / f"{stem}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        loaded[stem] = module
    return loaded


MODULES = _load_package_modules(
    "algorithm.knowledge_extract.v2",
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
        "batch",
        "split_extractor",
    ],
)
GA = MODULES["graph_assembler"]
GU = MODULES["graph_utils"]
SE = MODULES["split_extractor"]
BATCH = MODULES["batch"]
COVERAGE = MODULES["coverage"]

from stub_llm import StubLLM, parse_known_entities, span_item  # noqa: E402


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
        "mapping": {"score": 0.88, "status": "accepted"},
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
        self.assertEqual(graph["nodes"][0]["mapping_score"], 0.95)
        self.assertLess(graph["nodes"][0]["confidence"], 0.95)
        self.assertFalse(graph["nodes"][0]["confidence_details"]["calibrated"])
        self.assertEqual(graph["relationships"][0]["mapping_score"], 0.88)
        self.assertEqual(graph["relationships"][0]["confidence"], 0.88)
        self.assertEqual(graph["nodes"][0]["evidences"], OBJECTS[0]["evidence"])
        self.assertEqual(graph["relationships"][0]["evidences"], RELATIONS[0]["evidence"])
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
        # build_graph 产出自洽：所有关系端点都在节点集内
        node_uids = {node["uid"] for node in graph["nodes"]}
        self.assertEqual(
            len(GU.filter_relationships_with_known_nodes(graph["relationships"], node_uids)),
            len(graph["relationships"]),
        )

    def test_duplicate_confidence_is_order_independent(self) -> None:
        low = {**OBJECTS[0], "id": "low", "mapping": {"score": 0.62, "status": "rejected"}}
        high = {**OBJECTS[0], "id": "high", "mapping": {"score": 0.95, "status": "accepted"}}
        forward = self.assembler.build_graph([low, high], [])["nodes"][0]
        reverse = self.assembler.build_graph([high, low], [])["nodes"][0]

        self.assertEqual(forward["mapping_score"], 0.62)
        self.assertEqual(forward["confidence"], reverse["confidence"])
        self.assertEqual(forward["confidence_details"], reverse["confidence_details"])
        self.assertEqual(
            forward["confidence_details"]["source_mapping_scores"],
            [0.62, 0.95],
        )
        self.assertEqual(
            forward["confidence_details"]["score_spread"],
            0.1975,
        )

    def test_three_equal_duplicate_details_are_order_independent(self) -> None:
        objects = [
            {
                **OBJECTS[0],
                "id": method,
                "confidence": 0.8,
                "confidence_details": {"method": method, "status": "accepted"},
            }
            for method in ("c", "a", "b")
        ]
        nodes = [
            self.assembler.build_graph(list(ordered), [])["nodes"][0]
            for ordered in permutations(objects)
        ]

        self.assertTrue(
            all(node["confidence_details"] == nodes[0]["confidence_details"] for node in nodes)
        )
        self.assertEqual(nodes[0]["confidence_details"]["method"], "a")
        self.assertEqual(nodes[0]["confidence_details"]["observation_count"], 3)

    def test_three_equal_chunk_nodes_have_order_independent_details(self) -> None:
        items = [
            {
                "index": index,
                "graph": {
                    "nodes": [
                        {
                            "uid": "Term::制动系统",
                            "label": "Term",
                            "properties": {"name": "制动系统"},
                            "confidence": 0.8,
                            "confidence_details": {
                                "method": method,
                                "status": "accepted",
                            },
                        }
                    ],
                    "relationships": [],
                },
                "raw": {"relations": []},
            }
            for index, method in enumerate(("c", "a", "b"))
        ]
        details = [
            BATCH.deduplicate_chunk_graphs(list(ordered))["nodes"][0]["confidence_details"]
            for ordered in permutations(items)
        ]

        self.assertTrue(all(item == details[0] for item in details))
        self.assertEqual(details[0]["method"], "a")
        self.assertEqual(details[0]["observation_count"], 3)

    def test_projected_aggregate_payload_is_counted_once(self) -> None:
        aggregate_details = {
            "aggregation": "minimum_duplicate_score",
            "source_scores": [0.72, 0.91],
            "source_mapping_scores": [0.8, 0.95],
            "observation_count": 2,
            "score_min": 0.72,
            "score_max": 0.91,
            "score_spread": 0.19,
        }
        raw = {
            "entities": [
                {
                    "type": "Term",
                    "name": "制动系统",
                    "evidence": [{"span_id": "chunk-0:0:4", "quote": "制动系统"}],
                }
            ],
            "relations": [],
        }
        items = [
            {
                "index": index,
                "graph": {
                    "nodes": [
                        {
                            "uid": "Term::制动系统",
                            "label": "Term",
                            "properties": {"name": "制动系统"},
                            "confidence": 0.72,
                            "confidence_details": aggregate_details,
                        }
                    ],
                    "relationships": [],
                },
                "raw": deepcopy(raw),
            }
            for index in range(2)
        ]

        merged = BATCH.deduplicate_chunk_graphs(items)
        details = merged["nodes"][0]["confidence_details"]
        self.assertEqual(details["observation_count"], 2)
        self.assertEqual(details["source_scores"], [0.72, 0.91])

    def test_projected_single_payload_is_not_promoted_to_multiple_observations(self) -> None:
        raw = {
            "entities": [{"type": "Term", "name": "制动系统"}],
            "relations": [],
        }
        items = [
            {
                "index": index,
                "graph": {
                    "nodes": [
                        {
                            "uid": "Term::制动系统",
                            "label": "Term",
                            "properties": {"name": "制动系统"},
                            "confidence": 0.82,
                            "confidence_details": {"method": "single"},
                        }
                    ],
                    "relationships": [],
                },
                "raw": deepcopy(raw),
            }
            for index in range(2)
        ]

        details = BATCH.deduplicate_chunk_graphs(items)["nodes"][0]["confidence_details"]
        self.assertNotIn("observation_count", details)
        self.assertNotIn("source_scores", details)

    def test_independent_aggregate_payloads_still_merge(self) -> None:
        aggregate_details = {
            "aggregation": "minimum_duplicate_score",
            "source_scores": [0.72, 0.91],
            "observation_count": 2,
        }
        items = [
            {
                "index": index,
                "graph": {
                    "nodes": [
                        {
                            "uid": "Term::制动系统",
                            "label": "Term",
                            "properties": {"name": "制动系统"},
                            "confidence": 0.72,
                            "confidence_details": aggregate_details,
                        }
                    ],
                    "relationships": [],
                },
                "raw": {
                    "entities": [
                        {
                            "type": "Term",
                            "name": "制动系统",
                            "evidence": [{"span_id": f"chunk-{index}:0:4", "quote": "制动系统"}],
                        }
                    ],
                    "relations": [],
                },
            }
            for index in range(2)
        ]

        merged = BATCH.deduplicate_chunk_graphs(items)
        details = merged["nodes"][0]["confidence_details"]
        self.assertEqual(details["observation_count"], 4)
        self.assertEqual(details["source_scores"], [0.72, 0.72, 0.91, 0.91])

    def test_chunk_deduplication_does_not_mutate_inputs(self) -> None:
        items = [
            {
                "index": index,
                "graph": {
                    "nodes": [
                        {
                            "uid": "Term::制动系统",
                            "label": "Term",
                            "properties": {"name": "制动系统"},
                            "confidence": score,
                            "confidence_details": {"method": "test", "status": "accepted"},
                        }
                    ],
                    "relationships": [],
                },
                "raw": {"relations": []},
            }
            for index, score in enumerate((0.8, 0.9))
        ]
        before = deepcopy(items)

        BATCH.deduplicate_chunk_graphs(items)

        self.assertEqual(items, before)

    def test_batch_counts_identical_document_projection_once(self) -> None:
        graph = self.assembler.build_graph(OBJECTS, RELATIONS)
        raw = self.assembler.compat_raw(OBJECTS, RELATIONS)
        items = [
            {"index": index, "graph": deepcopy(graph), "raw": deepcopy(raw)}
            for index in range(2)
        ]

        merged = BATCH.deduplicate_chunk_graphs(items)

        self.assertEqual(len(merged["relationships"]), 1)
        details = merged["relationships"][0]["confidence_details"]
        self.assertNotIn("observation_count", details)
        self.assertNotIn("source_scores", details)

    def test_uid_stable_regardless_of_properties(self) -> None:
        objects = [
            {"id": "a", "type": "SimpleRule", "identity": "R-001", "properties": {"action": "A"}},
            {"id": "b", "type": "SimpleRule", "identity": "R-001", "properties": {"action": "B"}},
        ]
        graph = self.assembler.build_graph(objects, [])
        self.assertEqual(len(graph["nodes"]), 1)
        self.assertEqual(graph["nodes"][0]["uid"], GU.entity_uid("SimpleRule", "R-001", None))
        self.assertEqual(
            graph["nodes"][0]["properties"],
            {"action": "A", "name": "R-001"},
        )

    def test_duplicate_uid_merges_complementary_properties(self) -> None:
        objects = [
            {
                "id": "a",
                "type": "Term",
                "identity": "T-001",
                "properties": {"code": "T-001"},
                "evidence": [{"span_id": "chunk-0:0:5", "quote": "T-001"}],
            },
            {
                "id": "b",
                "type": "Term",
                "identity": "T-001",
                "properties": {"title": "制动系统"},
                "evidence": [{"span_id": "chunk-1:0:4", "quote": "制动系统"}],
            },
        ]

        node = self.assembler.build_graph(objects, [])["nodes"][0]

        self.assertEqual(
            node["properties"],
            {"code": "T-001", "title": "制动系统", "name": "T-001"},
        )
        self.assertEqual(
            {item["span_id"] for item in node["evidences"]},
            {"chunk-0:0:5", "chunk-1:0:4"},
        )

    def test_raw_relation_keeps_canonical_endpoint_uids(self) -> None:
        objects = [
            {"id": "a", "type": "Term", "identity": "T-001", "display_name": "制动系统"},
            {"id": "b", "type": "Term", "identity": "T-002", "display_name": "控制器"},
        ]
        relations = [{"id": "r", "type": "contains", "from": "a", "to": "b"}]

        relation = self.assembler.compat_raw(objects, relations)["relations"][0]

        self.assertEqual(relation["from_uid"], GU.entity_uid("Term", "T-001", None))
        self.assertEqual(relation["to_uid"], GU.entity_uid("Term", "T-002", None))

        graph = self.assembler.build_graph(objects, [])
        merged = BATCH.deduplicate_chunk_graphs(
            [{"index": 0, "graph": graph, "raw": {"entities": [], "relations": [relation]}}]
        )
        self.assertEqual(
            merged["relationships"],
            [
                {
                    "from_uid": GU.entity_uid("Term", "T-001", None),
                    "to_uid": GU.entity_uid("Term", "T-002", None),
                    "type": "contains",
                }
            ],
        )

    def test_triples(self) -> None:
        graph = self.assembler.build_graph(OBJECTS, RELATIONS)
        triples = self.assembler.build_triples(graph)
        self.assertEqual(len(triples), 1)
        self.assertEqual(triples[0]["relation"], "hasSubRule")
        self.assertEqual(triples[0]["head"], graph["relationships"][0]["from_uid"])
        self.assertEqual(triples[0]["tail"], graph["relationships"][0]["to_uid"])
        self.assertEqual(triples[0]["mapping_score"], 0.88)
        self.assertEqual(
            triples[0]["confidence"],
            graph["relationships"][0]["confidence"],
        )
        self.assertEqual(
            triples[0]["confidence_details"],
            graph["relationships"][0]["confidence_details"],
        )
        self.assertEqual(triples[0]["evidences"], RELATIONS[0]["evidence"])

    def test_dangling_relation_rejected_and_duplicate_uid_merged(self) -> None:
        # 当前实现不再提供 validate()：悬空关系在 build_graph 时直接拒绝
        objects = [
            {"id": "a", "type": "SimpleRule", "identity": "R-001", "properties": {}},
        ]
        with self.assertRaises(GA.GraphIntegrityError):
            self.assembler.build_graph(
                objects,
                [{"id": "relation-0", "type": "hasSubRule", "from": "a", "to": "missing"}],
            )
        # 同 identity 的对象合并为同一节点（重复 uid 去重）
        duplicates = [
            {"id": "a", "type": "SimpleRule", "identity": "R-001", "properties": {"action": "A"}},
            {"id": "b", "type": "SimpleRule", "identity": "R-001", "properties": {"action": "B"}},
        ]
        graph = self.assembler.build_graph(duplicates, [])
        self.assertEqual(len(graph["nodes"]), 1)

    def test_compat_raw_shape(self) -> None:
        raw = self.assembler.compat_raw(OBJECTS, RELATIONS)
        entity = raw["entities"][0]
        self.assertEqual(
            set(entity),
            {
                "type", "name", "properties", "candidate_id", "evidence", "evidences",
                "mapping_score", "confidence", "confidence_status", "gate_status",
                "confidence_details",
            },
        )
        self.assertEqual(entity["type"], "CompositeRule")
        self.assertEqual(entity["name"], "CR-001")
        self.assertEqual(entity["candidate_id"], "entity-0")
        self.assertEqual(entity["evidences"], OBJECTS[0]["evidence"])
        relation = raw["relations"][0]
        self.assertEqual(
            set(relation),
            {
                "from_id", "from_uid", "from_type", "from_name", "rel_type", "to_type", "to_name",
                "to_id", "to_uid", "evidence", "evidences",
                "mapping_score", "confidence", "confidence_status", "gate_status",
                "confidence_details",
            },
        )
        self.assertEqual(relation["rel_type"], "hasSubRule")
        self.assertEqual(relation["mapping_score"], 0.88)

    def test_canonical_raw_keeps_ids_for_internal_validation(self) -> None:
        objects = [
            {
                "id": "rule-1",
                "type": "SimpleRule",
                "identity": "R-001",
                "display_name": "规则一",
                "properties": {"ruleText": "必须检查", "action": "检查"},
                "compat_properties": {"规则文本": "必须检查", "动作": "检查"},
            }
        ]

        compat = self.assembler.compat_raw(objects, [])
        canonical = self.assembler.canonical_raw(objects, [])

        self.assertEqual(compat["entities"][0]["properties"], {"规则文本": "必须检查", "动作": "检查"})
        self.assertEqual(
            canonical["entities"][0]["properties"],
            {"ruleText": "必须检查", "action": "检查"},
        )

    def test_canonical_properties_are_covered_but_missing_id_is_reported(self) -> None:
        plan = SE.compile_plan((ONTOLOGIES_DIR / "rule_ontology.puml").read_text(encoding="utf-8"))
        intrinsic = [
            spec
            for spec in plan.feature_specs["SimpleRule"]
            if spec.kind == "intrinsic"
        ]
        properties = {spec.identifier: "present" for spec in intrinsic}
        labels = {
            spec.identifier: spec.display_name or spec.identifier
            for spec in plan.class_specs["SimpleRule"].properties
        }
        objects = [{
            "id": "rule-1",
            "type": "SimpleRule",
            "identity": "R-001",
            "display_name": "规则一",
            "properties": properties,
            "compat_properties": {labels[key]: value for key, value in properties.items()},
        }]
        canonical = self.assembler.canonical_raw(objects, [])

        complete_gaps = COVERAGE.CoverageAuditor().audit(plan, [], None, canonical["entities"], [])
        self.assertEqual(
            [gap for gap in complete_gaps if gap.gap_type == "feature_coverage"],
            [],
        )

        missing_id = next(iter(properties))
        canonical["entities"][0]["properties"].pop(missing_id)
        missing_gaps = COVERAGE.CoverageAuditor().audit(
            plan, [], None, canonical["entities"], []
        )
        feature_gaps = [gap for gap in missing_gaps if gap.gap_type == "feature_coverage"]
        self.assertEqual(len(feature_gaps), 1)
        self.assertIn(missing_id, feature_gaps[0].expected_features)


class IntegrationTest(unittest.TestCase):
    def test_extract_split_verifies_required_properties_by_canonical_id(self) -> None:
        ontology = """@startuml
class "设备" as Equipment {
  + 编号 (code): String
}
' @required Equipment:code
@enduml"""
        text = "设备A的编号为00123。"

        def entity_handler(prompt: str, chunk: dict) -> list[dict]:
            return [span_item("Equipment", "设备A")]

        def property_handler(prompt: str, chunk: dict) -> dict:
            updates = []
            for row in parse_known_entities(prompt):
                updates.append({
                    "entity_id": row["id"],
                    "properties": {"code": "00123"},
                    "evidence": {"quote": text},
                })
            return {"updates": updates}

        def run(property_updates: bool) -> dict:
            llm = StubLLM(
                entity_handler=entity_handler,
                property_handler=property_handler if property_updates else lambda _p, _c: {"updates": []},
            )
            return SE.extract_split(
                ontology,
                chunks=[SE.ExtractionChunk(chunk_id="chunk-0", text=text)],
                llm=llm,
                identity_admission="observe",
            )

        present = run(True)
        present_codes = {
            issue["code"] for issue in present["verification"]["issues"]
        }
        self.assertNotIn("REQUIRED_PROPERTY_MISSING", present_codes)
        self.assertEqual(present["raw"]["entities"][0]["properties"], {"编号": "00123"})

        missing = run(False)
        missing_codes = {
            issue["code"] for issue in missing["verification"]["issues"]
        }
        self.assertIn("REQUIRED_PROPERTY_MISSING", missing_codes)

    def test_result_contract_and_graph_consistency(self) -> None:
        case = json.loads((CASES_DIR / "06_composite_rule.json").read_text(encoding="utf-8"))
        ontology = (ONTOLOGIES_DIR / case["ontology_file"]).read_text(encoding="utf-8")
        text = case["text"]
        # 新管线为 span-first：实体名 = 原文 mention（gold 中的 CR-001/R-001 等
        # 编号不在原文中，属性补全也要求标识类属性逐字可定位，因此按 mention 断言）。
        composite_mention = "未经本企业充分论证和安全测试，未经集团批准，不得部署安装各类开源智能体。"
        member_mentions = ["未经本企业充分论证和安全测试", "未经集团批准"]
        member_rule_texts = [
            "未经本企业充分论证和安全测试，不得部署安装各类开源智能体。",
            "未经集团批准，不得部署安装各类开源智能体。",
        ]

        def entity_handler(prompt: str, chunk: dict) -> list[dict]:
            return [
                span_item("CompositeRule", composite_mention),
                span_item("SimpleRule", member_mentions[0]),
                span_item("SimpleRule", member_mentions[1]),
            ]

        def property_handler(prompt: str, chunk: dict) -> dict:
            updates = []
            for row in parse_known_entities(prompt):
                if row["type"] == "CompositeRule":
                    properties = {"ruleText": composite_mention, "combinationType": "AND"}
                else:
                    index = member_mentions.index(row["name"].strip())
                    properties = {
                        "ruleText": member_rule_texts[index],
                        "triggerCondition": member_mentions[index],
                        "action": "不得部署安装各类开源智能体",
                    }
                updates.append(
                    {
                        "entity_id": row["id"],
                        "properties": properties,
                        "evidence": {"quote": composite_mention},
                    }
                )
            return {"updates": updates}

        def relation_handler(prompt: str, pairs: list[dict], chunk: dict) -> list[dict]:
            return [
                {"pair_id": pair["pair_id"], "label": pair["relation_id"], "evidence": {"quote": composite_mention}}
                for pair in pairs
                if pair["relation_id"] == "hassubrule"
            ]

        llm = StubLLM(
            entity_handler=entity_handler,
            property_handler=property_handler,
            relation_handler=relation_handler,
        )
        result = SE.extract_split(
            ontology,
            chunks=[SE.ExtractionChunk(chunk_id="chunk-0", text=text)],
            llm=llm,
            identity_admission="observe",
        )
        # 保留现有 API + 新字段
        for key in ("raw", "graph", "warnings", "attempts", "profile", "error_code"):
            self.assertIn(key, result)
        for key in ("triples", "intermediate_model", "evidence_coverage", "coverage_gaps", "mapping"):
            self.assertIn(key, result)
        # raw 实体：1 CompositeRule + 2 SimpleRule，均带 hasSubRule
        canonical = GU.canonical_name
        pred_entity_keys = {(e["type"], canonical(e["name"])) for e in result["raw"]["entities"]}
        self.assertEqual(
            pred_entity_keys,
            {
                ("CompositeRule", canonical(composite_mention)),
                ("SimpleRule", canonical(member_mentions[0])),
                ("SimpleRule", canonical(member_mentions[1])),
            },
        )
        pred_relation_keys = {
            (r["from_type"], r["rel_type"], r["to_type"]) for r in result["raw"]["relations"]
        }
        self.assertEqual(pred_relation_keys, {("CompositeRule", "hasSubRule", "SimpleRule")})
        self.assertEqual(len(result["raw"]["relations"]), 2)
        # graph 校验：无悬空、uid 可复算、triples 一致
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
