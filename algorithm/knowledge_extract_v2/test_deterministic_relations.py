"""Regression tests for provenance-scoped deterministic relation assembly."""
from __future__ import annotations

import unittest

from algorithm.knowledge_extract_v2.candidate_models import EntityCandidate, RelationCandidate
from algorithm.knowledge_extract_v2.chunk_context import ChunkContextManager, ExtractionChunk
from algorithm.knowledge_extract_v2.composite import CompositeElementHandler
from algorithm.knowledge_extract_v2.entity_resolver import CandidatePool
from algorithm.knowledge_extract_v2.evidence import Evidence
from algorithm.knowledge_extract_v2.hierarchy import (
    assemble_document_relations,
    assemble_document_unit_relations,
    assemble_numbered_hierarchy_relations,
    assemble_standard_technical_relations,
)
from algorithm.knowledge_extract_v2.ontology_schema import OntologySchema
from algorithm.knowledge_extract_v2.ontology_compiler import compile_plan
from algorithm.knowledge_extract_v2.relation_resolver import enumerate_relation_pairs


def _evidence(chunk_id: str, text: str, start: int = 0) -> Evidence:
    return Evidence(
        span_id=f"{chunk_id}:{start}:{start + len(text)}",
        source_block_ids=[chunk_id],
        quote=text,
        start_char=start,
        end_char=start + len(text),
        evidence_type="structural",
    )


def _entity(type_id: str, name: str, chunk_id: str, quote: str, *, source: str) -> EntityCandidate:
    return EntityCandidate(
        candidate_id="",
        proposed_type=type_id,
        canonical_type=type_id,
        name=name,
        properties={},
        evidence=[_evidence(chunk_id, quote)],
        source=source,
    )


class DeterministicRelationAssemblyTest(unittest.TestCase):
    def test_body_organization_is_not_promoted_to_document_metadata(self) -> None:
        schema = OntologySchema.parse(
            """@startuml
class Document
class Section
class Organization
Document --> Section : hasStructuralElement
Document --> Organization : issuedBy
@enduml
"""
        )
        text = "Document title\n# 1 Body\nBody mentions Example Org"
        manager = ChunkContextManager([ExtractionChunk("c1", text, document_id="A")])
        pool = CandidatePool(schema)
        document = _entity("Document", "Document title", "c1", "Document title", source="document_anchor")
        section = _entity("Section", "1 Body", "c1", "# 1 Body", source="markdown_structure")
        section.evidence = [_evidence("c1", "# 1 Body", text.index("# 1 Body"))]
        organization = _entity("Organization", "Example Org", "c1", "Example Org", source="")
        organization.evidence = [_evidence("c1", "Example Org", text.index("Example Org"))]
        for row in (document, section, organization):
            pool.add_entity(row)

        assemble_document_relations(
            pool,
            manager,
            {"primary_candidate_id": document.candidate_id},
        )

        self.assertNotIn("issuedBy", {edge.canonical_relation for edge in pool.accepted_relations()})

    def test_composite_membership_stays_inside_document_scope(self) -> None:
        ontology = """@startuml
class CompositeRule
class SimpleRule
CompositeRule "1" --> "1..*" SimpleRule : hasSubRule
@enduml
"""
        plan = compile_plan(ontology)
        schema = OntologySchema.parse(ontology)
        manager = ChunkContextManager(
            [
                ExtractionChunk("doc-a", "when x then y", document_id="A"),
                ExtractionChunk("doc-b", "when x then y", document_id="B"),
            ]
        )
        pool = CandidatePool(schema)
        composite = _entity(
            "CompositeRule", "combined", "doc-a", "when x then y", source="test"
        )
        composite.properties["ruleText"] = "when x then y"
        member = _entity("SimpleRule", "member", "doc-b", "when x then y", source="test")
        member.properties["ruleText"] = "when x then y"
        pool.add_entity(composite)
        pool.add_entity(member)

        CompositeElementHandler.from_plan(plan).assemble(pool, plan, manager)

        self.assertEqual(pool.accepted_relations(), [])

    def test_composite_membership_separates_files_in_a_batch_document(self) -> None:
        ontology = """@startuml
class CompositeRule
class SimpleRule
CompositeRule "1" --> "1..*" SimpleRule : hasSubRule
@enduml
"""
        plan = compile_plan(ontology)
        schema = OntologySchema.parse(ontology)
        manager = ChunkContextManager(
            [
                ExtractionChunk(
                    "file-a", "when x then y", document_id="batch", metadata={"source_file": "a.md"}
                ),
                ExtractionChunk(
                    "file-b", "when x then y", document_id="batch", metadata={"source_file": "b.md"}
                ),
            ]
        )
        pool = CandidatePool(schema)
        composite = _entity("CompositeRule", "combined", "file-a", "when x then y", source="test")
        composite.properties["ruleText"] = "when x then y"
        member = _entity("SimpleRule", "member", "file-b", "when x then y", source="test")
        member.properties["ruleText"] = "when x then y"
        pool.add_entity(composite)
        pool.add_entity(member)

        CompositeElementHandler.from_plan(plan).assemble(pool, plan, manager)

        self.assertEqual(pool.accepted_relations(), [])

    def test_composite_membership_requires_locatable_evidence(self) -> None:
        ontology = """@startuml
class CompositeRule
class SimpleRule
CompositeRule "1" --> "1..*" SimpleRule : hasSubRule
@enduml
"""
        plan = compile_plan(ontology)
        pool = CandidatePool(OntologySchema.parse(ontology))
        manager = ChunkContextManager([ExtractionChunk("c1", "unrelated text")])
        composite = _entity("CompositeRule", "combined", "c1", "missing", source="test")
        composite.evidence = []
        composite.properties["ruleText"] = "when x then y"
        member = _entity("SimpleRule", "member", "c1", "missing", source="test")
        member.evidence = []
        member.properties.update(
            {"ruleText": "when x then y", "triggerCondition": "when x", "action": "then y"}
        )
        pool.add_entity(composite)
        pool.add_entity(member)
        warnings: list[dict] = []

        CompositeElementHandler.from_plan(plan).assemble(pool, plan, manager, warnings=warnings)

        self.assertEqual(pool.accepted_relations(), [])
        self.assertEqual(warnings[0]["code"], "COMPOSITE_MEMBERSHIP_EVIDENCE_NOT_FOUND")

    def test_relations_without_an_assembler_remain_classifier_candidates(self) -> None:
        ontology = """@startuml
class Constraint
class Object
class Property
Constraint --> Object : constrainsObject
Constraint --> Property : constrainsCharacteristic
Object --> Object : replaces
@enduml
"""
        plan = compile_plan(ontology)
        rows = enumerate_relation_pairs(
            plan,
            [
                {"id": "constraint", "type": "Constraint", "name": "C"},
                {"id": "object-a", "type": "Object", "name": "A"},
                {"id": "object-b", "type": "Object", "name": "B"},
                {"id": "property", "type": "Property", "name": "P"},
            ],
        )
        self.assertEqual(
            {row["relation_id"] for row in rows},
            {"constrainsobject", "constrainscharacteristic", "replaces"},
        )

    def test_separator_spelling_is_resolved_and_skipped_for_deterministic_relations(self) -> None:
        ontology = """@startuml
class Document
class DocumentUnit
Document --> DocumentUnit : HAS_PART
@enduml
"""
        plan = compile_plan(ontology)
        schema = OntologySchema.parse(ontology)

        self.assertEqual(len(schema.resolve_relation("hasPart")), 1)
        self.assertEqual(len(plan.resolve_relation("hasPart")), 1)
        self.assertEqual(
            enumerate_relation_pairs(
                plan,
                [
                    {"id": "document", "type": "Document", "name": "D"},
                    {"id": "unit", "type": "DocumentUnit", "name": "U"},
                ],
            ),
            [],
        )

    def test_dangling_rejection_is_not_hidden_by_a_partially_matching_edge(self) -> None:
        schema = OntologySchema.parse(
            """@startuml
class Source
class Target
Source --> Target : owns
@enduml
"""
        )
        pool = CandidatePool(schema)
        pool.add_entity(_entity("Source", "S", "c1", "S", source="test"))
        pool.add_entity(_entity("Target", "T", "c1", "T", source="test"))
        pool.add_relation(RelationCandidate("", "entity-0", "entity-1", "owns", None, []))
        pool.add_relation(RelationCandidate("", "missing", "entity-1", "owns", None, []))

        self.assertEqual(
            [row["code"] for row in pool.unresolved_relation_rejections()],
            ["UNKNOWN_RELATION_ENDPOINT_ID"],
        )

    def test_finite_cardinality_upper_bound_is_enforced(self) -> None:
        schema = OntologySchema.parse(
            """@startuml
class Source
class Target
Source "0..*" --> "0..2" Target : owns
@enduml
"""
        )
        pool = CandidatePool(schema)
        pool.add_entity(_entity("Source", "S", "c1", "S", source="test"))
        for name in ("T1", "T2", "T3"):
            pool.add_entity(_entity("Target", name, "c1", name, source="test"))
        for target_id in ("entity-1", "entity-2", "entity-3"):
            pool.add_relation(RelationCandidate("", "entity-0", target_id, "owns", None, []))

        self.assertEqual(len(pool.accepted_relations()), 2)
        self.assertEqual(
            [row["code"] for row in pool.unresolved_relation_rejections()],
            ["RELATION_CARDINALITY_EXCEEDED"],
        )

    def test_cardinality_isolated_by_overloaded_relation_signature(self) -> None:
        schema = OntologySchema.parse(
            """@startuml
class Activity
class Constraint
class Unit
Activity "1" --> "0..*" Unit : EVIDENCED_BY
Constraint "1" --> "0..*" Unit : EVIDENCED_BY
@enduml
"""
        )
        pool = CandidatePool(schema)
        for row in (
            _entity("Activity", "A", "c1", "A", source="test"),
            _entity("Constraint", "C", "c1", "C", source="test"),
            _entity("Unit", "U", "c1", "U", source="test"),
        ):
            pool.add_entity(row)
        pool.add_relation(RelationCandidate("", "entity-0", "entity-2", "EVIDENCED_BY", None, []))
        pool.add_relation(RelationCandidate("", "entity-1", "entity-2", "EVIDENCED_BY", None, []))

        self.assertEqual(len(pool.accepted_relations()), 2)
        self.assertEqual(pool.unresolved_relation_rejections(), [])

    def test_mapping_rejection_revives_better_cardinality_candidate(self) -> None:
        schema = OntologySchema.parse(
            """@startuml
class Source
class Target
Source "0..*" --> "1" Target : owns
@enduml
"""
        )
        pool = CandidatePool(schema)
        for row in (
            _entity("Source", "S", "c1", "S", source="test"),
            _entity("Target", "low", "c1", "low", source="test"),
            _entity("Target", "high", "c1", "high", source="test"),
        ):
            pool.add_entity(row)
        pool.add_relation(RelationCandidate("", "entity-0", "entity-1", "owns", None, []))
        pool.add_relation(RelationCandidate("", "entity-0", "entity-2", "owns", None, []))
        low, high = pool.relation_candidates()
        low.mapping_score = 0.4
        low.mapping_status = "rejected"
        low.warnings.append("MAPPING_REJECTED")
        high.mapping_score = 0.9
        high.mapping_status = "accepted"

        pool.recompute_relation_cardinality()

        self.assertEqual(
            [(edge.to_name, edge.mapping_score) for edge in pool.accepted_relations()],
            [("high", 0.9)],
        )

    def test_has_part_stays_inside_document_scope(self) -> None:
        schema = OntologySchema.parse(
            """@startuml
class Document
class DocumentUnit
Document "1" --> "0..*" DocumentUnit : HAS_PART
@enduml
"""
        )
        manager = ChunkContextManager(
            [
                ExtractionChunk(
                    "doc-a", "A\nA-unit", document_id="A", metadata={"source_file": "same.md"}
                ),
                ExtractionChunk(
                    "doc-b", "B\nB-unit", document_id="B", metadata={"source_file": "same.md"}
                ),
            ]
        )
        pool = CandidatePool(schema)
        for row in (
            _entity("Document", "A", "doc-a", "A", source="document_anchor"),
            _entity("Document", "B", "doc-b", "B", source="document_anchor"),
            _entity("DocumentUnit", "A-unit", "doc-a", "A-unit", source="document_unit"),
            _entity("DocumentUnit", "B-unit", "doc-b", "B-unit", source="document_unit"),
        ):
            pool.add_entity(row)

        assemble_document_unit_relations(pool, manager)

        self.assertEqual(
            {(edge.from_name, edge.to_name) for edge in pool.accepted_relations()},
            {("A", "A-unit"), ("B", "B-unit")},
        )

    def test_numbered_hierarchy_does_not_borrow_parent_from_other_document(self) -> None:
        schema = OntologySchema.parse(
            """@startuml
class Clause
Clause "1" --> "0..*" Clause : hasSubClause
@enduml
"""
        )
        manager = ChunkContextManager(
            [
                ExtractionChunk("doc-a", "1 Parent", document_id="A"),
                ExtractionChunk("doc-b", "1.1 Child", document_id="B"),
            ]
        )
        pool = CandidatePool(schema)
        pool.add_entity(_entity("Clause", "1 Parent", "doc-a", "1 Parent", source="markdown_structure"))
        pool.add_entity(_entity("Clause", "1.1 Child", "doc-b", "1.1 Child", source="markdown_structure"))

        self.assertEqual(assemble_numbered_hierarchy_relations(pool, manager), 0)
        self.assertEqual(pool.accepted_relations(), [])

    def test_numbered_hierarchy_abstains_across_unknown_multi_chunk_scope(self) -> None:
        schema = OntologySchema.parse(
            """@startuml
class Clause
Clause "1" --> "0..*" Clause : hasSubClause
@enduml
"""
        )
        manager = ChunkContextManager(
            [ExtractionChunk("a", "1 Parent"), ExtractionChunk("b", "1.1 Child")]
        )
        pool = CandidatePool(schema)
        pool.add_entity(_entity("Clause", "1 Parent", "a", "1 Parent", source="markdown_structure"))
        pool.add_entity(_entity("Clause", "1.1 Child", "b", "1.1 Child", source="markdown_structure"))

        self.assertEqual(assemble_numbered_hierarchy_relations(pool, manager), 0)
        self.assertEqual(pool.accepted_relations(), [])

    def test_fact_before_first_unit_is_not_assigned_to_future_unit(self) -> None:
        schema = OntologySchema.parse(
            """@startuml
class Fact
class DocumentUnit
Fact "0..*" --> "1" DocumentUnit : EVIDENCED_BY
@enduml
"""
        )
        text = "fact appears before the first unit\nunit heading"
        manager = ChunkContextManager([ExtractionChunk("c1", text, document_id="A")])
        pool = CandidatePool(schema)
        pool.add_entity(_entity("Fact", "fact", "c1", "fact", source="llm"))
        unit = _entity("DocumentUnit", "unit", "c1", "unit heading", source="document_unit")
        unit.evidence = [_evidence("c1", "unit heading", text.index("unit heading"))]
        pool.add_entity(unit)

        assemble_document_unit_relations(pool, manager)

        self.assertEqual(pool.accepted_relations(), [])

    def test_characteristics_are_linked_to_the_subject_in_the_same_clause(self) -> None:
        schema = OntologySchema.parse(
            """@startuml
class Clause
class "标准对象" as "Subject of Standardization"
class Property
Clause "1" --> "0..*" Property : specifiesCharacteristic
"Subject of Standardization" "1" --> "0..*" Property : hasCharacteristic
@enduml
"""
        )
        text = "## 6.1 要求\n电池容量应满足要求。\n## 6.2 要求\n电机转速应满足要求。"
        manager = ChunkContextManager([ExtractionChunk("c1", text, document_id="A")])
        pool = CandidatePool(schema)
        rows = (
            _entity("Clause", "6.1 要求", "c1", "## 6.1 要求", source="standard_semantics"),
            _entity("Clause", "6.2 要求", "c1", "## 6.2 要求", source="standard_semantics"),
            _entity("Subject of Standardization", "电池", "c1", "电池", source="technical_semantics"),
            _entity("Subject of Standardization", "电机", "c1", "电机", source="technical_semantics"),
            _entity("Property", "容量", "c1", "容量", source="technical_semantics"),
            _entity("Property", "转速", "c1", "转速", source="technical_semantics"),
        )
        rows[0].evidence = [_evidence("c1", "## 6.1 要求", text.index("## 6.1 要求"))]
        rows[1].evidence = [_evidence("c1", "## 6.2 要求", text.index("## 6.2 要求"))]
        for row in rows:
            pool.add_entity(row)

        assemble_standard_technical_relations(pool, manager)

        characteristic_edges = {
            (edge.from_name, edge.to_name)
            for edge in pool.accepted_relations()
            if edge.canonical_relation == "hasCharacteristic"
        }
        self.assertEqual(characteristic_edges, {("电池", "容量"), ("电机", "转速")})


if __name__ == "__main__":
    unittest.main()
