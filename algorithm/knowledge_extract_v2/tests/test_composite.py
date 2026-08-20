"""Phase 1 Step 7 单元测试：复合元素（§21 CompositeElementHandler，AC-6）。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract_v2/tests/test_composite.py

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
        "composite",
        "split_extractor",
    ],
)
CC = MODULES["chunk_context"]
OC = MODULES["ontology_compiler"]
SE = MODULES["split_extractor"]


RULE_ONTOLOGY = (ONTOLOGIES_DIR / "rule_ontology.puml").read_text(encoding="utf-8")
COMPOSITE_TEXT = "未经本企业充分论证和安全测试，未经集团批准，不得部署安装各类开源智能体。"


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
        manager = CC.ChunkContextManager.from_text(COMPOSITE_TEXT)
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
        manager = CC.ChunkContextManager.from_text(COMPOSITE_TEXT)
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
        manager = CC.ChunkContextManager.from_text(COMPOSITE_TEXT)
        warnings: list[dict] = []
        handler = SE.CompositeElementHandler(plan.composite_specs)
        handler.assemble(pool, plan, manager, warnings=warnings)
        self.assertIn("INSUFFICIENT_COMPOSITE_MEMBERS", {w["code"] for w in warnings})


class IntegrationTest(unittest.TestCase):
    def _gold_stub(self, case: dict, relations: bool):
        expected = case["expected"]
        canonical = MODULES["graph_utils"].canonical_name

        def call(prompt: str) -> str:
            if "实体候选提议" in prompt:
                payload = {"entities": expected["entities"]}
            elif "关系候选提议" in prompt:
                if not relations:
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

    def _relation_keys(self, result: dict) -> set:
        canonical = MODULES["graph_utils"].canonical_name
        return {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in result["raw"]["relations"]
        }

    def test_composite_rule_01_auto_assembly_matches_gold(self) -> None:
        case = json.loads((CASES_DIR / "06_composite_rule.json").read_text(encoding="utf-8"))
        result = SE.extract_split(
            RULE_ONTOLOGY,
            text=case["text"],
            llm=self._gold_stub(case, relations=False),
            document_type=case.get("document_type", "general"),
        )
        expected = case["expected"]
        canonical = MODULES["graph_utils"].canonical_name
        gold_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in expected["relations"]
        }
        self.assertEqual(self._relation_keys(result), gold_relation_keys)
        self.assertEqual(result["profile"]["composite_auto_memberships"], 2)
        codes = {w["code"] for w in result["warnings"]}
        self.assertNotIn("INSUFFICIENT_COMPOSITE_MEMBERS", codes)
        self.assertNotIn("INVALID_COMBINATION_TYPE", codes)

    def test_composite_rule_01_full_gold_exact(self) -> None:
        case = json.loads((CASES_DIR / "06_composite_rule.json").read_text(encoding="utf-8"))
        result = SE.extract_split(
            RULE_ONTOLOGY,
            text=case["text"],
            llm=self._gold_stub(case, relations=True),
            document_type=case.get("document_type", "general"),
        )
        expected = case["expected"]
        canonical = MODULES["graph_utils"].canonical_name
        gold_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in expected["relations"]
        }
        self.assertEqual(self._relation_keys(result), gold_relation_keys)
        self.assertEqual(result["profile"]["composite_auto_memberships"], 0)
        self.assertEqual(result["raw"]["entities"][0]["properties"].get("combinationType"), "AND")


if __name__ == "__main__":
    unittest.main(verbosity=2)
