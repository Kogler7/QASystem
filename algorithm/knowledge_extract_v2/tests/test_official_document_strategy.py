"""Phase 1 Step 6 单元测试：OfficialDocument Strategy（§12）。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract_v2/tests/test_official_document_strategy.py

覆盖：
- §12.1 公文头部元数据确定性抽取（文件标题/来文单位/来文字号）；
- §12.2 DocumentUnit 先建立（条目标记 -> DU-xxx + HAS_PART）；
- Schema Authority / AC-2：只输出本体声明的类型/属性、遵守目标范围；
- §12.4 Exchange sourceAgent/targetAgent 强校验；
- §12.6 EVIDENCED_BY 覆盖审计（无漏报、无对已覆盖实体的误报）；
- §10 注册表与 prompt_hints；
- 与 extract_split 的集成（official-document-01 精确对齐 / 02 子集对齐）。
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
        "standard_strategy",
        "official_document_strategy",
        "split_extractor",
    ],
)
CC = MODULES["chunk_context"]
EV = MODULES["evidence"]
OC = MODULES["ontology_compiler"]
OD = MODULES["official_document_strategy"]
SE = MODULES["split_extractor"]


OFFICIAL_ONTOLOGY = (ONTOLOGIES_DIR / "official_document_ontology.puml").read_text(encoding="utf-8")


def case_text(case_id: str) -> str:
    return json.loads((CASES_DIR / f"{case_id}.json").read_text(encoding="utf-8"))["text"]


def gold(case_id: str) -> dict:
    return json.loads((CASES_DIR / f"{case_id}.json").read_text(encoding="utf-8"))["expected"]


class DeterministicExtractionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = OC.compile_plan(OFFICIAL_ONTOLOGY)
        self.strategy = OD.OfficialDocumentExtractionStrategy()

    def test_document_metadata(self) -> None:
        text = case_text("02_official_document")
        manager = CC.ChunkContextManager.from_text(text)
        request = CC.ExtractionRequest(ontology=OFFICIAL_ONTOLOGY, text=text, document_type="official_document")
        result = self.strategy.deterministic_extraction(self.plan, manager, request)
        documents = [entity for entity in result["entities"] if entity["type"] == "OfficialDocument"]
        self.assertEqual(len(documents), 1)
        document = documents[0]
        self.assertEqual(document["name"], "关于开展OpenClaw（“龙虾”）等开源智能体安全风险排查工作的通知")
        self.assertEqual(document["properties"]["documentTitle"], document["name"])
        self.assertEqual(document["properties"]["sourceOrganization"], "中国中车集团有限公司")
        self.assertEqual(document["properties"]["sourceDocumentNumber"], "中车集团办科信〔2026〕18号")
        self.assertIsNotNone(manager.find_span("chunk-0", document["evidence"]["quote"]))

    def test_document_units_match_gold_01(self) -> None:
        text = case_text("02_official_document")
        manager = CC.ChunkContextManager.from_text(text)
        request = CC.ExtractionRequest(ontology=OFFICIAL_ONTOLOGY, text=text, document_type="official_document")
        result = self.strategy.deterministic_extraction(self.plan, manager, request)
        units = [entity for entity in result["entities"] if entity["type"] == "DocumentUnit"]
        self.assertEqual([unit["name"] for unit in units], [f"DU-00{i}" for i in range(1, 6)])
        gold_units = [entity for entity in gold("02_official_document")["entities"] if entity["type"] == "DocumentUnit"]
        for deterministic, gold_unit in zip(units, gold_units):
            self.assertEqual(deterministic["name"], gold_unit["name"])
            self.assertEqual(deterministic["properties"]["unitType"], gold_unit["properties"]["unitType"])
            self.assertEqual(deterministic["properties"]["unitTitle"], gold_unit["properties"]["unitTitle"])
            self.assertEqual(deterministic["properties"]["unitOrder"], gold_unit["properties"]["unitOrder"])
            self.assertEqual(deterministic["properties"]["originalText"], gold_unit["properties"]["originalText"])
        has_part = [r for r in result["relations"] if r["rel_type"] == "HAS_PART"]
        self.assertEqual(len(has_part), 5)

    def test_respects_target_scope(self) -> None:
        plan = OC.compile_plan(OFFICIAL_ONTOLOGY, target_types=["OperationalActivity"])
        text = case_text("02_official_document")
        manager = CC.ChunkContextManager.from_text(text)
        request = CC.ExtractionRequest(ontology=OFFICIAL_ONTOLOGY, text=text, document_type="official_document")
        result = self.strategy.deterministic_extraction(plan, manager, request)
        self.assertEqual(result, {"entities": [], "relations": []})


class CoverageAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = OC.compile_plan(OFFICIAL_ONTOLOGY)
        self.strategy = OD.OfficialDocumentExtractionStrategy()
        self.chunk = CC.ExtractionChunk(chunk_id="c0", text="一、各企业于2026年4月10日前完成排查。")
        self.manager = CC.ChunkContextManager([self.chunk])
        self.request = CC.ExtractionRequest(ontology=OFFICIAL_ONTOLOGY, chunks=[self.chunk], document_type="official_document")

    def test_evidence_gap_and_incomplete_exchange(self) -> None:
        raw = {
            "entities": [
                {"type": "OperationalActivity", "name": "完成排查", "properties": {}, "evidence": {"quote": "完成排查"}},
                {"type": "OperationalExchange", "name": "EX-001", "properties": {}, "evidence": {"quote": "完成排查"}},
                {"type": "DocumentUnit", "name": "DU-001", "properties": {}, "evidence": {"quote": "一、各企业于2026年4月10日前完成排查。"}},
            ],
            "relations": [],
        }
        bound = EV.bind_evidence(raw, self.manager)
        gaps = self.strategy.coverage_audit(self.plan, self.manager, self.request, bound["entities"], [])
        gap_types = {gap.gap_type for gap in gaps}
        self.assertIn("evidence_coverage", gap_types)
        self.assertIn("incomplete_exchange", gap_types)

    def test_no_gap_when_evidenced_by_and_exchange_complete(self) -> None:
        raw = {
            "entities": [
                {"type": "OperationalActivity", "name": "完成排查", "properties": {}, "evidence": {"quote": "完成排查"}},
                {"type": "OperationalExchange", "name": "EX-001", "properties": {}, "evidence": {"quote": "完成排查"}},
                {"type": "OperationalAgent", "name": "各企业", "properties": {}, "evidence": {"quote": "各企业"}},
                {"type": "DocumentUnit", "name": "DU-001", "properties": {}, "evidence": {"quote": "一、各企业于2026年4月10日前完成排查。"}},
            ],
            "relations": [
                {"from_type": "OperationalActivity", "from_name": "完成排查", "rel_type": "EVIDENCED_BY", "to_type": "DocumentUnit", "to_name": "DU-001"},
                {"from_type": "OperationalAgent", "from_name": "各企业", "rel_type": "EVIDENCED_BY", "to_type": "DocumentUnit", "to_name": "DU-001"},
                {"from_type": "OperationalExchange", "from_name": "EX-001", "rel_type": "EVIDENCED_BY", "to_type": "DocumentUnit", "to_name": "DU-001"},
                {"from_type": "OperationalExchange", "from_name": "EX-001", "rel_type": "sourceAgent", "to_type": "OperationalAgent", "to_name": "各企业"},
                {"from_type": "OperationalExchange", "from_name": "EX-001", "rel_type": "targetAgent", "to_type": "OperationalAgent", "to_name": "各企业"},
            ],
        }
        bound = EV.bind_evidence(raw, self.manager)
        gaps = self.strategy.coverage_audit(self.plan, self.manager, self.request, bound["entities"], bound["relations"])
        gap_types = {gap.gap_type for gap in gaps}
        self.assertNotIn("evidence_coverage", gap_types)
        self.assertNotIn("incomplete_exchange", gap_types)

    def test_structural_gap_for_metadata_chunk(self) -> None:
        chunk = CC.ExtractionChunk(chunk_id="c1", text="一、总体思路", section_id="s1", section_title="总体思路")
        manager = CC.ChunkContextManager([chunk])
        request = CC.ExtractionRequest(ontology=OFFICIAL_ONTOLOGY, chunks=[chunk], document_type="official_document")
        gaps = self.strategy.coverage_audit(self.plan, manager, request, [], [])
        self.assertEqual([gap.gap_type for gap in gaps], ["structural_coverage"])


class RegistryAndHintsTest(unittest.TestCase):
    def test_registry_and_hints(self) -> None:
        strategy = OD.STRATEGY_REGISTRY["official_document"]
        self.assertEqual(strategy.document_type, "official_document")
        self.assertIn("OperationalActivity", strategy.prompt_hints())
        self.assertIn("EVIDENCED_BY", strategy.prompt_hints())


class IntegrationTest(unittest.TestCase):
    def _stub(self, expected: dict):
        canonical = MODULES["graph_utils"].canonical_name

        def call(prompt: str) -> str:
            if "实体候选提议" in prompt:
                payload = {"entities": expected["entities"]}
            elif "关系候选提议" in prompt:
                index = {}
                for match in re.finditer(r"\|\s*`(entity-\d+)`\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|", prompt):
                    index[(match.group(2), canonical(match.group(3).strip()))] = match.group(1)
                relations = []
                for relation in expected.get("relations", []):
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
                payload = {"relations": relations}
            else:
                raise AssertionError("无法识别的 prompt 段落")
            return json.dumps(payload, ensure_ascii=False)

        return call

    def test_official_document_01_gold_exact(self) -> None:
        case_id = "02_official_document"
        expected = gold(case_id)
        result = SE.extract_split(
            OFFICIAL_ONTOLOGY,
            text=case_text(case_id),
            llm=self._stub(expected),
            document_type="official_document",
        )
        canonical = MODULES["graph_utils"].canonical_name
        gold_entity_keys = {(e["type"], canonical(e["name"])) for e in expected["entities"]}
        pred_entity_keys = {(e["type"], canonical(e["name"])) for e in result["raw"]["entities"]}
        self.assertEqual(pred_entity_keys, gold_entity_keys)
        gold_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in expected["relations"]
        }
        pred_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in result["raw"]["relations"]
        }
        self.assertEqual(pred_relation_keys, gold_relation_keys)
        # 结构覆盖无 gap（DocumentUnit + HAS_PART 全部就位）
        self.assertNotIn("structural_coverage", {gap["gap_type"] for gap in result["coverage_gaps"]})
        # 审计不得对 gold 中已 EVIDENCED_BY 的实体误报
        covered = {
            (r["from_type"], canonical(r["from_name"]))
            for r in expected["relations"]
            if r["rel_type"] == "EVIDENCED_BY"
        }
        gap_entities = {
            (gap["target_type"], canonical(gap["evidence"]))
            for gap in result["coverage_gaps"]
            if gap["gap_type"] == "evidence_coverage"
        }
        self.assertTrue(gap_entities.isdisjoint(covered))

    def test_official_document_02_subset_and_units(self) -> None:
        case_id = "07_official_document"
        expected = gold(case_id)
        result = SE.extract_split(
            OFFICIAL_ONTOLOGY,
            text=case_text(case_id),
            llm=self._stub(expected),
            document_type="official_document",
        )
        canonical = MODULES["graph_utils"].canonical_name
        operational_types = set(OD.OPERATIONAL_TYPES)
        gold_op_keys = {
            (e["type"], canonical(e["name"]))
            for e in expected["entities"]
            if e["type"] in operational_types
        }
        pred_keys = {(e["type"], canonical(e["name"])) for e in result["raw"]["entities"]}
        self.assertLessEqual(gold_op_keys, pred_keys)
        gold_op_relations = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in expected["relations"]
            if r["from_type"] in operational_types and r["to_type"] in operational_types
        }
        pred_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in result["raw"]["relations"]
        }
        self.assertLessEqual(gold_op_relations, pred_relation_keys)
        # 确定性 DocumentUnit 已建立
        units = [e for e in result["raw"]["entities"] if e["type"] == "DocumentUnit"]
        self.assertGreaterEqual(len(units), 4)
        self.assertIn("OfficialDocument", {e["type"] for e in result["raw"]["entities"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
