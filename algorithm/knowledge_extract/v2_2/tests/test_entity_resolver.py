"""Entity identity reconcile: ontology identity only, no surface-form heuristics."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = PACKAGE_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


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
        assert spec.loader is not None
        spec.loader.exec_module(module)
        loaded[stem] = module
    return loaded


MODULES = _load_package_modules(
    "knowledge_extract_v2_2_entity_resolver_test",
    PACKAGE_DIR,
    [
        "graph_utils",
        "ontology_schema",
        "hierarchy",
        "evidence",
        "candidate_models",
        "relation_resolver",
        "entity_resolver",
    ],
)
SCHEMA = MODULES["ontology_schema"]
ER = MODULES["entity_resolver"]
CM = MODULES["candidate_models"]

ONTOLOGY = """
@startuml
class Concern {
  关注点标识 (concernId)
  名称 (name)
}
class Stakeholder {
  利益相关者标识 (stakeholderId)
  名称 (name)
}
Concern "1" --> "1" Stakeholder : is a matter of importance to
' @identity Concern: concernId
' @identity Stakeholder: stakeholderId
@enduml
"""


class IdentityReconcileTest(unittest.TestCase):
    def _pool(self):
        schema = SCHEMA.OntologySchema.parse(ONTOLOGY)
        return ER.CandidatePool(schema)

    def _entity(self, type_id: str, name: str, properties: dict):
        return CM.EntityCandidate(
            candidate_id="",
            proposed_type=type_id,
            canonical_type=type_id,
            name=name,
            properties=dict(properties),
            evidence=[],
            source="test",
        )

    def test_display_name_property_keys_feed_identity(self) -> None:
        pool = self._pool()
        accepted = pool.add_entity(
            self._entity(
                "Concern",
                "表面名甲",
                {"关注点标识": "CON_1", "名称": "正式名称"},
            )
        )
        self.assertIsNotNone(accepted)
        assert accepted is not None
        self.assertEqual(accepted.identity_property, "concernId")
        self.assertEqual(accepted.identity_value, "CON_1")
        self.assertEqual(accepted.properties.get("concernId"), "CON_1")
        self.assertEqual(accepted.properties.get("name"), "正式名称")

    def test_reconcile_merges_same_identity_different_surface_names(self) -> None:
        pool = self._pool()
        first = pool.add_entity(self._entity("Concern", "表面名甲", {}))
        second = pool.add_entity(self._entity("Concern", "表面名乙", {}))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertNotEqual(first.candidate_id, second.candidate_id)
        self.assertEqual(len(pool.accepted_entities()), 2)

        first.properties["concernId"] = "CON_1"
        second.properties["concernId"] = "CON_1"
        merged = pool.reconcile_identities()
        self.assertEqual(merged, 1)
        accepted = pool.accepted_entities()
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].identity_property, "concernId")
        self.assertEqual(accepted[0].identity_value, "CON_1")

    def test_reconcile_keeps_distinct_identity_values(self) -> None:
        pool = self._pool()
        pool.add_entity(self._entity("Concern", "甲", {"concernId": "CON_1"}))
        pool.add_entity(self._entity("Concern", "乙", {"concernId": "CON_2"}))
        self.assertEqual(pool.reconcile_identities(), 0)
        self.assertEqual(len(pool.accepted_entities()), 2)

    def test_reconcile_does_not_merge_name_only_identity(self) -> None:
        pool = self._pool()
        pool.add_entity(self._entity("Concern", "甲", {}))
        pool.add_entity(self._entity("Concern", "乙", {}))
        self.assertEqual(pool.reconcile_identities(), 0)
        self.assertEqual(len(pool.accepted_entities()), 2)

    def test_add_entity_merges_when_identity_already_present(self) -> None:
        pool = self._pool()
        first = pool.add_entity(
            self._entity("Stakeholder", "工程师", {"stakeholderId": "SH_1"})
        )
        second = pool.add_entity(
            self._entity("Stakeholder", "工程师别称", {"stakeholderId": "SH_1"})
        )
        self.assertIsNotNone(first)
        self.assertIs(first, second)
        self.assertEqual(len(pool.accepted_entities()), 1)

    def test_rewire_collapses_duplicate_relations_after_reconcile(self) -> None:
        pool = self._pool()
        concern_a = pool.add_entity(self._entity("Concern", "甲", {}))
        concern_b = pool.add_entity(self._entity("Concern", "乙", {}))
        stakeholder = pool.add_entity(
            self._entity("Stakeholder", "工程师", {"stakeholderId": "SH_1"})
        )
        assert concern_a and concern_b and stakeholder
        pool.add_relation(
            CM.RelationCandidate(
                candidate_id="",
                from_candidate_id=concern_a.candidate_id,
                to_candidate_id=stakeholder.candidate_id,
                proposed_relation="is a matter of importance to",
                canonical_relation=None,
                evidence=[],
            )
        )
        pool.add_relation(
            CM.RelationCandidate(
                candidate_id="",
                from_candidate_id=concern_b.candidate_id,
                to_candidate_id=stakeholder.candidate_id,
                proposed_relation="is a matter of importance to",
                canonical_relation=None,
                evidence=[],
            )
        )
        self.assertEqual(len(pool.accepted_relations()), 2)
        concern_a.properties["concernId"] = "CON_1"
        concern_b.properties["concernId"] = "CON_1"
        pool.reconcile_identities()
        self.assertEqual(len(pool.accepted_entities()), 2)  # 1 concern + 1 stakeholder
        self.assertEqual(len(pool.accepted_relations()), 1)


if __name__ == "__main__":
    unittest.main()
