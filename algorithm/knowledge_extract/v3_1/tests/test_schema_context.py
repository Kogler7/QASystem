"""Offline regressions for the proposed compiled-schema prompt context."""
from copy import deepcopy
import json

import pytest

from algorithm.knowledge_extract.v2.ontology_schema import OntologySchema
from algorithm.knowledge_extract.v3_1.evidence import typed


from algorithm.knowledge_extract.v3_1.schema_context import build_schema_context, property_value_schema


ONTOLOGY = """@startuml
abstract class Base {
code: String
labels: String[]
}
' @identity Base: code
class Child {
enabled: Boolean
}
class Target {
count: Integer
weight: Double
status: StatusEnum
}
class Other
Child --|> Base
Base "1" --> "0..*" Target : connects
Base --> Other : connects
Base --> Target : observes
' Base -[:COMMENT_ONLY]-> Target (post-processing note)
@enduml"""


def _build(schema=None, **kwargs):
    schema = schema or OntologySchema.parse(ONTOLOGY)
    kwargs.setdefault("allowed_entity_types", schema.classes_by_id)
    return build_schema_context(schema, **kwargs)


def test_comments_are_not_relationship_declarations_and_signatures_stay_separate():
    context = _build()
    rows = context["relation_signatures"]
    assert [(r["from_type"], r["rel_type"], r["to_type"]) for r in rows] == [
        ("Base", "connects", "Target"), ("Base", "connects", "Other"), ("Base", "observes", "Target")]
    assert rows[0]["source_cardinality"] == "1"
    assert rows[0]["target_cardinality"] == "0..*"
    assert all(r["source_candidate_ids"] == r["target_candidate_ids"] == [] for r in rows)


def test_inherited_properties_identity_and_endpoint_compatibility():
    context = _build(candidates=[{"candidate_id": "child-1", "type": "Child"},
                                 {"candidate_id": "target-1", "type": "Target"},
                                 {"candidate_id": "other-1", "type": "Other"}])
    child = next(c for c in context["entity_types"] if c["type"] == "Child")
    assert child["identity_fallbacks"] == ["code"]
    assert {p["id"]: p["value_schema"] for p in child["properties"]} == {
        "code": {"type": ["string", "null"]},
        "labels": {"type": ["array", "null"], "items": {"type": ["string", "null"]}},
        "enabled": {"type": ["boolean", "null"]}}
    assert context["relation_signatures"][0]["source_candidate_ids"] == ["child-1"]
    assert context["relation_signatures"][0]["target_candidate_ids"] == ["target-1"]
    assert context["relation_signatures"][1]["target_candidate_ids"] == ["other-1"]


def test_allowed_types_limit_candidates_but_preserve_inherited_domain_signatures():
    context = _build(allowed_entity_types={"Child", "Target"}, candidates=[
        {"candidate_id": "base-1", "type": "Base"}, {"candidate_id": "child-1", "type": "Child"},
        {"candidate_id": "target-1", "type": "Target"}, {"candidate_id": "other-1", "type": "Other"}])
    assert context["allowed_entity_types"] == ["Child", "Target"]
    assert context["relation_signatures"][0]["source_candidate_ids"] == ["child-1"]
    assert context["relation_signatures"][1]["target_candidate_ids"] == []


def test_minimal_mode_omits_property_payload_and_does_not_copy_candidates_or_ontology():
    context = _build(include_properties=False, candidates=[
        {"candidate_id": "c", "type": "Child", "name": "NOT_COPIED", "evidence": ["NOT_COPIED"]}])
    assert context["entity_types"] == [{"type": value} for value in ["Base", "Child", "Other", "Target"]]
    assert "NOT_COPIED" not in json.dumps(context)
    assert "ontology" not in context


@pytest.mark.parametrize(("datatype", "expected"), [
    ("String", {"type": ["string", "null"]}), ("DateTime?", {"type": ["string", "null"]}),
    ("Long", {"type": ["integer", "null"]}), ("Decimal", {"type": ["number", "null"]}),
    ("Boolean", {"type": ["boolean", "null"]}),
    ("DomainEnum", {"type": ["string", "number", "boolean", "null"]}),
    ("Map<Key, Value>", {"type": ["object", "null"], "additionalProperties": {
        "type": ["string", "number", "boolean", "null"]}}),
    ("Map<DimensionID.ScaleID>", {"type": ["object", "null"], "additionalProperties": {
        "type": ["string", "number", "boolean", "null"]}}),
    ("Dict", {"type": ["object", "null"], "additionalProperties": {}}),
    ("Map<String, List<Map<Key, Integer>>>", {"type": ["object", "null"], "additionalProperties": {
        "type": ["array", "null"], "items": {"type": ["object", "null"], "additionalProperties": {
            "type": ["integer", "null"]}}}}),
    (" List < Integer [] > ? ", {"type": ["array", "null"], "items": {
        "type": ["array", "null"], "items": {"type": ["integer", "null"]}}}),
])
def test_property_json_shapes_match_current_mechanical_type_contract(datatype, expected):
    assert property_value_schema(datatype) == expected


def test_unknown_scalar_contract_does_not_promise_dictionary_support():
    assert all(typed(value, "CustomType") for value in ("x", 1, 2.5, True, None))
    assert not any(typed(value, "CustomType") for value in ({"key": "value"}, [], float("inf")))
    context = _build()
    prop = next(p for c in context["entity_types"] if c["type"] == "Target" for p in c["properties"] if p["id"] == "status")
    assert prop["declared_type"] == "StatusEnum"
    assert prop["value_schema"] == {"type": ["string", "number", "boolean", "null"]}


def test_inputs_are_unchanged_and_duplicate_ids_are_deduplicated():
    schema = OntologySchema.parse(ONTOLOGY)
    candidates = [{"candidate_id": "c", "type": "Child", "properties": {"code": "1"}}] * 2
    before_candidates = deepcopy(candidates)
    before_schema = deepcopy(schema)
    context = _build(schema, candidates=candidates)
    assert context["relation_signatures"][0]["source_candidate_ids"] == ["c"]
    assert candidates == before_candidates
    assert schema == before_schema


def test_endpoint_memberships_do_not_expand_into_quadratic_entity_pairs():
    schema = OntologySchema.parse("@startuml\nclass A\nA --> A : relates\n@enduml")
    candidates = [{"candidate_id": f"c-{i}", "type": "A"} for i in range(1000)]
    context = _build(schema, candidates=candidates)
    signature = context["relation_signatures"][0]
    assert len(signature["source_candidate_ids"]) == len(signature["target_candidate_ids"]) == 1000
    assert not any(key in signature for key in ("pairs", "pair_id", "supported", "decisions"))
    assert len(json.dumps(context)) < 25000


def test_inheritance_cycles_stay_bounded():
    schema = OntologySchema.parse("@startuml\nclass A\nclass B\nA --|> B\nB --|> A\nB --> A : r\n@enduml")
    context = _build(schema, candidates=[{"candidate_id": "a", "type": "A"}])
    assert context["relation_signatures"][0]["source_candidate_ids"] == ["a"]


def test_inconsistent_internal_catalog_is_not_silently_a_valid_contract():
    with pytest.raises(ValueError, match="Unknown allowed"):
        _build(allowed_entity_types={"Missing"})
    with pytest.raises(ValueError, match="Conflicting types"):
        _build(candidates=[{"candidate_id": "same", "type": "Base"}, {"candidate_id": "same", "type": "Target"}])
