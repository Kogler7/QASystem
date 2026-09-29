"""Phase 1 Step 9 单元测试：通用 Coverage Auditor（§22-§23 / AC-8）。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract/v2/tests/test_coverage_auditor.py

覆盖：
- §23.2 Feature Coverage（非 required 提示 / required 硬错误）；
- §23.3 Relation Coverage（cardinality min=1 的源端/目标端必需关系）；
- §23.4 Evidence Coverage；
- AC-8 结构漏抽（带结构元数据的 chunk 未绑定实体）；
- §23.5 Composite Coverage（成员数 / combinationType）；
- 与 extract_split 集成（composite-rule-01 自动组装后无复合 gap）。
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
        "split_extractor",
    ],
)
CC = MODULES["chunk_context"]
CV = MODULES["coverage"]
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


def make_manager(text: str) -> "CC.ChunkContextManager":
    return CC.ChunkContextManager([CC.ExtractionChunk(chunk_id="chunk-0", text=text)])


class FeatureCoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = OC.compile_plan(RULE_ONTOLOGY)
        self.auditor = CV.CoverageAuditor()
        self.manager = make_manager(COMPOSITE_TEXT)
        self.request = CC.ExtractionRequest(chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=COMPOSITE_TEXT)])

    def test_missing_features_reported_as_info(self) -> None:
        entities = [
            {
                "type": "SimpleRule",
                "name": "R-001",
                "properties": {"ruleId": "R-001"},
                "evidence": {"quote": COMPOSITE_TEXT},
            }
        ]
        gaps = self.auditor.audit(self.plan, self.manager, self.request, entities, [])
        feature_gaps = [gap for gap in gaps if gap.gap_type == "feature_coverage"]
        self.assertEqual(len(feature_gaps), 1)
        self.assertEqual(feature_gaps[0].severity, "info")
        self.assertEqual(
            set(feature_gaps[0].expected_features),
            {"ruleText", "triggerCondition", "action"},
        )

    def test_required_declared_makes_hard(self) -> None:
        required_specs = tuple(
            OC.FeatureSpec(
                feature_id=spec.feature_id,
                owner_type=spec.owner_type,
                kind=spec.kind,
                identifier=spec.identifier,
                datatype=spec.datatype,
                required=(spec.identifier == "ruleText"),
                multi_value=spec.multi_value,
                context_inferable=spec.context_inferable,
                lexical_patterns=spec.lexical_patterns,
                structural_patterns=spec.structural_patterns,
                relation_signature=spec.relation_signature,
                source=spec.source,
            )
            for spec in self.plan.feature_specs["SimpleRule"]
        )
        self.plan.feature_specs["SimpleRule"] = required_specs
        entities = [
            {
                "type": "SimpleRule",
                "name": "R-001",
                "properties": {"ruleId": "R-001"},
                "evidence": {"quote": COMPOSITE_TEXT},
            }
        ]
        gaps = self.auditor.audit(self.plan, self.manager, self.request, entities, [])
        feature_gap = next(gap for gap in gaps if gap.gap_type == "feature_coverage")
        self.assertEqual(feature_gap.severity, "warning")


class RelationCoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = OC.compile_plan(RULE_ONTOLOGY)
        self.auditor = CV.CoverageAuditor()
        self.manager = make_manager(COMPOSITE_TEXT)
        self.request = CC.ExtractionRequest(chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=COMPOSITE_TEXT)])

    def test_source_min_one_relation_missing(self) -> None:
        entities = [
            {
                "type": "CompositeRule",
                "name": "CR-001",
                "properties": {"ruleId": "CR-001", "ruleText": COMPOSITE_TEXT, "combinationType": "AND"},
                "evidence": {"quote": COMPOSITE_TEXT},
            }
        ]
        gaps = self.auditor.audit(self.plan, self.manager, self.request, entities, [])
        relation_gaps = [gap for gap in gaps if gap.gap_type == "relation_coverage"]
        self.assertTrue(any("hasSubRule" in gap.expected_relations for gap in relation_gaps))

    def test_target_min_one_relation_missing(self) -> None:
        entities = [
            {
                "type": "SimpleRule",
                "name": "R-001",
                "properties": {"ruleId": "R-001"},
                "evidence": {"quote": COMPOSITE_TEXT},
            }
        ]
        gaps = self.auditor.audit(self.plan, self.manager, self.request, entities, [])
        relation_gaps = [gap for gap in gaps if gap.gap_type == "relation_coverage"]
        self.assertTrue(any("hasSubRule" in gap.expected_relations for gap in relation_gaps))

    def test_relation_present_no_gap(self) -> None:
        entities = [
            {
                "type": "CompositeRule",
                "name": "CR-001",
                "properties": {"ruleId": "CR-001", "ruleText": COMPOSITE_TEXT, "combinationType": "AND"},
                "evidence": {"quote": COMPOSITE_TEXT},
            },
            {
                "type": "SimpleRule",
                "name": "R-001",
                "properties": {"ruleId": "R-001"},
                "evidence": {"quote": COMPOSITE_TEXT},
            },
        ]
        relations = [
            {
                "from_type": "CompositeRule",
                "from_name": "CR-001",
                "rel_type": "hasSubRule",
                "to_type": "SimpleRule",
                "to_name": "R-001",
                "evidence": {"quote": COMPOSITE_TEXT},
            }
        ]
        gaps = self.auditor.audit(self.plan, self.manager, self.request, entities, relations)
        has_sub_rule_gaps = [
            gap for gap in gaps if gap.gap_type == "relation_coverage" and "hasSubRule" in gap.expected_relations
        ]
        self.assertEqual(has_sub_rule_gaps, [])


class EvidenceAndStructuralTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = OC.compile_plan(RULE_ONTOLOGY)
        self.auditor = CV.CoverageAuditor()

    def test_evidence_missing(self) -> None:
        manager = make_manager(COMPOSITE_TEXT)
        request = CC.ExtractionRequest(chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=COMPOSITE_TEXT)])
        entities = [{"type": "SimpleRule", "name": "R-001", "properties": {"ruleId": "R-001"}}]
        relations = [{"from_name": "a", "to_name": "b", "rel_type": "hasSubRule"}]
        gaps = self.auditor.audit(self.plan, manager, request, entities, relations)
        evidence_gaps = [gap for gap in gaps if gap.gap_type == "evidence_missing"]
        self.assertEqual(len(evidence_gaps), 2)
        self.assertTrue(all(gap.severity == "warning" for gap in evidence_gaps))

    def test_structural_gap_for_metadata_chunk(self) -> None:
        chunk = CC.ExtractionChunk(
            chunk_id="c1",
            text="5.1 外观",
            clause_number="5.1",
            section_title="外观",
        )
        manager = CC.ChunkContextManager([chunk])
        request = CC.ExtractionRequest(chunks=[chunk])
        gaps = self.auditor.audit(self.plan, manager, request, [], [])
        self.assertTrue(any(gap.gap_type == "structural_coverage" for gap in gaps))
        # 绑定实体后不再报告
        entities = [{"type": "Clause", "name": "5.1", "properties": {}, "evidence": {"quote": "5.1 外观"}}]
        bound = MODULES["evidence"].bind_evidence({"entities": entities, "relations": []}, manager)
        gaps = self.auditor.audit(self.plan, manager, request, bound["entities"], [])
        self.assertFalse(any(gap.gap_type == "structural_coverage" for gap in gaps))


class CompositeCoverageTest(unittest.TestCase):
    def test_composite_membership_and_combination(self) -> None:
        plan = OC.compile_plan(RULE_ONTOLOGY)
        auditor = CV.CoverageAuditor()
        manager = make_manager(COMPOSITE_TEXT)
        request = CC.ExtractionRequest(chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=COMPOSITE_TEXT)])
        entities = [
            {
                "type": "CompositeRule",
                "name": "CR-001",
                "properties": {"ruleId": "CR-001", "ruleText": COMPOSITE_TEXT},
                "evidence": {"quote": COMPOSITE_TEXT},
            }
        ]
        gaps = auditor.audit(plan, manager, request, entities, [])
        composite_gaps = [gap for gap in gaps if gap.gap_type == "composite_membership"]
        self.assertEqual(len(composite_gaps), 1)
        self.assertIn("combinationType", composite_gaps[0].reason)
        self.assertEqual(composite_gaps[0].expected_relations, ("hasSubRule",))


class IntegrationTest(unittest.TestCase):
    def test_composite_rule_01_generic_coverage(self) -> None:
        case = json.loads((CASES_DIR / "06_composite_rule.json").read_text(encoding="utf-8"))

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

        llm = StubLLM(entity_handler=entity_handler, property_handler=property_handler)
        result = SE.extract_split(
            RULE_ONTOLOGY,
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=case["text"])],
            llm=llm,
        )
        gap_types = {gap["gap_type"] for gap in result["coverage_gaps"]}
        # 自动组装补全成员 -> 无复合 gap；hasSubRule 双端基数 min=1 均被自动成员关系覆盖
        self.assertNotIn("composite_membership", gap_types)
        self.assertNotIn("relation_coverage", gap_types)
        # gold 实体都带证据 -> 无 evidence_missing
        self.assertNotIn("evidence_missing", gap_types)


if __name__ == "__main__":
    unittest.main(verbosity=2)
