"""Phase 1 Step 10 单元测试：GraphAssembler + Compat（§26-§28）。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract/v0/tests/test_graph_assembler.py

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
    "knowledge_extract_v0",
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
        # build_graph 产出自洽：所有关系端点都在节点集内
        node_uids = {node["uid"] for node in graph["nodes"]}
        self.assertEqual(
            len(GU.filter_relationships_with_known_nodes(graph["relationships"], node_uids)),
            len(graph["relationships"]),
        )

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
        # v0 不抽取关系：关系集合恒为空
        self.assertEqual(pred_relation_keys, set())
        self.assertEqual(len(result["raw"]["relations"]), 0)
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
