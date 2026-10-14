"""relation_only：golden/seed 实体 → 纯 LLM 关系，跳过 Stage1 与确定性补充。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from algorithm.knowledge_extract.v2_2_v31.chunk_context import ExtractionChunk
from algorithm.knowledge_extract.v2_2_v31.split_extractor import extract_split
from algorithm.knowledge_extract.v2_2_v31.tests.stub_llm import StubLLM


ONTOLOGY = """@startuml
class Person {
  + name: String
}
class Organization {
  + name: String
}
Person --> Organization : worksAt
@enduml
"""

TEXT = "张三就职于示例单位。"


def _support_works_at(prompt: str, pairs: list[dict], chunk: dict) -> list[dict]:
    return [
        {
            "pair_id": pair["pair_id"],
            "label": pair["relation_id"],
            "evidence": {"quote": TEXT},
        }
        for pair in pairs
        if pair["relation_id"] == "worksat"
    ]


class RelationOnlyExtractTest(unittest.TestCase):
    def test_requires_nonempty_seed_entities(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            extract_split(
                ONTOLOGY,
                chunks=[ExtractionChunk(chunk_id="c0", text=TEXT)],
                llm=StubLLM(),
                relation_only=True,
                seed_entities=[],
                max_repair_rounds=0,
                clef=False,
            )
        self.assertIn("seed_entities", str(ctx.exception))

    def test_seed_entities_require_relation_only(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            extract_split(
                ONTOLOGY,
                chunks=[ExtractionChunk(chunk_id="c0", text=TEXT)],
                llm=StubLLM(),
                relation_only=False,
                seed_entities=[{"type": "Person", "name": "张三", "properties": {}}],
                max_repair_rounds=0,
                clef=False,
            )
        self.assertIn("relation_only", str(ctx.exception))

    def test_skips_entity_stage_and_structural_assembly(self) -> None:
        seed = [
            {"type": "Person", "name": "张三", "properties": {"name": "张三"}},
            {"type": "Organization", "name": "示例单位", "properties": {"name": "示例单位"}},
        ]
        llm = StubLLM(relation_handler=_support_works_at)
        with (
            patch(
                "algorithm.knowledge_extract.v2_2_v31.split_extractor.run_entity_stage"
            ) as run_entity,
            patch(
                "algorithm.knowledge_extract.v2_2_v31.split_extractor.enrich_document_entities"
            ) as enrich_docs,
            patch(
                "algorithm.knowledge_extract.v2_2_v31.split_extractor.assemble_document_relations"
            ) as assemble_docs,
            patch(
                "algorithm.knowledge_extract.v2_2_v31.split_extractor.CompositeElementHandler"
            ) as composite,
        ):
            result = extract_split(
                ONTOLOGY,
                chunks=[ExtractionChunk(chunk_id="c0", text=TEXT)],
                llm=llm,
                relation_only=True,
                seed_entities=seed,
                max_repair_rounds=0,
                clef=False,
                skill_refs=[],
            )
        run_entity.assert_not_called()
        enrich_docs.assert_not_called()
        assemble_docs.assert_not_called()
        composite.assert_not_called()

        profile = result["profile"]
        self.assertTrue(profile["relation_only"])
        self.assertEqual(profile["deterministic_enrichment"], "disabled")
        self.assertEqual(profile["entity_source"], "seed")
        self.assertEqual(profile["seeded_entities"], 2)

        raw_entities = {(row["type"], row["name"]) for row in result["raw"]["entities"]}
        self.assertEqual(raw_entities, {("Person", "张三"), ("Organization", "示例单位")})
        self.assertEqual(len(result["raw"]["relations"]), 1)
        relation = result["raw"]["relations"][0]
        self.assertEqual(relation["rel_type"], "worksAt")
        self.assertEqual(relation["from_name"], "张三")
        self.assertEqual(relation["to_name"], "示例单位")
        self.assertIn("relation", [task for task, _ in llm.calls])
        self.assertNotIn("entity", [task for task, _ in llm.calls])


if __name__ == "__main__":
    unittest.main()
