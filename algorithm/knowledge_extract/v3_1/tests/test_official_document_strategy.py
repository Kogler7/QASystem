"""Current V3.1 domain routing and extraction integration regressions."""
from __future__ import annotations

import json
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


OFFICIAL_ONTOLOGY = (ONTOLOGIES_DIR / "official_document_ontology.puml").read_text(encoding="utf-8")


def case_text(case_id: str) -> str:
    return json.loads((CASES_DIR / f"{case_id}.json").read_text(encoding="utf-8"))["text"]


def gold(case_id: str) -> dict:
    return json.loads((CASES_DIR / f"{case_id}.json").read_text(encoding="utf-8"))["expected"]


class IntegrationTest(unittest.TestCase):
    """新管线集成：extract_split 不再调用策略的 deterministic_extraction /

    coverage_audit（旧接线与确定性实体补充已移除）。这里验证公文路由与原文名称抽取链路。"""

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

        llm = StubLLM(entity_handler=entity_handler, verification_handler=lambda item, chunk: 'yes')
        result = SE.extract_split(
            OFFICIAL_ONTOLOGY,
            chunks=[CC.ExtractionChunk(chunk_id="chunk-0", text=text)],
            llm=llm,
        )
        self.assertEqual(result["profile"]["document_route"]["kind"], "official_document")
        self.assertIsNone(result["error_code"])
        canonical = graph_utils.canonical_name
        pred_keys = {(e["type"], canonical(e["name"])) for e in result["raw"]["entities"]}
        emitted = {(type_id, canonical(name)) for type_id, name in verbatim}
        self.assertLessEqual(emitted, pred_keys)
        self.assertEqual(result["evidence_coverage"]["entities"]["rate"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
