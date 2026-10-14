"""Unit tests for Stage-1 v3.1 bridge and exact-key seeding."""
from __future__ import annotations

import unittest

from algorithm.knowledge_extract.v2_2_v31.chunk_context import ExtractionChunk
from algorithm.knowledge_extract.v2_2_v31.entity_resolver import CandidatePool
from algorithm.knowledge_extract.v2_2_v31.ontology_schema import OntologySchema
from algorithm.knowledge_extract.v2_2_v31.v31_entity_bridge import (
    V31_ENTITY_SOURCE,
    entity_dict_to_candidate,
    seed_frozen_entities,
    to_v31_chunks,
)


ONTOLOGY = """@startuml
class "术语" as Terminology <<KnowledgeUnit>> {
  + 术语标准名称 (TermName): String
}
' @identity Terminology: TermName
@enduml
"""


class V31EntityBridgeTest(unittest.TestCase):
    def test_chunk_roundtrip_preserves_text_and_id(self) -> None:
        chunks = [
            ExtractionChunk(
                chunk_id="c1",
                text="主数据治理是核心。",
                metadata={"block_id": "b1"},
            )
        ]
        converted = to_v31_chunks(chunks)
        self.assertEqual(converted[0].chunk_id, "c1")
        self.assertEqual(converted[0].text, "主数据治理是核心。")
        self.assertEqual(converted[0].metadata.get("block_id"), "b1")

    def test_seed_exact_key_merges_evidence_not_aliases(self) -> None:
        schema = OntologySchema.parse(ONTOLOGY)
        pool = CandidatePool(schema, allowed_types={"Terminology"})
        first = {
            "type": "Terminology",
            "name": "主数据治理",
            "properties": {"TermName": "主数据治理"},
            "evidence": {
                "span_id": "s1",
                "source_block_ids": ["c1"],
                "quote": "主数据治理是核心",
            },
        }
        second = {
            "type": "Terminology",
            "name": "主数据治理",
            "properties": {"TermName": "主数据治理"},
            "evidences": [
                {
                    "span_id": "s2",
                    "source_block_ids": ["c2"],
                    "quote": "开展主数据治理",
                }
            ],
        }
        alias = {
            "type": "Terminology",
            "name": "主数据管理",
            "properties": {"TermName": "主数据管理"},
            "evidence": {
                "span_id": "s3",
                "source_block_ids": ["c1"],
                "quote": "主数据管理",
            },
        }
        seed_frozen_entities(pool, [first, second, alias])
        accepted = pool.accepted_entities()
        self.assertEqual(len(accepted), 2)
        by_name = {entity.name: entity for entity in accepted}
        self.assertEqual(by_name["主数据治理"].source, V31_ENTITY_SOURCE)
        self.assertEqual(len(by_name["主数据治理"].evidence), 2)
        self.assertIn("主数据管理", by_name)

    def test_entity_dict_builds_mentions_from_name_and_quote(self) -> None:
        candidate = entity_dict_to_candidate(
            {
                "type": "Terminology",
                "name": "主数据治理",
                "properties": {},
                "evidence": {
                    "span_id": "s1",
                    "source_block_ids": ["c1"],
                    "quote": "开展主数据治理",
                },
            }
        )
        self.assertEqual(candidate.source, V31_ENTITY_SOURCE)
        self.assertEqual(candidate.mentions[0], "主数据治理")
        self.assertIn("开展主数据治理", candidate.mentions)


if __name__ == "__main__":
    unittest.main()
