"""第一步入口护栏测试：空本体失败、chunk 容错与元数据补全、点分 UID 解析。

运行（无需 openai / pytest）：
    python algorithm/knowledge_extract_v2/tests/test_intake_guardrails.py
"""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = PACKAGE_DIR / "tests"
for path in (TESTS_DIR, PACKAGE_DIR.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _load_package_modules(package_name: str, directory: Path, stems: list[str]) -> dict[str, types.ModuleType]:
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
        "chunk_metadata",
        "chunk_context",
        "evidence",
        "graph_assembler",
        "prompt_router",
        "relation_resolver",
        "candidate_generation",
        "run_state",
        "llm_client",
        "split_extractor",
        "harness_extractor",
    ],
)
SE = MODULES["split_extractor"]
CC = MODULES["chunk_context"]
CM = MODULES["chunk_metadata"]
RS = MODULES["run_state"]
OS = MODULES["ontology_schema"]
HE = MODULES["harness_extractor"]

from stub_llm import StubLLM  # noqa: E402


SIMPLE_ONTOLOGY = '@startuml\nclass "术语" as Terminology\n@enduml'


def chunk(chunk_id: str, text: str, **kwargs) -> "CC.ExtractionChunk":
    return CC.ExtractionChunk(chunk_id=chunk_id, text=text, **kwargs)


class OntologyFailureTest(unittest.TestCase):
    """空本体 / 零类 / 零目标范围必须在调用 LLM 前结构化失败。"""

    def test_empty_ontology_permissive_structured_failure(self) -> None:
        llm = StubLLM()
        result = SE.extract_split(
            "",
            chunks=[chunk("c0", "第一条 为了规范管理，制定本办法。")],
            llm=llm,
            validation_mode="permissive",
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "ONTOLOGY_EMPTY")
        self.assertFalse(result["verification"]["valid"])
        self.assertEqual(result["verification"]["hard_issue_count"], 1)
        self.assertEqual(llm.calls, [])
        self.assertEqual(result["raw"], {"entities": [], "relations": []})
        self.assertTrue(result.get("_error"))
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("ONTOLOGY_EMPTY", codes)

    def test_zero_class_ontology_permissive_structured_failure(self) -> None:
        llm = StubLLM()
        result = SE.extract_split(
            "这不是 PlantUML 文本",
            chunks=[chunk("c0", "第一条 为了规范管理，制定本办法。")],
            llm=llm,
            validation_mode="permissive",
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "ONTOLOGY_EMPTY")
        self.assertFalse(result["verification"]["valid"])
        self.assertEqual(llm.calls, [])

    def test_unknown_target_types_permissive_structured_failure(self) -> None:
        llm = StubLLM()
        result = SE.extract_split(
            SIMPLE_ONTOLOGY,
            chunks=[chunk("c0", "术语的定义。")],
            target_types=["不存在的类"],
            llm=llm,
            validation_mode="permissive",
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "TARGET_SCOPE_EMPTY")
        self.assertFalse(result["verification"]["valid"])
        self.assertEqual(llm.calls, [])

    def test_strict_mode_raises_typed_error(self) -> None:
        with self.assertRaises(RS.ExtractionValidationError) as captured:
            SE.extract_split(
                "",
                chunks=[chunk("c0", "正文")],
                llm=StubLLM(),
            )
        self.assertEqual(captured.exception.error_code, "ONTOLOGY_EMPTY")
        self.assertEqual(captured.exception.stage, "ontology_compile")

    def test_harness_empty_ontology_raises_typed_error(self) -> None:
        with self.assertRaises(RS.ExtractionValidationError) as captured:
            HE.extract_harness("", [chunk("c0", "正文")], llm=StubLLM())
        self.assertEqual(captured.exception.error_code, "ONTOLOGY_EMPTY")


class ChunkIntakeTest(unittest.TestCase):
    """上游 chunk 宽容接收：dict 直通、重复 id 改名、空白 chunk 跳过。"""

    def test_dict_chunks_accepted_and_missing_id_generated(self) -> None:
        result = SE.extract_split(
            SIMPLE_ONTOLOGY,
            chunks=[{"text": "术语定义。"}, {"chunk_id": "x1", "text": "另一个术语。"}],
            llm=StubLLM(),
            validation_mode="permissive",
        )
        self.assertEqual(result["status"], "succeeded")
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("CHUNK_ID_GENERATED", codes)

    def test_invalid_chunk_inputs_rejected(self) -> None:
        with self.assertRaises(TypeError):
            SE.extract_split(SIMPLE_ONTOLOGY, chunks="完整文本", llm=StubLLM())
        with self.assertRaises(TypeError):
            SE.extract_split(SIMPLE_ONTOLOGY, chunks=[{"chunk_id": "c0"}], llm=StubLLM())
        with self.assertRaises(TypeError):
            SE.extract_split(SIMPLE_ONTOLOGY, chunks=[42], llm=StubLLM())

    def test_duplicate_chunk_id_renamed(self) -> None:
        first = chunk("c1", "第一条 内容一。")
        second = chunk("c1", "第二条 内容二。")
        result = SE.extract_split(
            SIMPLE_ONTOLOGY,
            chunks=[first, second],
            llm=StubLLM(),
            validation_mode="permissive",
        )
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("DUPLICATE_CHUNK_ID", codes)
        self.assertEqual(first.chunk_id, "c1")
        self.assertEqual(second.chunk_id, "c1")
        self.assertEqual(result["profile"]["chunks"], 2)

    def test_intake_does_not_mutate_chunk_or_nested_metadata(self) -> None:
        original = chunk("c0", "## 第一章 总则\n第一条 内容。", metadata={"nested": {"source": "upstream"}})
        before = original.to_dict()
        result = SE.extract_split(
            SIMPLE_ONTOLOGY,
            chunks=[original, chunk("c0", "第二条 内容。")],
            llm=StubLLM(),
            validation_mode="permissive",
        )
        self.assertEqual(original.to_dict(), before)
        self.assertIn("DUPLICATE_CHUNK_ID", [warning["code"] for warning in result["warnings"]])

    def test_malformed_boundaries_are_structured_warnings(self) -> None:
        result = SE.extract_split(
            SIMPLE_ONTOLOGY,
            chunks=[
                chunk("a", "第一段", document_id="doc", start_offset=10, end_offset=2),
                chunk("b", "第二段", document_id="doc", start_offset=1, previous_chunk_id="missing"),
            ],
            llm=StubLLM(),
            validation_mode="permissive",
        )
        codes = {warning["code"] for warning in result["warnings"]}
        self.assertIn("INVALID_CHUNK_SPAN", codes)
        self.assertIn("CHUNK_OFFSET_REGRESSION", codes)
        self.assertIn("CHUNK_REFERENCE_MISSING", codes)
        self.assertIn("chunk_contract", result["profile"])

    def test_blank_chunks_skipped_without_llm(self) -> None:
        result = SE.extract_split(
            SIMPLE_ONTOLOGY,
            chunks=[chunk("c0", "术语定义。"), chunk("c-blank", "   ")],
            llm=StubLLM(),
            validation_mode="permissive",
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["profile"]["scannable_chunks"], 1)
        self.assertEqual(result["profile"]["skipped_chunks"], 1)
        ledger = {entry["block_id"]: entry for entry in result["run_trace"]["coverage_ledger"]}
        self.assertFalse(ledger["c-blank"]["entity_scan_complete"])
        self.assertFalse(ledger["c-blank"]["relation_scan_complete"])

    def test_all_blank_chunks_reported_empty(self) -> None:
        result = SE.extract_split(
            SIMPLE_ONTOLOGY,
            chunks=[chunk("c0", "  "), chunk("c1", "")],
            llm=StubLLM(),
            validation_mode="permissive",
        )
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertIn("EMPTY_INPUT", codes)
        self.assertEqual(result["profile"]["scannable_chunks"], 0)
        self.assertEqual(result["profile"]["chunks"], 2)


class MetadataDerivationTest(unittest.TestCase):
    """缺失的 document/section/clause 元数据从文本结构确定性派生。"""

    def test_derives_document_section_clause(self) -> None:
        chunks = [
            chunk("c0", "开头无标题的导语。"),
            chunk("c1", "## 第一章 总则\n第一条 为了规范管理，制定本办法。", start_offset=10),
            chunk("c2", "第二条 本办法适用于公司各单位。", start_offset=80),
        ]
        counts = CM.enrich_chunk_metadata(chunks)
        self.assertEqual(chunks[0].document_id, "document-0001")
        self.assertEqual(chunks[0].section_id, "document-0001::s000")
        self.assertTrue(chunks[1].section_id.endswith("::s001"))
        self.assertEqual(chunks[1].section_title, "第一章 总则")
        self.assertEqual(chunks[1].clause_number, "第一条")
        self.assertEqual(chunks[2].clause_number, "第二条")
        self.assertNotEqual(chunks[1].section_id, chunks[2].section_id)
        self.assertEqual(counts["documents_defaulted"], 1)
        self.assertEqual(counts["clause_numbers_derived"], 2)

    def test_offset_regression_starts_new_default_document(self) -> None:
        chunks = [
            chunk("c0", "## 第一章 甲\n第一条 内容甲。", start_offset=10),
            chunk("c1", "## 第一章 乙\n第一条 内容乙。", start_offset=0),
        ]
        CM.enrich_chunk_metadata(chunks)
        self.assertNotEqual(chunks[0].document_id, chunks[1].document_id)
        self.assertTrue(chunks[0].section_id.startswith(chunks[0].document_id))
        self.assertTrue(chunks[1].section_id.startswith(chunks[1].document_id))

    def test_existing_metadata_preserved(self) -> None:
        original = chunk(
            "c0",
            "## 第一章 总则\n第一条 内容。",
            document_id="doc-a",
            section_id="doc-a::sec-9",
            section_title="既定标题",
            clause_number="既定条号",
        )
        CM.enrich_chunk_metadata([original])
        self.assertEqual(original.document_id, "doc-a")
        self.assertEqual(original.section_id, "doc-a::sec-9")
        self.assertEqual(original.section_title, "既定标题")
        self.assertEqual(original.clause_number, "既定条号")

    def test_clause_in_body_after_heading(self) -> None:
        derived = CM._heading_of("## 第一章 总则\n第一条 为了规范管理。")
        self.assertEqual(derived, (2, "第一章 总则", "第一条"))

    def test_no_false_positive_on_year_line(self) -> None:
        self.assertIsNone(CM._heading_of("2023 年数据统计如下。"))

    def test_structureless_document_keeps_section_none(self) -> None:
        chunks = [
            chunk("c0", "未经本企业充分论证和安全测试，不得部署安装各类开源智能体。"),
            chunk("c1", "本通知即日起施行。", start_offset=100),
        ]
        counts = CM.enrich_chunk_metadata(chunks)
        self.assertEqual(chunks[0].document_id, "document-0001")
        self.assertIsNone(chunks[0].section_id)
        self.assertIsNone(chunks[1].section_id)
        self.assertEqual(counts["section_ids_derived"], 0)


class DottedUidResolutionTest(unittest.TestCase):
    """插件元类 UID（点分 key）解析与歧义保护。"""

    def test_dotted_uid_resolves_to_simple_symbol(self) -> None:
        schema = OS.OntologySchema.parse(SIMPLE_ONTOLOGY)
        resolved = schema.resolve_class("公文本体元模型.术语")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.identifier, "Terminology")

    def test_exact_symbol_still_wins(self) -> None:
        schema = OS.OntologySchema.parse(SIMPLE_ONTOLOGY)
        self.assertEqual(schema.resolve_class("Terminology").identifier, "Terminology")

    def test_unknown_symbol_not_matched(self) -> None:
        schema = OS.OntologySchema.parse(SIMPLE_ONTOLOGY)
        self.assertIsNone(schema.resolve_class("其他包.不存在"))

    def test_ambiguous_ontology_symbol_not_silently_picked(self) -> None:
        schema = OS.OntologySchema.parse('@startuml\nclass "a.b.Thing" as T1\n@enduml')
        # 查询为简单名、本体符号带点且无歧义时可回退；有歧义（多个类后缀相同）时为 None。
        self.assertEqual(schema.resolve_class("Thing").identifier, "T1")
        ambiguous = OS.OntologySchema.parse(
            '@startuml\nclass "x.Kind" as K1\nclass "y.Kind" as K2\n@enduml'
        )
        self.assertIsNone(ambiguous.resolve_class("Kind"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
