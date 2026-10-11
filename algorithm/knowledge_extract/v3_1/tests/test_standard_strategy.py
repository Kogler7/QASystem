"""Current V3.1 domain routing and extraction integration regressions."""
from __future__ import annotations

import json
import re
import unittest
from .. import graph_utils
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = PACKAGE_DIR / "tests" / "fixtures"
ONTOLOGIES_DIR = FIXTURES_DIR / "ontologies"
CASES_DIR = FIXTURES_DIR / "cases"
TESTS_DIR = PACKAGE_DIR / "tests"


from .. import chunk_context as CC
from .. import split_extractor as SE

from .stub_llm import StubLLM, span_item  # noqa: E402


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


class IntegrationTest(unittest.TestCase):
    """新管线集成：extract_split 不再调用策略 deterministic_extraction（该接线

    已在重构中移除），profile 也不再有 strategy 字段，改为 document_route。"""

    def test_extract_split_routes_standard_document(self) -> None:
        result = SE.extract_split(
            STANDARD_ONTOLOGY,
            chunks=[
                CC.ExtractionChunk(
                    chunk_id="chunk-0",
                    text=STANDARD_TEXT + "\n电池单体按6.2.1检验，外观应无变形及裂纹。",
                )
            ],
            llm=StubLLM(),
        )
        self.assertEqual(result["profile"]["document_route"]["kind"], "standard")
        self.assertIsNone(result["error_code"])
        # 空 LLM 输出 -> 无实体，但契约字段齐全
        self.assertEqual(result["raw"], {"entities": [], "relations": []})
        for key in ("graph", "triples", "intermediate_model", "coverage_gaps", "mapping"):
            self.assertIn(key, result)

    def test_standard_01_verbatim_gold_entities(self) -> None:
        # gold 中编号/名称逐字落在原文的实体（span-first 只能产出这些；
        # 确定性结构关系组装依赖封面/规范性引用结构，此处不覆盖）。
        # ExternalResource 被 CandidateSelector 要求 ontology 关系支撑
        # （referencesExternalResource 属确定性关系、不进入 pair 枚举，
        # 无支撑关系时以 CANDIDATE_UNSUPPORTED_INFORMATION 拒绝），故排除。
        case = json.loads((CASES_DIR / "01_standard.json").read_text(encoding="utf-8"))
        gold = case["expected"]
        chunks = [CC.ExtractionChunk.from_dict(payload) for payload in structure_chunks(case)]
        verbatim = [
            (entity["type"], entity["name"])
            for entity in gold["entities"]
            if entity["name"]
            and entity["type"] != "ExternalResource"
            and any(entity["name"] in chunk.text for chunk in chunks)
        ]
        self.assertGreaterEqual(len(verbatim), 10)

        def entity_handler(prompt: str, chunk: dict) -> list[dict]:
            text = chunk.get("text") or ""
            return [span_item(type_id, name) for type_id, name in verbatim if name in text]

        llm = StubLLM(entity_handler=entity_handler)
        result = SE.extract_split(STANDARD_ONTOLOGY, chunks=chunks, llm=llm)
        self.assertIsNone(result["error_code"])
        canonical = graph_utils.canonical_name
        pred_keys = {(e["type"], canonical(e["name"])) for e in result["raw"]["entities"]}
        emitted = {(type_id, canonical(name)) for type_id, name in verbatim}
        self.assertLessEqual(emitted, pred_keys)
        codes = [warning["code"] for warning in result["warnings"]]
        self.assertNotIn("ENTITY_OUT_OF_SCOPE", codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
