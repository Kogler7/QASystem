import json

import pytest

from algorithm.jev.blocking import iter_coref_pairs, names_overlap
from algorithm.jev.config import get_jev_config
from algorithm.jev.contract import (
    CONTRACT_VERSION,
    DOCUMENT_COREF,
    RELATION_SUPPORT,
    render_state,
    training_record,
    validate_record,
)
from algorithm.jev.routing import route
from algorithm.jev.service import DecisionService, Judgment


def test_names_overlap_requires_a_real_span():
    assert names_overlap("应答器", "轨道应答器") is True
    assert names_overlap("甲", "甲公司") is False
    assert names_overlap("应答器", "应答器") is False
    assert names_overlap("知识地图", "知识图谱") is True


def test_coref_pairs_skip_exact_names_and_order_sides():
    pairs = iter_coref_pairs(
        [
            {"type": "Term", "name": "轨道应答器", "properties": {"code": "A"}},
            {"type": "Term", "name": "应答器", "properties": {}},
            {"type": "Term", "name": "应答器", "properties": {"code": "B"}},
            {"type": "Org", "name": "应答器", "properties": {}},
        ]
    )
    assert len(pairs) == 2
    assert {item["left_name"] for item in pairs} == {"应答器"}
    assert {item["right_name"] for item in pairs} == {"轨道应答器"}
    assert all(item["left_name"] != item["right_name"] for item in pairs)


def test_render_state_keeps_field_order():
    state = render_state(
        DOCUMENT_COREF,
        {
            "type": "Term",
            "left_name": "应答器",
            "right_name": "轨道应答器",
            "left_properties": {"b": 1, "a": "x"},
            "right_properties": {},
            "left_quote": "甲",
            "right_quote": "",
        },
    )
    assert state == "\n".join(
        [
            "task: document_coref",
            "type: Term",
            "left_name: 应答器",
            "right_name: 轨道应答器",
            "left_properties: {\"a\":\"x\",\"b\":1}",
            "right_properties: {}",
            "left_quote: 甲",
            "right_quote: ",
        ]
    )


def test_training_record_is_one_hot_and_rejects_unknown_label():
    record = training_record(
        record_id="doc1-coref-1",
        state_id="doc1",
        split="train",
        task_id=DOCUMENT_COREF,
        payload={"type": "Term", "left_name": "应答器", "right_name": "轨道应答器"},
        gold_label="uncertain",
        source_group_id="doc1",
    )
    validate_record(record)
    assert record["metadata"]["contract"] == CONTRACT_VERSION
    assert record["gold_probs"]["verdict"]["uncertain"] == 1.0
    assert sum(record["gold_probs"]["verdict"].values()) == 1.0
    with pytest.raises(ValueError):
        training_record(
            record_id="x",
            state_id="doc1",
            split="train",
            task_id=RELATION_SUPPORT,
            payload={},
            gold_label="same_referent",
            source_group_id="doc1",
        )


def test_route_keeps_low_confidence_and_uncertain(monkeypatch):
    monkeypatch.setenv("KG_JEV_CHOICE_CONFIDENCE", "0.9")
    confident = Judgment("p", "same_referent", {"same_referent": 0.95}, 0.95, "decided")
    weak = Judgment("p", "same_referent", {"same_referent": 0.5}, 0.5, "decided")
    unsure = Judgment("p", "uncertain", {"uncertain": 0.99}, 0.99, "decided")
    assert route(confident, DOCUMENT_COREF) == "merge"
    assert route(weak, DOCUMENT_COREF) == "keep"
    assert route(unsure, DOCUMENT_COREF) == "keep"
    dropped = Judgment("p", "unsupported", {"unsupported": 0.91}, 0.91, "decided")
    assert route(dropped, RELATION_SUPPORT) == "drop"


def test_predict_skips_without_loading_a_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("KG_JEV_ENABLED", "1")
    monkeypatch.setenv("KG_JEV_MODEL_PATH", str(tmp_path))
    monkeypatch.delenv("ALGORITHM_RUNTIME_PROFILE", raising=False)
    prediction = DecisionService().predict(DOCUMENT_COREF, [{"state_id": "p", "type": "Term"}])
    assert prediction.status == "skipped"
    assert prediction.reason == "domain_checkpoint_not_ready"
    assert prediction.judgments == []


def test_predict_skips_when_disabled(monkeypatch):
    monkeypatch.delenv("KG_JEV_ENABLED", raising=False)
    assert get_jev_config()["enabled"] is False
    prediction = DecisionService().predict(DOCUMENT_COREF, [])
    assert prediction.reason == "disabled"


def test_predict_refuses_remote_only(monkeypatch, tmp_path):
    monkeypatch.setenv("KG_JEV_ENABLED", "1")
    monkeypatch.setenv("KG_JEV_MODEL_PATH", str(tmp_path))
    monkeypatch.setenv("ALGORITHM_RUNTIME_PROFILE", "remote-only")
    prediction = DecisionService().predict(DOCUMENT_COREF, [])
    assert prediction.reason == "remote_only"


def test_injected_predictor_can_decide(monkeypatch, tmp_path):
    monkeypatch.setenv("KG_JEV_ENABLED", "1")
    monkeypatch.setenv("KG_JEV_MODEL_PATH", str(tmp_path))
    monkeypatch.delenv("ALGORITHM_RUNTIME_PROFILE", raising=False)

    def predictor(task_id, requests):
        assert task_id == DOCUMENT_COREF
        assert requests[0]["questions"]["verdict"]["type"] == "choice"
        return [Judgment(requests[0]["id"], "different", {"different": 1.0}, 1.0, "decided")]

    prediction = DecisionService(predictor).predict(
        DOCUMENT_COREF,
        [{"state_id": "p", "type": "Term", "left_name": "甲", "right_name": "乙"}],
    )
    assert prediction.status == "decided"
    assert prediction.judgments[0].choice == "different"
    json.dumps(prediction.judgments[0].to_dict(), ensure_ascii=False)
