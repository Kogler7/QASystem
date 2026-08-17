"""Phase 1 Step 5 单元测试：Standard Strategy（§10-§11）。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract_v2/tests/test_standard_strategy.py

覆盖：
- §11.1 确定性抽取：文档元数据、结构单元（消费 chunk metadata）、ICS/CCS；
- Schema Authority / AC-2：只输出本体声明的类型/属性、遵守目标范围；
- §11.4 覆盖审计：structural / constraint / action / reference /
  characteristic coverage（能发现 gap，也能在覆盖时保持干净）；
- §10 Strategy Registry 解析；
- 与 extract_split 的集成（确定性实体先入池、覆盖 gap 输出）。
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
        "split_extractor",
    ],
)
CC = MODULES["chunk_context"]
EV = MODULES["evidence"]
OC = MODULES["ontology_compiler"]
SS = MODULES["standard_strategy"]
SE = MODULES["split_extractor"]


STANDARD_ONTOLOGY = (ONTOLOGIES_DIR / "standard_ontology.puml").read_text(encoding="utf-8")

STANDARD_TEXT = (
    "GB/T 31486—2024《电动汽车用动力蓄电池电性能要求及试验方法》\n"
    "\n"
    "本标准规定了电动汽车用动力蓄电池的电性能要求及试验方法，于2024年9月29日发布，"
    "自2025年4月1日起实施，为推荐性标准，当前状态为现行。"
)


def structure_chunks(case: dict) -> list[dict]:
    """按标题行模拟上游结构元数据切片（Step 5 验收/集成测试用）。"""
    chunks: list[dict] = []
    current: dict | None = None
    for line in (case["text"] or "").splitlines():
        if not line.strip():
            continue
        match = re.match(r"^\s*(\d+(?:\.\d+){0,3})\s+(.+?)\s*$", line)
        heading = bool(match) and not re.fullmatch(r"[\d.\s]+", match.group(2))
        if heading:
            if current is not None:
                chunks.append(current)
            number, title = match.group(1), match.group(2)
            current = {
                "chunk_id": f"{case['id']}-{number}",
                "text": line,
                "document_id": case["id"],
                "clause_number": number,
                "section_title": title,
                "previous_chunk_id": chunks[-1]["chunk_id"] if chunks else None,
            }
        else:
            if current is None:
                current = {"chunk_id": f"{case['id']}-preamble", "text": "", "document_id": case["id"]}
            current["text"] = (current["text"] + "\n" if current["text"] else "") + line
    if current is not None:
        chunks.append(current)
    return chunks


class DeterministicExtractionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = OC.compile_plan(STANDARD_ONTOLOGY)
        self.manager = CC.ChunkContextManager.from_text(STANDARD_TEXT)
        self.request = CC.ExtractionRequest(
            ontology=STANDARD_ONTOLOGY,
            text=STANDARD_TEXT,
            document_type="standard",
        )
        self.strategy = SS.StandardExtractionStrategy()

    def test_standard_metadata(self) -> None:
        result = self.strategy.deterministic_extraction(self.plan, self.manager, self.request)
        standards = [entity for entity in result["entities"] if entity["type"] == "Standard"]
        self.assertEqual(len(standards), 1)
        standard = standards[0]
        self.assertEqual(standard["name"], "GB/T 31486—2024")
        self.assertEqual(standard["properties"]["standardNumber"], "GB/T 31486—2024")
        self.assertEqual(standard["properties"]["documentName"], "电动汽车用动力蓄电池电性能要求及试验方法")
        self.assertEqual(standard["properties"]["issuedDate"], "2024-09-29")
        self.assertEqual(standard["properties"]["effectiveDate"], "2025-04-01")
        self.assertEqual(standard["properties"]["status"], "现行")
        self.assertEqual(standard["properties"]["constraintType"], "推荐性")
        # evidence quote 必须可定位
        self.assertIsNotNone(self.manager.find_span("chunk-0", standard["evidence"]["quote"]))

    def test_structure_units_and_relations(self) -> None:
        chunks = [
            CC.ExtractionChunk(
                chunk_id="c0",
                text="GB/T 31486—2024《电动汽车用动力蓄电池电性能要求及试验方法》",
                document_id="doc",
            ),
            CC.ExtractionChunk(
                chunk_id="c5",
                text="5 性能要求",
                clause_number="5",
                section_title="性能要求",
                document_id="doc",
                previous_chunk_id="c0",
            ),
            CC.ExtractionChunk(
                chunk_id="c51",
                text="5.1 外观\n电池单体按6.2.1检验，外观应无变形及裂纹。",
                clause_number="5.1",
                section_title="外观",
                document_id="doc",
                previous_chunk_id="c5",
            ),
        ]
        manager = CC.ChunkContextManager(chunks)
        request = CC.ExtractionRequest(ontology=STANDARD_ONTOLOGY, chunks=chunks, document_type="standard")
        result = self.strategy.deterministic_extraction(self.plan, manager, request)
        types_by_name = {entity["name"]: entity["type"] for entity in result["entities"]}
        self.assertEqual(types_by_name.get("5"), "Section")
        self.assertEqual(types_by_name.get("5.1"), "Clause")
        # hasStructuralElement 只指向顶层（Section 5），不指向 5.1
        structural = [r for r in result["relations"] if r["rel_type"] == "hasStructuralElement"]
        self.assertEqual({(r["to_type"], r["to_name"]) for r in structural}, {("Section", "5")})
        # hasSubClause Section 5 -> Clause 5.1
        sub_clauses = [r for r in result["relations"] if r["rel_type"] == "hasSubClause"]
        self.assertEqual(
            {(r["from_name"], r["to_name"]) for r in sub_clauses},
            {("5", "5.1")},
        )

    def test_respects_target_scope(self) -> None:
        plan = OC.compile_plan(STANDARD_ONTOLOGY, target_types=["Standard"])
        result = self.strategy.deterministic_extraction(plan, self.manager, self.request)
        self.assertEqual({entity["type"] for entity in result["entities"]}, {"Standard"})
        self.assertEqual(result["relations"], [])

    def test_ontology_without_standard_class(self) -> None:
        terminology = """@startuml terminology
class "术语" as Terminology <<KnowledgeUnit>> {
  + 术语标准名称 (TermName): String
}
' @identity Terminology: TermName
@enduml
"""
        plan = OC.compile_plan(terminology)
        result = self.strategy.deterministic_extraction(plan, self.manager, self.request)
        self.assertEqual(result, {"entities": [], "relations": []})


class CoverageAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = OC.compile_plan(STANDARD_ONTOLOGY)
        self.strategy = SS.StandardExtractionStrategy()

    def test_gaps_detected_when_entities_missing(self) -> None:
        text = "电池单体按6.2.1检验，其初始容量不应低于额定容量，并符合制造商提供的产品技术条件。"
        chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
        manager = CC.ChunkContextManager([chunk])
        request = CC.ExtractionRequest(ontology=STANDARD_ONTOLOGY, chunks=[chunk], document_type="standard")
        gaps = self.strategy.coverage_audit(self.plan, manager, request, [], [])
        gap_types = {gap.gap_type for gap in gaps}
        self.assertIn("constraint_coverage", gap_types)
        self.assertIn("action_coverage", gap_types)
        self.assertIn("characteristic_coverage", gap_types)
        self.assertIn("reference_coverage", gap_types)
        self.assertTrue(all(gap.severity == "info" for gap in gaps))

    def test_no_gaps_when_entities_cover(self) -> None:
        text = "电池单体按6.2.1检验，其初始容量不应低于额定容量，并符合制造商提供的产品技术条件。"
        chunk = CC.ExtractionChunk(chunk_id="c0", text=text)
        manager = CC.ChunkContextManager([chunk])
        request = CC.ExtractionRequest(ontology=STANDARD_ONTOLOGY, chunks=[chunk], document_type="standard")
        raw = {
            "entities": [
                {"type": "Constraint", "name": "C", "properties": {}, "evidence": {"quote": "不应低于额定容量"}},
                {"type": "ActionClass", "name": "A", "properties": {}, "evidence": {"quote": "按6.2.1检验"}},
                {"type": "Property", "name": "P", "properties": {}, "evidence": {"quote": "初始容量"}},
                {"type": "ExternalResource", "name": "R", "properties": {}, "evidence": {"quote": "产品技术条件"}},
            ],
            "relations": [],
        }
        bound = EV.bind_evidence(raw, manager)
        gaps = self.strategy.coverage_audit(self.plan, manager, request, bound["entities"], [])
        self.assertEqual(gaps, [])

    def test_structural_gap(self) -> None:
        chunk = CC.ExtractionChunk(
            chunk_id="c1",
            text="5.1 外观",
            clause_number="5.1",
            section_title="外观",
        )
        manager = CC.ChunkContextManager([chunk])
        request = CC.ExtractionRequest(ontology=STANDARD_ONTOLOGY, chunks=[chunk], document_type="standard")
        gaps = self.strategy.coverage_audit(self.plan, manager, request, [], [])
        self.assertEqual([gap.gap_type for gap in gaps], ["structural_coverage"])


class RegistryTest(unittest.TestCase):
    def test_get_strategy_resolution(self) -> None:
        self.assertEqual(SS.get_strategy("standard").document_type, "standard")
        self.assertEqual(SS.get_strategy("unknown").document_type, "generic")
        self.assertEqual(SS.get_strategy(strategy="standard").document_type, "standard")
        self.assertEqual(SS.get_strategy(strategy="nope").document_type, "generic")


class IntegrationTest(unittest.TestCase):
    def test_extract_split_uses_standard_strategy(self) -> None:
        def empty_llm(prompt: str) -> str:
            key = "实体候选提议" if "实体候选提议" in prompt else "关系候选提议"
            return json.dumps({"entities": []} if key == "实体候选提议" else {"relations": []}, ensure_ascii=False)

        result = SE.extract_split(
            STANDARD_ONTOLOGY,
            text=STANDARD_TEXT + "\n电池单体按6.2.1检验，外观应无变形及裂纹。",
            document_type="standard",
            llm=empty_llm,
        )
        self.assertEqual(result["profile"]["strategy"], "standard")
        standards = [entity for entity in result["raw"]["entities"] if entity["type"] == "Standard"]
        self.assertEqual(len(standards), 1)
        self.assertEqual(standards[0]["name"], "GB/T 31486—2024")
        self.assertIn("standardNumber", standards[0]["properties"])
        # LLM 空输出时，覆盖审计应报告行动缺失（含“按6.2.1检验”行动句）
        gap_types = {gap["gap_type"] for gap in result["coverage_gaps"]}
        self.assertIn("action_coverage", gap_types)

    def test_standard_01_gold_roundtrip(self) -> None:
        case = json.loads((CASES_DIR / "01_standard.json").read_text(encoding="utf-8"))
        gold = case["expected"]
        chunks = [CC.ExtractionChunk.from_dict(payload) for payload in structure_chunks(case)]

        def entity_handler(prompt: str) -> dict:
            return {"entities": gold["entities"]}

        def relation_handler(prompt: str) -> dict:
            entries = {}
            for match in re.finditer(r"\|\s*`(entity-\d+)`\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|", prompt):
                canonical = MODULES["graph_utils"].canonical_name
                entries[(match.group(2), canonical(match.group(3).strip()))] = match.group(1)
            relations = []
            for relation in gold.get("relations", []):
                from_id = entries.get((relation["from_type"], canonical(relation["from_name"])))
                to_id = entries.get((relation["to_type"], canonical(relation["to_name"])))
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
            return {"relations": relations}

        def call(prompt: str) -> str:
            if "实体候选提议" in prompt:
                payload = entity_handler(prompt)
            elif "关系候选提议" in prompt:
                payload = relation_handler(prompt)
            else:
                raise AssertionError("无法识别的 prompt 段落")
            return json.dumps(payload, ensure_ascii=False)

        result = SE.extract_split(
            STANDARD_ONTOLOGY,
            chunks=chunks,
            llm=call,
            document_type="standard",
        )
        canonical = MODULES["graph_utils"].canonical_name
        gold_entity_keys = {(e["type"], canonical(e["name"])) for e in gold["entities"]}
        pred_entity_keys = {(e["type"], canonical(e["name"])) for e in result["raw"]["entities"]}
        self.assertEqual(pred_entity_keys, gold_entity_keys)
        gold_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in gold["relations"]
        }
        pred_relation_keys = {
            (r["from_type"], canonical(r["from_name"]), r["rel_type"], r["to_type"], canonical(r["to_name"]))
            for r in result["raw"]["relations"]
        }
        self.assertEqual(pred_relation_keys, gold_relation_keys)
        gap_types = {gap["gap_type"] for gap in result["coverage_gaps"]}
        self.assertNotIn("structural_coverage", gap_types)
        self.assertNotIn("constraint_coverage", gap_types)
        self.assertNotIn("action_coverage", gap_types)


if __name__ == "__main__":
    unittest.main(verbosity=2)
