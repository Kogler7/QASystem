from algorithm.knowledge_extract.v1.graph_utils import merge_node_properties


def test_merge_node_properties_uses_late_scalar_values_including_falsy_updates():
    assert merge_node_properties({"minValue": 0}, {"minValue": 100}) == {"minValue": 100}
    assert merge_node_properties({"flag": False}, {"flag": True}) == {"flag": True}


def test_merge_node_properties_preserves_name_and_does_not_inject_empty_name():
    merged = merge_node_properties({"name": "外观"}, {"alias": "外观别名"})

    assert merged == {"name": "外观", "alias": "外观别名"}
