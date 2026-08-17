"""Phase 1 Step 9 单元测试：通用 Coverage Auditor（§22-§23 / AC-8）。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract_v2/tests/test_coverage_auditor.py

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
        "split_extractor",
    ],
)
CC = MODULES["chunk_context"]
CV = MODULES["coverage"]
OC = MODULES["ontology_compiler"]
SE = MODULES["split_extractor"]


RULE_ONTOLOGY = (ONTOLOGIES_DIR / "rule_ontology.puml").read_text(encoding="utf-8")
COMPOSITE_TEXT = "未经本企业充分论证和安全测试，未经集团批准，不得部署安装各类开源智能体。"


class FeatureCoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = OC.compile_plan(RULE_ONTOLOGY)
        self.auditor = CV.CoverageAuditor()
        self.manager = CC.ChunkContextManager.from_text(COMPOSITE_TEXT)
        self.request = CC.ExtractionRequest(ontology=RULE_ONTOLOGY, text=COMPOSITE_TEXT)

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
                required=(spec.identifier == "ruleId"),
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
        self.manager = CC.ChunkContextManager.from_text(COMPOSITE_TEXT)
        self.request = CC.ExtractionRequest(ontology=RULE_ONTOLOGY, text=COMPOSITE_TEXT)

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
        manager = CC.ChunkContextManager.from_text(COMPOSITE_TEXT)
        request = CC.ExtractionRequest(ontology=RULE_ONTOLOGY, text=COMPOSITE_TEXT)
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
        request = CC.ExtractionRequest(ontology=RULE_ONTOLOGY, chunks=[chunk])
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
        manager = CC.ChunkContextManager.from_text(COMPOSITE_TEXT)
        request = CC.ExtractionRequest(ontology=RULE_ONTOLOGY, text=COMPOSITE_TEXT)
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
        expected = case["expected"]

        def call(prompt: str) -> str:
            if "实体候选提议" in prompt:
                payload = {"entities": expected["entities"]}
            elif "关系候选提议" in prompt:
                payload = {"relations": []}
            else:
                raise AssertionError("无法识别的 prompt 段落")
            return json.dumps(payload, ensure_ascii=False)

        result = SE.extract_split(
            RULE_ONTOLOGY,
            text=case["text"],
            llm=call,
            document_type=case.get("document_type", "general"),
        )
        gap_types = {gap["gap_type"] for gap in result["coverage_gaps"]}
        # 自动组装补全成员 -> 无复合 gap；SimpleRule 无 constrainsObject -> 关系覆盖 gap
        self.assertNotIn("composite_membership", gap_types)
        self.assertIn("relation_coverage", gap_types)
        # gold 实体都带证据 -> 无 evidence_missing
        self.assertNotIn("evidence_missing", gap_types)


if __name__ == "__main__":
    unittest.main(verbosity=2)
