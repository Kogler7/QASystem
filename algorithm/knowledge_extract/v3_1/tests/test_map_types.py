"""No model calls: mapping shape, recursion, and prior type contracts."""
from copy import deepcopy
from decimal import Decimal

import pytest

from algorithm.knowledge_extract.v3_1 import schema_types as helper
from algorithm.knowledge_extract.v3_1.evidence import typed


@pytest.mark.parametrize(("datatype", "value_type"), [
    ("Map", ""), ("DICT?", ""), ("object", ""), ("CustomType", None),
    ("Map<DimensionID.ScaleID>", "dimensionid.scaleid"),
    ("Map < Key, List < Map < K, Integer > > > ?", "list<map<k,integer>>"),
    ("Dict<String, 带空格的声明>", "带空格的声明"),
    ("Map<>", None), ("Map<A,B,C>", None), ("Map<K,List<V>", None),
])
def test_mapping_syntax_only(datatype, value_type):
    assert helper.map_value_type(datatype) == value_type


def test_single_parameter_map_keeps_values_and_unknown_scalar_semantics():
    value = {"equipment": "equipment.signalSystem", "step": "diagnosis", "enabled": True, "count": 2}
    before = deepcopy(value)
    assert typed(value, "Map<DimensionID.ScaleID>")
    assert value == before
    assert not typed({"equipment": {"label": "nested"}}, "Map<DimensionID.ScaleID>")
    assert not typed("equipment=signalSystem", "Map<DimensionID.ScaleID>")
    assert not typed({1: "equipment"}, "Map<DimensionID.ScaleID>")


def test_two_parameter_map_checks_values_recursively_without_key_coercion():
    assert typed({"2": [1, 2, None]}, "Map<Integer,List<Integer>>")
    assert not typed({2: [1]}, "Map<Integer,List<Integer>>")
    assert not typed({"2": [True]}, "Map<Integer,List<Integer>>")
    assert typed({"a": [{"b": 2}]}, "Map<K,List<Map<K,Integer>>>")
    assert not typed({"a": [{"b": "2"}]}, "Map<K,List<Map<K,Integer>>>")
    assert typed([{ "a": 2 }], "Map<K,Integer>[]")


@pytest.mark.parametrize("datatype", ["Map", "Dict", "object"])
def test_untyped_mapping_accepts_only_legal_json_objects(datatype):
    assert typed({"a": [None, True, "x", 1, 2.5, {"nested": []}]}, datatype)
    assert typed({}, datatype)
    for value in [{"a": float("nan")}, {"a": float("inf")}, {"a": Decimal("1.2")},
                  {"a": (1, 2)}, {"a": {1}}, {1: "x"}, {"a": {1: "x"}}, []]:
        assert not typed(value, datatype)
    cyclic = {}
    cyclic["self"] = cyclic
    assert not typed(cyclic, datatype)
    shared = {"x": []}
    assert typed({"a": shared, "b": shared}, datatype)


def test_nested_mapping_rejects_nonfinite_numbers_and_illegal_python_values():
    assert not typed({"a": float("inf")}, "Map<K,Double>")
    assert not typed({"a": object()}, "Map<K,Unknown>")
    assert not typed({"a": {"b": float("nan")}}, "Map<K,Map>")


@pytest.mark.parametrize(("datatype", "value", "valid"), [
    ("String", "v", True), ("String", 1, False), ("Int", 1, True), ("Int", True, False),
    ("Boolean", False, True), ("Boolean", "false", False), ("Float", float("inf"), False),
    ("CustomEnum", "enabled", True), ("CustomEnum", {"enabled": True}, False),
    ("String[]", ["a", "b"], True), ("String[]", "a,b", False),
    ("List<Int>", [1, 2], True), ("List<Int>", [1, "2"], False),
])
def test_preexisting_non_map_shape_contracts(datatype, value, valid):
    assert typed(value, datatype) is valid
