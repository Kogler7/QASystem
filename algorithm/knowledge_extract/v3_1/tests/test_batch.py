from copy import deepcopy

from algorithm.knowledge_extract.v3_1.batch import deduplicate_chunk_graphs


def node(uid):
    kind, name = uid.split("::", 1)
    return {"uid": uid, "label": kind, "properties": {"name": name}}


def edge(**metadata):
    return {"from_uid": "A::甲", "to_uid": "B::乙", "type": "R", **metadata}


def raw_edge(**metadata):
    return {"from_type": "A", "from_name": "甲", "rel_type": "R",
            "to_type": "B", "to_name": "乙", **metadata}


def item(index, edges=(), raw=(), nodes=None):
    return {"index": index, "graph": {"nodes": nodes if nodes is not None else [node("A::甲"), node("B::乙")],
                                      "relationships": list(edges)}, "raw": {"relations": list(raw)}}


def test_later_graph_provenance_survives_document_raw_in_earlier_item():
    evidence = {"quote": "甲联系乙", "source_block_ids": ["chunk-0001"]}
    rich = edge(evidences=[evidence], chunk_indices=[1], provenance_status="evidence")
    items = [item(0, raw=[raw_edge(evidence=evidence)]), item(1, [rich], [raw_edge(evidence=evidence)])]
    assert deduplicate_chunk_graphs(items)["relationships"][0]["evidences"] == [evidence]
    assert deduplicate_chunk_graphs(items)["relationships"][0]["chunk_indices"] == [1]
    assert deduplicate_chunk_graphs(items)["relationships"][0]["provenance_status"] == "evidence"


def test_duplicate_graph_edges_union_only_supplied_provenance():
    first = {"quote": "甲联系乙", "source_block_ids": ["chunk-0001"]}
    second = {"quote": "乙由甲联系", "source_block_ids": ["chunk-0002"]}
    items = [item(0, [edge(evidences=[first], chunk_indices=[1], provenance_status="evidence", properties={"x": "first"})]),
             item(1, [edge(evidences=[first, second], chunk_indices=[1, 2], provenance_status="evidence", properties={"x": "second"})])]
    result = deduplicate_chunk_graphs(items)["relationships"]
    assert len(result) == 1
    assert result[0]["evidences"] == [first, second]
    assert result[0]["chunk_indices"] == [1, 2]
    # This patch does not resolve conflicting relationship facts/properties.
    assert result[0]["properties"] == {"x": "first"}


def test_keeps_legacy_raw_only_cross_chunk_bridge_and_drops_unknown_endpoint():
    unknown = {**raw_edge(), "to_name": "不存在"}
    items = [item(0, raw=[raw_edge(), unknown], nodes=[node("A::甲")]),
             item(1, nodes=[node("B::乙")])]
    assert deduplicate_chunk_graphs(items)["relationships"] == [edge()]


def test_raw_only_provenance_is_preserved_without_inventing_source_indices():
    evidence = {"quote": "甲联系乙", "source_block_ids": ["chunk-0002"]}
    result = deduplicate_chunk_graphs([item(0, raw=[raw_edge(evidence=evidence)])])["relationships"][0]
    assert result["evidence"] == evidence
    assert "chunk_indices" not in result
    assert "provenance_status" not in result


def test_singular_and_plural_evidence_merge_without_claiming_inferred_source():
    first = {"quote": "甲联系乙"}
    second = {"quote": "乙由甲联系", "source_block_ids": ["chunk-0002"]}
    items = [item(0, [edge(evidence=first, chunk_indices=[], provenance_status="inferred_from_endpoints")]),
             item(1, [edge(evidences=[second], chunk_indices=[2], provenance_status="evidence")])]
    result = deduplicate_chunk_graphs(items)["relationships"][0]
    assert result["evidences"] == [first, second]
    assert result["chunk_indices"] == [2]
    assert result["provenance_status"] == "evidence"


def test_does_not_mutate_input_edges_and_results_do_not_alias_them():
    evidence = {"quote": "甲联系乙", "source_block_ids": ["chunk-0001"]}
    items = [item(0, [edge(evidences=[evidence], chunk_indices=[1])]),
             item(1, [edge(evidences=[evidence], chunk_indices=[2])])]
    before = deepcopy(items)
    merged = deduplicate_chunk_graphs(items)
    assert items == before
    merged["relationships"][0]["evidences"][0]["quote"] = "changed"
    merged["relationships"][0]["chunk_indices"].append(99)
    assert items == before


def test_graph_metadata_precedes_raw_copies_regardless_of_item_order():
    evidence = {"quote": "甲联系乙", "source_block_ids": ["chunk-0001"]}
    rich = edge(evidences=[evidence], chunk_indices=[1], provenance_status="evidence", properties={"x": "graph"})
    items = [item(0, raw=[raw_edge()]), item(1, [rich], [raw_edge()])]
    assert deduplicate_chunk_graphs(items)["relationships"] == deduplicate_chunk_graphs(list(reversed(items)))["relationships"]


def test_distinct_relation_types_remain_distinct():
    items = [item(0, [edge(), {**edge(), "type": "S"}])]
    assert [value["type"] for value in deduplicate_chunk_graphs(items)["relationships"]] == ["R", "S"]
