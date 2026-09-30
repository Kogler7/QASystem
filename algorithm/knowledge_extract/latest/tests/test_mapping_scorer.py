"""Phase 1 Step 8 单元测试：Mapping Scorer（§17-§19）。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract/v3/tests/test_mapping_scorer.py

覆盖：
- §17 权重/阈值配置（归一化、非法阈值拒绝、权重影响总分）；
- §18 属性评分（覆盖/合法性/identity、required penalty 仅在本体声明时启用）；
- §19 关系参与评分、关系合法性/证据/cardinality 评分；
- §16 L4 上下文分量（本体未允许推断时不适用）；
- §34 状态边界（>=0.85 accepted / >=0.70 low / <0.70 rejected）；
- 与 extract_split 集成：默认注释模式不影响结果，enforce 模式按阈值过滤。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
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
    "knowledge_extract_latest",
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
        "mapping_scorer",
        "split_extractor",
    ],
)
MS = MODULES["mapping_scorer"]
OC = MODULES["ontology_compiler"]
SE = MODULES["split_extractor"]
CC = MODULES["chunk_context"]

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))  # 允许独立运行（python tests/test_mapping_scorer.py）

from algorithm.knowledge_extract.latest.tests.stub_llm import StubLLM, parse_known_entities, span_item  # noqa: E402


RULE_ONTOLOGY = (ONTOLOGIES_DIR / "rule_ontology.puml").read_text(encoding="utf-8")


def make_entity(plan: object, type_id: str, name: str, properties: dict) -> Any:
    return SE.EntityCandidate(
        candidate_id="entity-0",
        proposed_type=type_id,
        canonical_type=type_id,
        name=name,
        properties=properties,
        evidence=[],
        source="test",
    )


class ConfigTest(unittest.TestCase):
    def test_default_weights_normalized(self) -> None:
        scorer = MS.MappingScorer()
        self.assertAlmostEqual(sum(scorer.weights.values()), 1.0)
        self.assertEqual(scorer.accepted_threshold, 0.85)
        self.assertEqual(scorer.low_confidence_threshold, 0.70)

    def test_custom_weights_normalized(self) -> None:
        scorer = MS.MappingScorer(weights={"attribute": 2, "relation": 1})
        self.assertAlmostEqual(scorer.weights["attribute"], 2 / 3)
        self.assertAlmostEqual(scorer.weights["relation"], 1 / 3)
        self.assertAlmostEqual(scorer.weights["context"], 0.0)

    def test_invalid_thresholds_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MS.MappingScorer(accepted_threshold=0.7, low_confidence_threshold=0.85)

    def test_invalid_weights_rejected(self) -> None:
        for weights in (
            {"attribute": -1.0, "relation": 2.0},
            {"attribute": float("nan")},
            {"attribute": 1.0, "unknown": 1.0},
        ):
            with self.subTest(weights=weights), self.assertRaises(ValueError):
                MS.MappingScorer(weights=weights)

    def test_confidence_keeps_mapping_score_and_explains_signals(self) -> None:
        payload = MS.mapping_confidence_payload(
            0.8,
            "low_confidence",
            evidences=[
                {
                    "span_id": "chunk-0:0:4",
                    "source_block_ids": ["chunk-0"],
                    "quote": "示例文本",
                    "start_char": 0,
                    "end_char": 4,
                    "evidence_type": "explicit",
                }
            ],
            canonical_mapping_valid=True,
        )

        self.assertEqual(payload["mapping_score"], 0.8)
        self.assertLessEqual(payload["confidence"], payload["mapping_score"])
        self.assertEqual(payload["confidence_status"], "needs_review")
        self.assertEqual(payload["confidence_details"]["source"], "multi_signal")
        self.assertEqual(
            set(payload["confidence_details"]["signals"]),
            {"mapping", "evidence", "canonical_mapping"},
        )
        self.assertFalse(payload["confidence_details"]["calibrated"])

    def test_missing_evidence_and_invalid_schema_apply_gates(self) -> None:
        missing_evidence = MS.mapping_confidence_payload(
            0.99,
            "accepted",
            evidences=[],
            canonical_mapping_valid=True,
        )
        self.assertLess(missing_evidence["confidence"], 0.85)
        self.assertEqual(missing_evidence["confidence_status"], "needs_review")
        self.assertIn("missing_evidence", missing_evidence["confidence_details"]["gates"])

        invalid_schema = MS.mapping_confidence_payload(
            0.99,
            "accepted",
            evidences=[],
            canonical_mapping_valid=False,
        )
        self.assertEqual(invalid_schema["confidence"], 0.0)
        self.assertEqual(invalid_schema["confidence_status"], "rejected")
        self.assertIn(
            "canonical_mapping_missing",
            invalid_schema["confidence_details"]["gates"],
        )

    def test_relation_can_exclude_correlated_numeric_signals_but_keep_gates(self) -> None:
        payload = MS.mapping_confidence_payload(
            0.82,
            "low_confidence",
            evidences=[
                {
                    "span_id": "chunk-0:0:4",
                    "source_block_ids": ["chunk-0"],
                    "quote": "示例文本",
                    "start_char": 0,
                    "end_char": 4,
                }
            ],
            canonical_mapping_valid=True,
            aggregate_evidence=False,
            aggregate_canonical_mapping=False,
        )

        self.assertEqual(payload["confidence"], 0.82)
        self.assertEqual(set(payload["confidence_details"]["signals"]), {"mapping"})
        self.assertEqual(payload["confidence_status"], "needs_review")
        self.assertIn("mapping_low_confidence", payload["confidence_details"]["gates"])

    def test_equal_confidence_merge_has_deterministic_detail_tie_break(self) -> None:
        first = {
            "confidence": 0.8,
            "confidence_details": {"method": "z", "status": "accepted"},
        }
        second = {
            "confidence": 0.8,
            "confidence_details": {"method": "a", "status": "accepted"},
        }

        forward = MS.merge_confidence_payload(first, second)
        reverse = MS.merge_confidence_payload(second, first)

        self.assertEqual(forward, reverse)
        self.assertEqual(forward["confidence_details"]["method"], "a")

    def test_three_way_equal_confidence_merge_is_order_independent(self) -> None:
        observations = [
            {
                "confidence": 0.8,
                "confidence_details": {"method": method, "status": "accepted"},
            }
            for method in ("c", "a", "b")
        ]
        results = []
        for ordered in permutations(observations):
            merged = ordered[0]
            for observation in ordered[1:]:
                merged = MS.merge_confidence_payload(merged, observation)
            results.append(merged)

        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(results[0]["confidence_details"]["method"], "a")
        self.assertEqual(results[0]["confidence_details"]["source_scores"], [0.8, 0.8, 0.8])
        self.assertEqual(results[0]["confidence_details"]["observation_count"], 3)

    def test_merge_preserves_strongest_status_and_union_of_gates(self) -> None:
        passed = {
            "confidence": 0.8,
            "confidence_status": "accepted",
            "gate_status": "passed",
            "confidence_details": {"method": "z", "gates": []},
        }
        blocked = {
            "confidence": 0.9,
            "confidence_status": "rejected",
            "gate_status": "blocked",
            "confidence_details": {
                "method": "a",
                "gates": ["schema_invalid", "final_verification_failed"],
            },
        }

        forward = MS.merge_confidence_payload(passed, blocked)
        reverse = MS.merge_confidence_payload(blocked, passed)

        self.assertEqual(forward, reverse)
        self.assertEqual(forward["confidence"], 0.8)
        self.assertEqual(forward["confidence_status"], "rejected")
        self.assertEqual(forward["gate_status"], "blocked")
        self.assertEqual(
            forward["confidence_details"]["gates"],
            ["final_verification_failed", "schema_invalid"],
        )

    def test_merge_status_precedence_is_monotonic_across_repeated_merges(self) -> None:
        observations = [
            {
                "confidence": 0.95,
                "confidence_status": "accepted",
                "gate_status": "passed",
                "confidence_details": {"gates": []},
            },
            {
                "confidence": 0.9,
                "confidence_status": "needs_review",
                "gate_status": "review",
                "confidence_details": {"gates": ["missing_evidence"]},
            },
            {
                "confidence": 0.85,
                "confidence_status": "rejected",
                "gate_status": "blocked",
                "confidence_details": {"gates": ["schema_invalid"]},
            },
        ]
        results = []
        for ordered in permutations(observations):
            merged = ordered[0]
            for observation in ordered[1:]:
                merged = MS.merge_confidence_payload(merged, observation)
            results.append(merged)

        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(results[0]["confidence_status"], "rejected")
        self.assertEqual(results[0]["gate_status"], "blocked")
        self.assertEqual(
            results[0]["confidence_details"]["gates"],
            ["missing_evidence", "schema_invalid"],
        )

    def test_equal_confidence_uses_lower_mapping_score_independent_of_order(self) -> None:
        lower_mapping = {
            "mapping_score": 0.7,
            "confidence": 0.7,
            "confidence_details": {"method": "same"},
        }
        higher_mapping = {
            "mapping_score": 0.9,
            "confidence": 0.7,
            "confidence_details": {"method": "same"},
        }

        forward = MS.merge_confidence_payload(lower_mapping, higher_mapping)
        reverse = MS.merge_confidence_payload(higher_mapping, lower_mapping)

        self.assertEqual(forward, reverse)
        self.assertEqual(forward["mapping_score"], 0.7)


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
        feature_specs = self.plan.feature_specs
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

    def test_relation_participation(self) -> None:
        entity = make_entity(
            self.plan,
            "CompositeRule",
            "CR-001",
            {"ruleId": "CR-001", "ruleText": "t", "combinationType": "AND"},
        )
        score = self.scorer.score_entity(entity, self.plan)
        self.assertEqual(score.breakdown["relation"], 0.0)  # 无关系参与
        # 模拟参与 hasSubRule
        fake_relation = types.SimpleNamespace(
            from_candidate_id="entity-0",
            canonical_relation="hasSubRule",
            proposed_relation="hasSubRule",
        )
        score_with = self.scorer.score_entity(entity, self.plan, [fake_relation])
        self.assertEqual(score_with.breakdown["relation"], 1.0)

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


class RelationScoreTest(unittest.TestCase):
    def test_relation_score_evidence_legality_cardinality(self) -> None:
        plan = OC.compile_plan(RULE_ONTOLOGY)
        scorer = MS.MappingScorer()
        full = SE.RelationCandidate(
            candidate_id="relation-0",
            from_candidate_id="entity-0",
            to_candidate_id="entity-1",
            proposed_relation="hasSubRule",
            canonical_relation="hasSubRule",
            evidence=["e"],
            from_name="CR-001",
            to_name="R-001",
            from_type="CompositeRule",
            to_type="SimpleRule",
        )
        score = scorer.score_relation(full, plan)
        self.assertEqual(score.score, 1.0)
        self.assertEqual(score.status, "accepted")

        no_evidence = SE.RelationCandidate(
            candidate_id="relation-1",
            from_candidate_id="entity-0",
            to_candidate_id="entity-1",
            proposed_relation="hasSubRule",
            canonical_relation="hasSubRule",
            evidence=[],
            from_name="CR-001",
            to_name="R-001",
            from_type="CompositeRule",
            to_type="SimpleRule",
        )
        score = scorer.score_relation(no_evidence, plan)
        self.assertAlmostEqual(score.score, 0.5 * 0 + 0.3 * 1.0 + 0.2 * 1.0)

        cardinality_violation = SE.RelationCandidate(
            candidate_id="relation-2",
            from_candidate_id="entity-0",
            to_candidate_id="entity-1",
            proposed_relation="hasSubRule",
            canonical_relation="hasSubRule",
            evidence=["e"],
            warnings=["RELATION_CARDINALITY_EXCEEDED"],
            from_name="CR-001",
            to_name="R-001",
            from_type="CompositeRule",
            to_type="SimpleRule",
        )
        score = scorer.score_relation(cardinality_violation, plan)
        self.assertAlmostEqual(score.score, 0.5 * 1.0 + 0.3 * 1.0 + 0.2 * 0.5)


class IntegrationTest(unittest.TestCase):
    TEXT = "未经集团批准，不得部署安装各类开源智能体。"

    def _extract(self, properties: dict | None, **kwargs):
        def entity_handler(prompt: str, chunk: dict) -> list[dict]:
            return [span_item("SimpleRule", self.TEXT)]

        def property_handler(prompt: str, chunk: dict) -> dict:
            if not properties:
                return {"updates": []}
            return {
                "updates": [
                    {
                        "entity_id": row["id"],
                        "properties": dict(properties),
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
            identity_admission="observe",
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
        entity = result["raw"]["entities"][0]
        self.assertEqual(entity["mapping_score"], result["mapping"]["entities"][0]["score"])
        self.assertEqual(entity["confidence_status"], "rejected")
        self.assertEqual(entity["confidence_details"]["source"], "multi_signal")
        self.assertEqual(
            entity["confidence_details"]["method"],
            "mapping_bounded_multi_signal_v3",
        )
        self.assertFalse(entity["confidence_details"]["calibrated"])
        self.assertEqual(
            set(entity["confidence_details"]["signals"]),
            {"mapping", "evidence", "canonical_mapping"},
        )
        self.assertEqual(
            result["mapping"]["accepted"]
            + result["mapping"]["low_confidence"]
            + result["mapping"]["rejected"],
            len(result["mapping"]["entities"]) + len(result["mapping"]["relations"]),
        )
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
