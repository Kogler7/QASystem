from algorithm.graph_align import align_nodes, conflicting_properties, shortlist
from algorithm.jev.service import Judgment


def test_conflicting_properties_ignore_blank_and_whitespace():
    assert conflicting_properties({"code": "A"}, {"code": " A "}) == []
    assert conflicting_properties({"code": "A"}, {"code": "B"}) == ["code"]
    assert conflicting_properties({"code": ""}, {"code": "B"}) == []


def test_shortlist_keeps_same_type_overlaps_only():
    pairs = shortlist(
        [{"label": "Term", "uid": "new", "properties": {"name": "轨道应答器"}}],
        [
            {"label": "Term", "uid": "old", "properties": {"name": "应答器", "code": "1"}},
            {"label": "Org", "uid": "org", "properties": {"name": "应答器"}},
            {"label": "Term", "uid": "same", "properties": {"name": "轨道应答器"}},
        ],
    )
    assert [item["existing_uid"] for item in pairs] == ["old"]


def test_same_node_with_property_clash_is_conflict_not_a_link():
    pair = shortlist(
        [{"type": "Term", "name": "轨道应答器", "properties": {"code": "A"}}],
        [{"type": "Term", "uid": "n1", "name": "应答器", "properties": {"code": "B"}}],
    )[0]
    judgment = Judgment(pair["state_id"], "same_node", {"same_node": 0.99}, 0.99, "decided")
    annotation = align_nodes(
        [{"type": "Term", "name": "轨道应答器", "properties": {"code": "A"}}],
        [{"type": "Term", "uid": "n1", "name": "应答器", "properties": {"code": "B"}}],
        {pair["state_id"]: judgment},
        routes={pair["state_id"]: "link"},
    )[0]
    assert annotation["annotation"] == "conflict"
    assert annotation["existing_uid"] == "n1"
    assert annotation["property_conflicts"] == ["code"]


def test_low_confidence_route_stays_new():
    nodes = [{"type": "Term", "name": "轨道应答器", "properties": {}}]
    existing = [{"type": "Term", "uid": "n1", "name": "应答器", "properties": {}}]
    pair = shortlist(nodes, existing)[0]
    judgment = Judgment(pair["state_id"], "same_node", {"same_node": 0.99}, 0.99, "decided")
    annotation = align_nodes(nodes, existing, {pair["state_id"]: judgment}, routes={pair["state_id"]: "keep"})[0]
    assert annotation["annotation"] == "new"
