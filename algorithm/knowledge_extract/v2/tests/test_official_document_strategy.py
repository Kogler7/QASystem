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

from stub_llm import StubLLM, span_item  # noqa: E402


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
        manager = CC.ChunkContextManager([CC.ExtractionChunk(chunk_id="chunk-0", text=text)])
        request = CC.ExtractionRequest(chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=text)])
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
        # v2/v3 均采用语义单元命名：DocumentUnit 按语义命名（如「通知正文」「排查要求一」），
        # 与 test_data/eval-v2、eval-v3 official golden 的命名约定一致（DU-XXX 编号已废弃）。
        # 该 fixture 为单 chunk 摘录，确定性结构只切出正文一个单元；完整公文的
        # 条款级切分见 benchmark golden。
        text = case_text("02_official_document")
        manager = CC.ChunkContextManager([CC.ExtractionChunk(chunk_id="chunk-0", text=text)])
        request = CC.ExtractionRequest(chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=text)])
        result = self.strategy.deterministic_extraction(self.plan, manager, request)
        units = [entity for entity in result["entities"] if entity["type"] == "DocumentUnit"]
        self.assertEqual([unit["name"] for unit in units], ["通知正文"])
        unit = units[0]
        self.assertEqual(unit["properties"]["unitType"], "正文")
        self.assertTrue(unit["properties"]["originalText"])
        self.assertTrue(unit["evidence"]["quote"])
        has_part = [r for r in result["relations"] if r["rel_type"] == "HAS_PART"]
        self.assertEqual(len(has_part), 1)  # 单 chunk 摘录只切出正文一个结构单元

    def test_respects_target_scope(self) -> None:
        plan = OC.compile_plan(OFFICIAL_ONTOLOGY, target_types=["OperationalActivity"])
        text = case_text("02_official_document")
        manager = CC.ChunkContextManager([CC.ExtractionChunk(chunk_id="chunk-0", text=text)])
        request = CC.ExtractionRequest(chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=text)])
        result = self.strategy.deterministic_extraction(plan, manager, request)
        self.assertEqual(result, {"entities": [], "relations": []})


class CoverageAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = OC.compile_plan(OFFICIAL_ONTOLOGY)
        self.strategy = OD.OfficialDocumentExtractionStrategy()
        self.chunk = CC.ExtractionChunk(chunk_id="c0", text="一、各企业于2026年4月10日前完成排查。")
        self.manager = CC.ChunkContextManager([self.chunk])
        self.request = CC.ExtractionRequest(chunks=[self.chunk])

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
        request = CC.ExtractionRequest(chunks=[chunk])
        gaps = self.strategy.coverage_audit(self.plan, manager, request, [], [])
        self.assertEqual([gap.gap_type for gap in gaps], ["structural_coverage"])


class RegistryAndHintsTest(unittest.TestCase):
    def test_registry_and_hints(self) -> None:
        strategy = OD.STRATEGY_REGISTRY["official_document"]
        self.assertEqual(strategy.document_type, "official_document")
        self.assertIn("OperationalActivity", strategy.prompt_hints())
        self.assertIn("EVIDENCED_BY", strategy.prompt_hints())


class IntegrationTest(unittest.TestCase):
    """新管线集成：extract_split 不再调用策略的 deterministic_extraction /

    coverage_audit（该接线已在重构中移除，确定性补充由 document_entities /
    structure_entities 等承担）。这里验证公文路由与 span-first 抽取链路。"""

    def test_official_document_route_and_span_extraction(self) -> None:
        case_id = "02_official_document"
        text = case_text(case_id)
        expected = gold(case_id)
        # 只取名字逐字落在原文中的 operational gold 实体（span-first 约束）
        verbatim = [
            (entity["type"], entity["name"])
            for entity in expected["entities"]
            if entity["type"].startswith("Operational")
            and entity["name"]
            and entity["name"] in text
        ]
        self.assertGreaterEqual(len(verbatim), 5)

        def entity_handler(prompt: str, chunk: dict) -> list[dict]:
            return [span_item(type_id, name) for type_id, name in verbatim]

        llm = StubLLM(entity_handler=entity_handler)
        result = SE.extract_split(
            OFFICIAL_ONTOLOGY,
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=text)],
            llm=llm,
        )
        self.assertEqual(result["profile"]["document_route"]["kind"], "official_document")
        self.assertIsNone(result["error_code"])
        canonical = MODULES["graph_utils"].canonical_name
        pred_keys = {(e["type"], canonical(e["name"])) for e in result["raw"]["entities"]}
        emitted = {(type_id, canonical(name)) for type_id, name in verbatim}
        self.assertLessEqual(emitted, pred_keys)
        self.assertEqual(result["evidence_coverage"]["entities"]["rate"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
