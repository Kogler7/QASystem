"""Phase 1 Step 7 单元测试：复合元素（§21 CompositeElementHandler，AC-6）。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract/v2_1/tests/test_composite.py

覆盖：
- §21 CompositeSpec 自动发现（CompositeRule hasSubRule SimpleRule +
  combinationType = AND | OR | SEQUENCE）；
- AC-6：成员关系确定性组装（triggerCondition + action 支撑判定）、
  combinationType 校验、成员数校验；
- 与 extract_split 集成：composite-rule-01 gold 实体无关系时自动补建
  hasSubRule，与 gold 关系集合一致。
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
    "knowledge_extract_v2_1",
    PACKAGE_DIR,
    [
        "config",
        "scheduler",
        "graph_utils",
        "ontology_schema",
        "ontology_compiler",
        "chunk_context",
        "evidence",
        "composite",
        "split_extractor",
    ],
)
CC = MODULES["chunk_context"]
OC = MODULES["ontology_compiler"]
SE = MODULES["split_extractor"]

from stub_llm import StubLLM, parse_known_entities, span_item  # noqa: E402


RULE_ONTOLOGY = (ONTOLOGIES_DIR / "rule_ontology.puml").read_text(encoding="utf-8")
COMPOSITE_TEXT = "未经本企业充分论证和安全测试，未经集团批准，不得部署安装各类开源智能体。"
MEMBER_MENTIONS = ["未经本企业充分论证和安全测试", "未经集团批准"]
MEMBER_RULE_TEXTS = [
    "未经本企业充分论证和安全测试，不得部署安装各类开源智能体。",
    "未经集团批准，不得部署安装各类开源智能体。",
]


def make_manager(text: str = COMPOSITE_TEXT) -> Any:
    return CC.ChunkContextManager([CC.ExtractionChunk(chunk_id="chunk-0", text=text)])


def make_pool() -> tuple[Any, Any]:
    schema = MODULES["ontology_schema"].OntologySchema.parse(RULE_ONTOLOGY)
    plan = OC.compile_plan(RULE_ONTOLOGY)
    pool = SE.CandidatePool(schema, allowed_types=plan.allowed_types)

    def add_entity(type_id: str, name: str, properties: dict) -> Any:
        return pool.add_entity(
            SE.EntityCandidate(
                candidate_id="",
                proposed_type=type_id,
                canonical_type=type_id,
                name=name,
                properties=properties,
                evidence=[],
                source="test",
            )
        )

    composite = add_entity(
        "CompositeRule",
        "CR-001",
        {"ruleId": "CR-001", "ruleText": COMPOSITE_TEXT, "combinationType": "AND"},
    )
    member1 = add_entity(
        "SimpleRule",
        "R-001",
        {
            "ruleId": "R-001",
            "ruleText": "未经本企业充分论证和安全测试，不得部署安装各类开源智能体。",
            "triggerCondition": "未经本企业充分论证和安全测试",
            "action": "不得部署安装各类开源智能体",
        },
    )
    member2 = add_entity(
        "SimpleRule",
        "R-002",
        {
            "ruleId": "R-002",
            "ruleText": "未经集团批准，不得部署安装各类开源智能体。",
            "triggerCondition": "未经集团批准",
            "action": "不得部署安装各类开源智能体",
        },
    )
    return pool, plan


class DiscoveryTest(unittest.TestCase):
    def test_rule_composite_spec_discovered(self) -> None:
        plan = OC.compile_plan(RULE_ONTOLOGY)
        spec = plan.composite_specs["CompositeRule"]
        self.assertEqual(spec.composite_type, "CompositeRule")
        self.assertEqual(spec.member_type, "SimpleRule")
        self.assertEqual(spec.membership_relation, "hasSubRule")
        self.assertEqual(spec.combination_property, "combinationType")
        self.assertEqual(spec.allowed_combinations, frozenset({"AND", "OR", "SEQUENCE"}))
        self.assertEqual(spec.min_members, 2)

    def test_no_composite_specs_for_plain_ontology(self) -> None:
        terminology = """@startuml t
class "术语" as Terminology <<KnowledgeUnit>> {
  + 术语标准名称 (TermName): String
}
' @identity Terminology: TermName
@enduml
"""
        plan = OC.compile_plan(terminology)
        self.assertEqual(plan.composite_specs, {})


class AssemblyTest(unittest.TestCase):
    def test_auto_membership_established(self) -> None:
        pool, plan = make_pool()
        manager = make_manager(COMPOSITE_TEXT)
        warnings: list[dict] = []
        handler = SE.CompositeElementHandler(plan.composite_specs)
        issues = handler.assemble(pool, plan, manager, warnings=warnings)
        memberships = [
            relation
            for relation in pool.accepted_relations()
            if relation.canonical_relation == "hasSubRule"
        ]
        self.assertEqual(len(memberships), 2)
        self.assertEqual(
            {relation.to_name for relation in memberships},
            {"R-001", "R-002"},
        )
        self.assertTrue(all(relation.evidence for relation in memberships))
        self.assertEqual(
            [issue["code"] for issue in issues],
            ["AUTO_MEMBERSHIP_ESTABLISHED", "AUTO_MEMBERSHIP_ESTABLISHED"],
        )
        # AND 合法、成员数足够 -> 无校验告警
        self.assertEqual(warnings, [])

    def test_missing_and_invalid_combination_type(self) -> None:
        schema = MODULES["ontology_schema"].OntologySchema.parse(RULE_ONTOLOGY)
        plan = OC.compile_plan(RULE_ONTOLOGY)
        pool = SE.CandidatePool(schema, allowed_types=plan.allowed_types)
        for name, combination in (("CR-MISSING", None), ("CR-BAD", "XOR")):
            properties = {"ruleId": name, "ruleText": COMPOSITE_TEXT}
            if combination is not None:
                properties["combinationType"] = combination
            pool.add_entity(
                SE.EntityCandidate(
                    candidate_id="",
                    proposed_type="CompositeRule",
                    canonical_type="CompositeRule",
                    name=name,
                    properties=properties,
                    evidence=[],
                    source="test",
                )
            )
        manager = make_manager(COMPOSITE_TEXT)
        warnings: list[dict] = []
        handler = SE.CompositeElementHandler(plan.composite_specs)
        handler.assemble(pool, plan, manager, warnings=warnings)
        codes = {warning["code"] for warning in warnings}
        self.assertIn("MISSING_COMBINATION_TYPE", codes)
        self.assertIn("INVALID_COMBINATION_TYPE", codes)
        self.assertIn("INSUFFICIENT_COMPOSITE_MEMBERS", codes)

    def test_insufficient_members(self) -> None:
        schema = MODULES["ontology_schema"].OntologySchema.parse(RULE_ONTOLOGY)
        plan = OC.compile_plan(RULE_ONTOLOGY)
        pool = SE.CandidatePool(schema, allowed_types=plan.allowed_types)
        composite = pool.add_entity(
            SE.EntityCandidate(
                candidate_id="",
                proposed_type="CompositeRule",
                canonical_type="CompositeRule",
                name="CR-001",
                properties={"ruleId": "CR-001", "ruleText": COMPOSITE_TEXT, "combinationType": "AND"},
                evidence=[],
                source="test",
            )
        )
        member = pool.add_entity(
            SE.EntityCandidate(
                candidate_id="",
                proposed_type="SimpleRule",
                canonical_type="SimpleRule",
                name="R-001",
                properties={"ruleId": "R-001", "triggerCondition": "未经集团批准", "action": "不得部署安装各类开源智能体"},
                evidence=[],
                source="test",
            )
        )
        pool.add_relation(
            SE.RelationCandidate(
                candidate_id="",
                from_candidate_id=composite.candidate_id,
                to_candidate_id=member.candidate_id,
                proposed_relation="hasSubRule",
                canonical_relation=None,
                evidence=[],
            )
        )
        manager = make_manager(COMPOSITE_TEXT)
        warnings: list[dict] = []
        handler = SE.CompositeElementHandler(plan.composite_specs)
        handler.assemble(pool, plan, manager, warnings=warnings)
        self.assertIn("INSUFFICIENT_COMPOSITE_MEMBERS", {w["code"] for w in warnings})


class IntegrationTest(unittest.TestCase):
    def _gold_stub(self, relations: bool) -> StubLLM:
        """新管线 stub：实体 span + 属性补全 + pair 关系分类（label 为 canonical relation_id）。"""

        def entity_handler(prompt: str, chunk: dict) -> list[dict]:
            return [
                span_item("CompositeRule", COMPOSITE_TEXT),
                span_item("SimpleRule", MEMBER_MENTIONS[0]),
                span_item("SimpleRule", MEMBER_MENTIONS[1]),
            ]

        def property_handler(prompt: str, chunk: dict) -> dict:
            updates = []
            for row in parse_known_entities(prompt):
                if row["type"] == "CompositeRule":
                    properties = {"ruleText": COMPOSITE_TEXT, "combinationType": "AND"}
                else:
                    index = MEMBER_MENTIONS.index(row["name"].strip())
                    properties = {
                        "ruleText": MEMBER_RULE_TEXTS[index],
                        "triggerCondition": MEMBER_MENTIONS[index],
                        "action": "不得部署安装各类开源智能体",
                    }
                updates.append(
                    {
                        "entity_id": row["id"],
                        "properties": properties,
                        "evidence": {"quote": COMPOSITE_TEXT},
                    }
                )
            return {"updates": updates}

        def relation_handler(prompt: str, pairs: list[dict], chunk: dict) -> list[dict]:
            if not relations:
                return []
            return [
                {"pair_id": pair["pair_id"], "label": pair["relation_id"], "evidence": {"quote": COMPOSITE_TEXT}}
                for pair in pairs
                if pair["relation_id"] == "hassubrule"
            ]

        return StubLLM(
            entity_handler=entity_handler,
            property_handler=property_handler,
            relation_handler=relation_handler,
        )

    def _relation_keys(self, result: dict) -> set:
        canonical = MODULES["graph_utils"].canonical_name
        return {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in result["raw"]["relations"]
        }

    def _gold_relation_keys(self) -> set:
        canonical = MODULES["graph_utils"].canonical_name
        return {
            ("CompositeRule", canonical(COMPOSITE_TEXT), "hasSubRule", "SimpleRule", canonical(MEMBER_MENTIONS[0])),
            ("CompositeRule", canonical(COMPOSITE_TEXT), "hasSubRule", "SimpleRule", canonical(MEMBER_MENTIONS[1])),
        }

    def _run_case(self, relations: bool) -> dict:
        case = json.loads((CASES_DIR / "06_composite_rule.json").read_text(encoding="utf-8"))
        return SE.extract_split(
            RULE_ONTOLOGY,
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=case["text"])],
            llm=self._gold_stub(relations),
        )

    def test_composite_rule_01_auto_assembly_matches_gold(self) -> None:
        result = self._run_case(relations=False)
        self.assertEqual(self._relation_keys(result), self._gold_relation_keys())
        self.assertEqual(result["profile"]["composite_auto_memberships"], 2)
        codes = {w["code"] for w in result["warnings"]}
        self.assertNotIn("INSUFFICIENT_COMPOSITE_MEMBERS", codes)
        self.assertNotIn("INVALID_COMBINATION_TYPE", codes)

    def test_composite_rule_01_full_gold_exact(self) -> None:
        result = self._run_case(relations=True)
        self.assertEqual(self._relation_keys(result), self._gold_relation_keys())
        self.assertEqual(result["profile"]["composite_auto_memberships"], 0)
        composite = next(
            obj for obj in result["intermediate_model"]["objects"] if obj["type"] == "CompositeRule"
        )
        self.assertEqual(composite["properties"].get("combinationType"), "AND")


if __name__ == "__main__":
    unittest.main(verbosity=2)
