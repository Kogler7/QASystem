"""Budget, repair and graph-integrity regressions for the new controller."""
from __future__ import annotations

import time

import pytest

from algorithm.knowledge_extract.v3_1 import ExtractionChunk, extract_aggregate
from algorithm.knowledge_extract.v3_1.controller import Controller, CallFailure
from algorithm.knowledge_extract.v3_1.evidence import locate, typed
from algorithm.knowledge_extract.v3_1.llm_client import thinking_disabled
from algorithm.knowledge_extract.v2.chunk_context import ChunkContextManager
from algorithm.knowledge_extract.v3_1.tests.test_semantic_contracts import (
    ScriptedLLM, SemanticDecider, _entity, _empty, _relation, _catalog_id,
)


def test_timeout_returns_without_waiting_for_late_sync_model_and_no_late_state_commit():
    def slow(_stage, _payload, _context):
        time.sleep(.2)
        return {"entities": []}

    controller = Controller(ScriptedLLM(slow), deadline=time.monotonic() + 1, timeout=.02, max_calls=1)
    start = time.monotonic()
    with pytest.raises(CallFailure, match="LLM_TIMEOUT"):
        controller.call("entities", "source-A", {})
    assert time.monotonic() - start < .12
    assert controller.attempts[0]["type"] == "timeout"
    time.sleep(.22)
    assert controller.late_responses == 1
    assert controller.attempts[0]["type"] == "timeout"


@pytest.mark.parametrize("start,end", [(0, 999), (True, 3), (0, True), (2, 1), (-1, 3)])
def test_evidence_offsets_are_real_unicode_intervals(start, end):
    context = ChunkContextManager([ExtractionChunk("source-A", "甲乙丙")])
    assert locate({"quote": "甲乙丙", "start_char": start, "end_char": end}, context, "source-A") == []


def test_typed_validation_does_not_extract_number_from_conditional_prose():
    assert not typed("至少10，最多20", "Integer")
    assert not typed(True, "Integer")
    assert not typed(float("nan"), "Number")
    assert typed([1, 2], "Integer[]")
    assert not typed([1, "2"], "Integer[]")


def test_default_thinking_is_independent_of_shared_config(monkeypatch):
    monkeypatch.delenv("KG_EXTRACT_V31_DISABLE_THINKING", raising=False)
    monkeypatch.setenv("KG_EXTRACT_LLM_DISABLE_THINKING", "0")
    assert thinking_disabled() is True
    monkeypatch.setenv("KG_EXTRACT_V31_DISABLE_THINKING", "0")
    assert thinking_disabled() is False


def test_attribute_enrichment_cannot_add_identity_without_rekeying():
    text = "甲的编号是X1，说明是存在。"
    ontology = "@startuml\nclass A {\ncode: String\nsummary: String\n}\n' @identity A: code\n@enduml"

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a", "A", "甲", text)]}
        if stage == "attributes":
            return {"updates": [{"candidate_id": _catalog_id(payload, "甲"), "properties": {"code": "X1"},
                                 "evidence": [{"chunk_id": "source-A", "quote": text}]}]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(), use_scheduler=False, max_repair_rounds=0)
    assert result["status"] == "partial"
    assert result["intermediate_model"]["objects"][0]["identity"] == "甲"
    assert "code" not in result["raw"]["entities"][0]["properties"]
    assert any(issue["code"] == "IDENTITY_CHANGE_REQUIRES_REPAIR" for issue in result["coverage_gaps"])


def test_repair_type_resolution_materializes_candidate_and_restarts_relations():
    text = "甲执行乙。"
    ontology = "@startuml\nclass A\nclass B\nclass Work\nB --> Work : executes\n@enduml"

    def select(task):
        return "UNKNOWN" if task.kind == "entity_type" else "supported"

    def model(stage, payload, _context):
        if stage == "entities":
            candidate = _entity("actor", "A", "甲", text)
            candidate["alternative_types"] = ["B"]
            return {"entities": [candidate, _entity("work", "Work", "乙", text)]}
        if stage == "repair":
            return {"decisions": [{"task_id": task["task_id"], "selected": "B", "context_complete": True,
                                   "evidence": [{"chunk_id": "source-A", "quote": text}]}
                                  for task in payload["tasks"] if task["kind"] == "entity_type"]}
        if stage == "relations" and any(e["type"] == "B" for e in payload["entities"]):
            return {"relations": [_relation(payload, "甲", "executes", "乙", text)]}
        return _empty(stage)

    llm = ScriptedLLM(model)
    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=llm,
                               decider=SemanticDecider(select), use_scheduler=False)
    assert result["status"] == "succeeded"
    assert {(entity["type"], entity["name"]) for entity in result["raw"]["entities"]} == {("B", "甲"), ("Work", "乙")}
    assert len(result["raw"]["relations"]) == 1
    assert sum(call["task"] == "relations" for call in llm.calls) == 2


def test_repair_entity_support_restarts_missing_attributes_and_relationships():
    text = "甲的职责为巡检，负责乙。"
    ontology = "@startuml\nclass A {\nrole: String\n}\nclass B\nA --> B : responsible\n@enduml"

    def select(task):
        return "insufficient" if task.kind == "entity_support" and any(claim.get("name") == "甲" for claim in task.state["claims"].values()) else "supported"

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a", "A", "甲", text), _entity("b", "B", "乙", text)]}
        if stage == "repair":
            return {"decisions": [{"task_id": task["task_id"], "selected": "supported", "context_complete": True,
                                   "evidence": [{"chunk_id": "source-A", "quote": text}]} for task in payload["tasks"]]}
        if stage == "attributes":
            return {"updates": [{"candidate_id": _catalog_id(payload, "甲"), "properties": {"role": "巡检"},
                                 "evidence": [{"chunk_id": "source-A", "quote": text}]}]}
        if stage == "relations" and any(e["name"] == "甲" for e in payload["entities"]):
            return {"relations": [_relation(payload, "甲", "responsible", "乙", text)]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(select), use_scheduler=False)
    assert result["status"] == "succeeded"
    assert next(entity for entity in result["raw"]["entities"] if entity["name"] == "甲")["properties"]["role"] == "巡检"
    assert len(result["raw"]["relations"]) == 1


def test_empty_repair_cannot_erase_unlinked_condition_on_repeated_validation():
    text = "只有故障时，甲才协助乙。"
    ontology = "@startuml\nclass A\nclass B\nclass Condition\nA --> B : assists\n@enduml"

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a", "A", "甲", text), _entity("b", "B", "乙", text), _entity("c", "Condition", "故障时", text)]}
        if stage == "relations":
            return {"relations": [_relation(payload, "甲", "assists", "乙", text,
                                              condition_status="represented", condition_entity_ids=[_catalog_id(payload, "故障时")])]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(), use_scheduler=False)
    assert result["status"] == "partial"
    assert not result["raw"]["relations"]
    assert any(issue["code"] == "CONDITION_NOT_LINKED" and issue["status"] == "unresolved" for issue in result["run_trace"]["issues"])


def test_declared_cardinality_conflict_is_not_a_valid_graph():
    text = "甲同时分配给乙和丙。"
    ontology = '@startuml\nclass A\nclass B\nA --> "0..1" B : assignedTo\n@enduml'

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a", "A", "甲", text), _entity("b", "B", "乙", text), _entity("c", "B", "丙", text)]}
        if stage == "relations":
            return {"relations": [_relation(payload, "甲", "assignedTo", name, text) for name in ["乙", "丙"]]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(), use_scheduler=False)
    assert result["status"] == "partial"
    assert result["verification"]["valid"] is False
    assert any(issue["code"] == "CARDINALITY_CONFLICT" for issue in result["coverage_gaps"])


def test_unrelated_repair_change_cannot_clear_existing_property_conflict():
    text = "甲的职责在两个记录中分别写为巡检和审批。乙也在场。"
    ontology = "@startuml\nclass A {\nrole: String\n}\nclass B\n@enduml"

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a1", "A", "甲", text, properties={"role": "巡检"}),
                                 _entity("a2", "A", "甲", text, properties={"role": "审批"})]}
        if stage == "repair":
            return {"entities": [_entity("b", "B", "乙", text)],
                    "resolved_issue_ids": [issue["id"] for issue in payload["issues"] if "id" in issue]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(lambda task: "UNKNOWN" if task.kind == "attribute_conflict" else "supported"),
                               use_scheduler=False)
    assert result["status"] == "partial"
    assert any(issue["code"] == "PROPERTY_CONFLICT" for issue in result["coverage_gaps"])


def test_primary_failure_is_cleared_only_after_actual_stage_reexecution():
    text = "甲存在。"
    ontology = "@startuml\nclass A\n@enduml"
    calls = []

    def model(stage, payload, _context):
        calls.append(stage)
        if stage == "entities":
            if calls.count("entities") == 1:
                raise ConnectionError("private endpoint detail")
            return {"entities": [_entity("a", "A", "甲", text)]}
        if stage == "repair":
            return {"resolved_issue_ids": [issue["id"] for issue in payload["issues"] if "id" in issue]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(), use_scheduler=False)
    assert result["status"] == "succeeded"
    assert result["error_code"] is None
    assert calls.count("entities") == 2
    assert any(attempt["type"] == "request" for attempt in result["attempts"])
    assert not any(attempt["terminal"] for attempt in result["attempts"])
    assert result["run_trace"]["recoveries"][0]["status"] == "completed"
