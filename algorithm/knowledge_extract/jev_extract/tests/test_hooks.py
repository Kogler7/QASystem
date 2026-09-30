from algorithm.jev.service import DecisionService, Judgment
from algorithm.knowledge_extract.jev_extract.hooks import attach_jev_shadow


def _result():
    graph = {
        "nodes": [
            {"uid": "Term::轨道应答器", "label": "Term", "properties": {"name": "轨道应答器", "code": "A"}},
        ],
        "relationships": [],
    }
    return {
        "status": "succeeded",
        "graph": graph,
        "raw": {
            "entities": [
                {"type": "Term", "name": "应答器", "properties": {}, "source_refs": [{"quote": "应答器属于轨道应答器。"}]},
                {"type": "Term", "name": "轨道应答器", "properties": {"code": "A"}, "source_refs": [{"quote": "应答器属于轨道应答器。"}]},
            ],
            "relations": [
                {
                    "from_type": "Term",
                    "from_name": "应答器",
                    "rel_type": "belongsTo",
                    "to_type": "Term",
                    "to_name": "轨道应答器",
                    "source_refs": [{"quote": "应答器属于轨道应答器。"}],
                }
            ],
        },
    }


def test_disabled_shadow_returns_the_same_object(monkeypatch):
    monkeypatch.delenv("KG_JEV_ENABLED", raising=False)
    result = _result()
    assert attach_jev_shadow(result) is result
    assert "jev" not in result


def test_enabled_without_checkpoint_adds_report_and_keeps_graph(monkeypatch, tmp_path):
    monkeypatch.setenv("KG_JEV_ENABLED", "1")
    monkeypatch.setenv("KG_JEV_DOCUMENT_COREF", "1")
    monkeypatch.setenv("KG_JEV_RELATION_SUPPORT", "1")
    monkeypatch.setenv("KG_JEV_GRAPH_ALIGN", "1")
    monkeypatch.setenv("KG_JEV_MODEL_PATH", str(tmp_path))
    monkeypatch.delenv("ALGORITHM_RUNTIME_PROFILE", raising=False)
    result = _result()
    reported = attach_jev_shadow(result)
    assert reported is not result
    assert reported["graph"] is result["graph"]
    assert reported["raw"] is result["raw"]
    assert set(reported["jev"]["tasks"]) == {"document_coref", "relation_support", "graph_align"}
    assert reported["jev"]["tasks"]["document_coref"]["status"] == "skipped"
    assert reported["jev"]["tasks"]["document_coref"]["proposals"] == []
    assert reported["jev"]["tasks"]["document_coref"]["candidate_count"] == 1
    assert reported["jev"]["tasks"]["relation_support"]["candidate_count"] == 1
    assert reported["jev"]["tasks"]["graph_align"]["reason"] == "no_existing_nodes"


def test_decided_coref_proposal_does_not_merge(monkeypatch, tmp_path):
    monkeypatch.setenv("KG_JEV_ENABLED", "1")
    monkeypatch.setenv("KG_JEV_DOCUMENT_COREF", "1")
    monkeypatch.setenv("KG_JEV_CHOICE_CONFIDENCE", "0.9")
    monkeypatch.setenv("KG_JEV_MODEL_PATH", str(tmp_path))
    monkeypatch.delenv("ALGORITHM_RUNTIME_PROFILE", raising=False)

    def predictor(_task_id, requests):
        return [Judgment(requests[0]["id"], "same_referent", {"same_referent": 0.97}, 0.97, "decided")]

    result = _result()
    reported = attach_jev_shadow(result, service=DecisionService(predictor))
    proposal = reported["jev"]["tasks"]["document_coref"]["proposals"][0]
    assert proposal["route"] == "merge"
    assert reported["raw"]["entities"] == result["raw"]["entities"]
    assert len(reported["graph"]["nodes"]) == 1
