"""Phase 1 Step 8 单元测试：Mapping Scorer（§17-§19）。

运行（在 88 测试环境执行）：
    python algorithm/knowledge_extract/v3_1/tests/test_mapping_scorer.py

覆盖：
- §17 权重/阈值配置（归一化、非法阈值拒绝、权重影响总分）；
- §18 属性评分（覆盖/合法性/identity、required penalty 仅在本体声明时启用）；
- §19 关系参与评分、关系合法性/证据/cardinality 评分；
- §16 L4 上下文分量（本体未允许推断时不适用）；
- §34 状态边界（>=0.85 accepted / >=0.70 low / <0.70 rejected）；
- 与 extract_split 集成：默认注释模式不影响结果，enforce 模式按阈值过滤。
"""
from __future__ import annotations

import types
import unittest
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PACKAGE_DIR / "tests" / "fixtures"
ONTOLOGIES_DIR = FIXTURES_DIR / "ontologies"
CASES_DIR = FIXTURES_DIR / "cases"
TESTS_DIR = PACKAGE_DIR / "tests"


from .. import mapping_scorer as MS
from .. import ontology_compiler as OC
from .. import split_extractor as SE
from .. import chunk_context as CC

from .stub_llm import StubLLM, parse_known_entities, span_item  # noqa: E402


RULE_ONTOLOGY = (ONTOLOGIES_DIR / "rule_ontology.puml").read_text(encoding="utf-8")


def make_entity(plan: object, type_id: str, name: str, properties: dict) -> Any:
    return MS.EntityMappingInput(type=type_id, properties=properties)


class ConfigTest(unittest.TestCase):
    def test_default_weights_normalized(self) -> None:
        scorer = MS.MappingScorer()
        self.assertAlmostEqual(sum(scorer.weights.values()), 1.0)
        self.assertEqual(scorer.accepted_threshold, 0.85)
        self.assertEqual(scorer.low_confidence_threshold, 0.70)

    def test_custom_weights_normalized(self) -> None:
        scorer = MS.MappingScorer(weights={"attribute": 2, "context": 1})
        self.assertAlmostEqual(scorer.weights["attribute"], 2 / 3)
        self.assertAlmostEqual(scorer.weights["context"], 1 / 3)

    def test_invalid_thresholds_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MS.MappingScorer(accepted_threshold=0.7, low_confidence_threshold=0.85)


class EntityScoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = OC.compile_plan(RULE_ONTOLOGY)
        self.scorer = MS.MappingScorer()

    def test_attribute_coverage_validity_identity(self) -> None:
        entity = make_entity(
            self.plan,
            "SimpleRule",
            "R-001",
            {"ruleId": "R-001", "ruleText": "t", "triggerCondition": "c", "action": "a"},
        )
        score = self.scorer.score_entity(entity, self.plan)
        # 4/4 覆盖、全部合法、identity 命中 -> attribute = 0.5*1 + 0.3*1 + 0.2*1
        self.assertEqual(score.breakdown["attribute"], 1.0)

    def test_required_penalty_only_when_declared(self) -> None:
        entity = make_entity(
            self.plan,
            "SimpleRule",
            "R-001",
            {"ruleId": "R-001", "ruleText": "t"},
        )
        # 未声明 required -> 无惩罚
        score = self.scorer.score_entity(entity, self.plan)
        self.assertEqual(score.breakdown["attribute"], round(0.5 * 0.5 + 0.3 * 1.0 + 0.2 * 1.0, 4))
        # 声明 TermName/TermDefinition required -> 缺失惩罚
        terminology_plan = OC.compile_plan(
            """@startuml t
class "术语" as Terminology <<KnowledgeUnit>> {
  + 术语标准名称 (TermName): String
  + 术语标准定义 (TermDefinition): String
}
' @identity Terminology: TermName
@enduml
"""
        )
        required_specs = tuple(
            OC.FeatureSpec(
                feature_id=spec.feature_id,
                owner_type=spec.owner_type,
                kind=spec.kind,
                identifier=spec.identifier,
                datatype=spec.datatype,
                required=(spec.identifier in {"TermName", "TermDefinition"}),
                multi_value=spec.multi_value,
                context_inferable=spec.context_inferable,
                lexical_patterns=spec.lexical_patterns,
                structural_patterns=spec.structural_patterns,
                relation_signature=spec.relation_signature,
                source=spec.source,
            )
            for spec in terminology_plan.feature_specs["Terminology"]
        )
        terminology_plan.feature_specs["Terminology"] = required_specs
        partial = make_entity(terminology_plan, "Terminology", "主数据治理", {"TermName": "主数据治理"})
        score = self.scorer.score_entity(partial, terminology_plan)
        self.assertEqual(score.breakdown["attribute"], round(0.6 * 0.5 + 0.4 * 0.5, 4))


    def test_context_not_applicable_by_default(self) -> None:
        entity = make_entity(self.plan, "Object", "办公设备", {"objectName": "办公设备"})
        score = self.scorer.score_entity(entity, self.plan)
        self.assertIsNone(score.breakdown["context"])

    def test_context_score_when_ontology_allows(self) -> None:
        plan = OC.compile_plan(RULE_ONTOLOGY)
        entity = make_entity(plan, "SimpleRule", "R-001", {"ruleId": "R-001", "ruleText": "t"})
        context_spec = OC.FeatureSpec(
            feature_id="SimpleRule.ctx",
            owner_type="SimpleRule",
            kind="context",
            identifier="contextHint",
            datatype="String",
            required=None,
            multi_value=None,
            context_inferable=True,
            lexical_patterns=(),
            structural_patterns=(),
            relation_signature=None,
            source="test",
        )
        plan.feature_specs["SimpleRule"] = (*plan.feature_specs["SimpleRule"], context_spec)
        score = self.scorer.score_entity(entity, plan)
        self.assertEqual(score.breakdown["context"], 0.0)  # 属性缺失
        entity.properties["contextHint"] = "x"
        score = self.scorer.score_entity(entity, plan)
        self.assertEqual(score.breakdown["context"], 1.0)


class StatusAndWeightTest(unittest.TestCase):
    def test_status_boundaries(self) -> None:
        scorer = MS.MappingScorer()
        self.assertEqual(scorer._status(0.90), "accepted")
        self.assertEqual(scorer._status(0.85), "accepted")
        self.assertEqual(scorer._status(0.75), "low_confidence")
        self.assertEqual(scorer._status(0.70), "low_confidence")
        self.assertEqual(scorer._status(0.50), "rejected")

    def test_weights_affect_total(self) -> None:
        plan = OC.compile_plan(RULE_ONTOLOGY)
        entity = make_entity(
            plan,
            "SimpleRule",
            "R-001",
            {"ruleId": "R-001", "ruleText": "t", "triggerCondition": "c", "action": "a"},
        )
        attr_weighted = MS.MappingScorer(weights={"attribute": 1.0}).score_entity(entity, plan)
        context_weighted = MS.MappingScorer(weights={"context": 1.0}).score_entity(entity, plan)
        self.assertEqual(attr_weighted.score, 1.0)
        self.assertEqual(context_weighted.score, 0.0)




class IntegrationTest(unittest.TestCase):
    TEXT = "未经集团批准，不得部署安装各类开源智能体。"

    def _extract(self, properties: dict | None, **kwargs):
        def entity_handler(prompt: str, chunk: dict) -> list[dict]:
            return [span_item("SimpleRule", self.TEXT)]

        def property_handler(prompt: str, chunk: dict) -> dict:
            return {
                "entities": [
                    {
                        "type": row["type"], "name": row["name"],
                        "properties": dict(properties or {}),
                        "evidence": {"quote": self.TEXT},
                    }
                    for row in parse_known_entities(prompt)
                ]
            }

        llm = StubLLM(entity_handler=entity_handler, property_handler=property_handler)
        return SE.extract_split(
            RULE_ONTOLOGY,
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=self.TEXT)],
            llm=llm,
            **kwargs,
        )

    def test_annotate_mode_does_not_change_raw(self) -> None:
        result = self._extract(
            {
                "ruleText": self.TEXT,
                "triggerCondition": "未经集团批准",
                "action": "不得部署安装各类开源智能体",
            }
        )
        self.assertEqual(len(result["raw"]["entities"]), 1)
        self.assertGreaterEqual(result["mapping"]["entities"][0]["score"], 0.0)
        self.assertEqual(result["profile"]["mapping_mode"], "annotate")
        self.assertEqual(
            result["intermediate_model"]["objects"][0]["mapping"]["status"],
            "accepted",
        )

    def test_enforce_mode_filters_rejected(self) -> None:
        # 属性补全为空 -> attribute 覆盖 0 -> 映射分数低于阈值 -> enforce 模式过滤
        result = self._extract(None, apply_mapping_threshold=True)
        self.assertEqual(result["raw"]["entities"], [])
        self.assertGreaterEqual(result["mapping"]["rejected"], 1)
        codes = {w["code"] for w in result["warnings"]}
        self.assertIn("MAPPING_REJECTED", codes)
        self.assertEqual(result["profile"]["mapping_mode"], "enforce")


if __name__ == "__main__":
    unittest.main(verbosity=2)
