"""First-step (entity discovery) contracts at the real harness boundary.

Covered here: per-citation evidence isolation, the declared and honored
``context_requests`` channel, visibility of candidates excluded by an undecided
type, the relation-free entities-stage schema context, and the issue shape the
model is actually shown.
"""
from __future__ import annotations

import json

from algorithm.knowledge_extract.v2.chunk_context import ChunkContextManager, ExtractionChunk
from algorithm.knowledge_extract.v3_1 import extract_aggregate
from algorithm.knowledge_extract.v3_1.evidence import locate
from algorithm.knowledge_extract.v3_1.prompts import messages
from algorithm.knowledge_extract.v3_1.schema_context import build_schema_context
from algorithm.knowledge_extract.v3_1.tests.test_semantic_contracts import (
    ScriptedLLM, SemanticDecider, _empty, _entity,
)
from algorithm.knowledge_extract.v2.ontology_schema import OntologySchema


def test_entity_survives_one_unlocatable_citation_end_to_end():
    text = "甲组承担巡视。\n乙组承担复核。"
    ontology = "@startuml\nclass Duty\n@enduml"

    def model(stage, payload, _context):
        if stage == "entities":
            anchor = payload["anchor_chunk_id"]
            return {"entities": [
                {"candidate_id": "a", "type": "Duty", "name": "巡视职责", "properties": {},
                 # first citation resolves, second is a typo'd line number
                 "evidence": [{"span_id": f"{anchor}:L1"}, {"span_id": f"{anchor}:L99"}]}],
                "alias_pairs": [], "issues": []}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(), use_scheduler=False, max_repair_rounds=0)
    assert [entity["name"] for entity in result["raw"]["entities"]] == ["巡视职责"]
    assert result["intermediate_model"]["objects"][0]["evidence"] == [
        {"quote": text.splitlines(keepends=True)[0], "source_block_ids": ["source-A"],
         "start_char": 0, "end_char": len(text.splitlines(keepends=True)[0])}]
    assert not [issue for issue in result["run_trace"]["issues"]
                if issue["code"] == "INVALID_ENTITY_EVIDENCE_OR_NAME"]


def test_candidate_with_no_locatable_citation_is_still_rejected():
    text = "甲组承担巡视。"
    ontology = "@startuml\nclass Duty\n@enduml"

    def model(stage, payload, _context):
        if stage == "entities":
            anchor = payload["anchor_chunk_id"]
            return {"entities": [
                {"candidate_id": "a", "type": "Duty", "name": "巡视职责", "properties": {},
                 "evidence": [{"span_id": f"{anchor}:L99"}, {"quote": "原文未出现的证据"}]}],
                "alias_pairs": [], "issues": []}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(), use_scheduler=False, max_repair_rounds=0)
    assert result["raw"]["entities"] == []
    assert any(issue["code"] == "INVALID_ENTITY_EVIDENCE_OR_NAME" for issue in result["coverage_gaps"])


def test_discovery_context_request_is_recorded_and_read_by_the_repair_rerun():
    texts = ["甲组承担巡视。", "无关记录。", "无关记录。", "复核依据在末段：乙组承担复核。"]
    chunks = [ExtractionChunk(f"source-{index}", text) for index, text in enumerate(texts)]
    ontology = "@startuml\nclass Duty\n@enduml"
    seen = {"requests": 0}

    def model(stage, payload, _context):
        if stage == "entities":
            anchor = payload["anchor_chunk_id"]
            if anchor != "source-0":
                return {"entities": [], "alias_pairs": [], "issues": []}
            response = {"entities": [_entity("a", "Duty", "巡视职责", texts[0], chunk=anchor)],
                        "alias_pairs": [], "issues": []}
            if seen["requests"] == 0:
                # evidence for the same duty sits two chunks away, outside the +-1 window
                seen["requests"] += 1
                response["context_requests"] = ["source-3"]
            return response
        if stage == "repair":
            return {**_empty("repair"),
                    "resolved_issue_ids": [issue["id"] for issue in payload["issues"] if "id" in issue]}
        return _empty(stage)

    llm = ScriptedLLM(model)
    result = extract_aggregate(ontology, chunks, llm=llm, decider=SemanticDecider(), use_scheduler=False)
    requests = [call for call in llm.calls if call["task"] == "entities"
                and "source-3" in [source["chunk_id"] for source in call["input"]["sources"]]]
    issue = next(issue for issue in result["run_trace"]["issues"]
                 if issue["code"] == "ADDITIONAL_CONTEXT_REQUESTED")
    assert issue["chunk"] == "source-0" and issue["claim"]["chunk_ids"] == ["source-3"]
    # the request reaches the rerun as real source text, not as a silent drop
    assert requests and requests[-1]["input"]["anchor_chunk_id"] == "source-0"
    assert issue["status"] == "resolved"


def test_undecided_type_reports_the_exclusion_instead_of_only_the_open_question():
    ontology = "@startuml\nclass A\nclass B\nA --> B : uses\n@enduml"
    text = "甲使用乙。"

    def model(stage, payload, _context):
        if stage == "entities":
            anchor = payload["anchor_chunk_id"]
            ambiguous = _entity("a", "A", "甲", text, chunk=anchor)
            ambiguous["alternative_types"] = ["B"]
            return {"entities": [ambiguous, _entity("b", "B", "乙", text, chunk=anchor)],
                    "alias_pairs": [], "issues": []}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(lambda task: "UNKNOWN" if task.kind == "entity_type" else "supported"),
                               use_scheduler=False)
    exclusion = next(issue for issue in result["coverage_gaps"] if issue["code"] == "ENTITY_TYPE_UNRESOLVED")
    assert exclusion["chunk"] == "source-A"
    assert exclusion["claim"]["name"] == "甲" and exclusion["claim"]["types"] == ["A", "B"]
    assert exclusion["claim"]["task_ids"] == [
        record["task_id"] for record in result["run_trace"]["tasks"] if record["kind"] == "entity_type"]
    assert [entity["name"] for entity in result["raw"]["entities"]] == ["乙"]
    # 类型未决导致该候选被排除＝召回缺口，已发布的图仍然可信。
    assert result["status"] == "succeeded"
    assert result["verification"]["valid"] is True
    assert result["verification"]["coverage_complete"] is False


def test_type_exclusion_issue_retires_once_the_type_is_actually_decided():
    ontology = "@startuml\nclass A\nclass B\nA --> B : uses\n@enduml"
    text = "甲使用乙。"

    def model(stage, payload, _context):
        if stage == "entities":
            anchor = payload["anchor_chunk_id"]
            ambiguous = _entity("a", "A", "甲", text, chunk=anchor)
            ambiguous["alternative_types"] = ["B"]
            return {"entities": [ambiguous], "alias_pairs": [], "issues": []}
        if stage == "repair":
            return {**_empty("repair"),
                    "decisions": [{"task_id": task["task_id"], "selected": "B", "context_complete": True,
                                   "evidence": [{"chunk_id": "source-A", "quote": text}]}
                                  for task in payload["tasks"] if task["kind"] == "entity_type"]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(lambda task: "UNKNOWN" if task.kind == "entity_type" else "supported"),
                               use_scheduler=False)
    assert [entity["type"] for entity in result["raw"]["entities"]] == ["B"]
    assert result["status"] == "succeeded"
    assert next(issue for issue in result["run_trace"]["issues"]
                if issue["code"] == "ENTITY_TYPE_UNRESOLVED")["status"] == "resolved"


def test_entities_stage_receives_no_relation_signatures_while_relations_stage_does():
    ontology = "@startuml\nclass A {\ncode: String\n}\nclass B {\ncode: String\n}\nA --> B : uses\n@enduml"
    text = "甲使用乙。"

    def model(stage, payload, _context):
        if stage == "entities":
            anchor = payload["anchor_chunk_id"]
            return {"entities": [_entity("a", "A", "甲", text, chunk=anchor),
                                 _entity("b", "B", "乙", text, chunk=anchor)],
                    "alias_pairs": [], "issues": []}
        if stage == "relations":
            return {"relations": [], "context_requests": [], "issues": []}
        return _empty(stage)

    llm = ScriptedLLM(model)
    extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=llm,
                      decider=SemanticDecider(), use_scheduler=False, max_repair_rounds=0)
    by_stage = {call["task"]: call["input"]["schema_context"] for call in llm.calls}
    assert "relation_signatures" not in by_stage["entities"]
    assert by_stage["entities"]["entity_types"] and by_stage["entities"]["allowed_entity_types"] == ["A", "B"]
    assert [signature["rel_type"] for signature in by_stage["relations"]["relation_signatures"]] == ["uses"]
    assert [signature["rel_type"] for signature in by_stage["attributes"]["relation_signatures"]] == ["uses"]


def test_schema_context_can_omit_relations_without_losing_the_identifier_contract():
    schema = OntologySchema.parse("@startuml\nclass A\nclass B\nA --> B : uses\n@enduml")
    without = build_schema_context(schema, allowed_entity_types=["A", "B"], include_relations=False)
    assert set(without) == {"allowed_entity_types", "entity_types"}
    with_relations = build_schema_context(
        schema, allowed_entity_types=["A", "B"],
        candidates=[{"candidate_id": "A:甲", "type": "A"}])
    assert [signature["source_candidate_ids"] for signature in with_relations["relation_signatures"]] == [["A:甲"]]


def test_model_issue_shape_shows_a_real_boolean_and_the_declared_context_channel():
    _system, user = messages("entities", {})
    payload = json.loads(user["content"])
    assert payload["output_shape"]["issues"] == [{
        "kind": "source_observation|unresolved_fact", "affects_completeness": False,
        "message": "specific observation or unresolved issue",
        "evidence": [{"span_id": "exact ID from sources[].spans"}]}]
    assert payload["output_shape"]["context_requests"] == []
    assert "context_requests" in payload["instructions"]


def test_issue_shape_page_is_shared_by_every_stage():
    for stage in ("entities", "attributes", "relations", "resolve", "repair"):
        shape = json.loads(messages(stage, {})[1]["content"])["output_shape"]
        assert shape["issues"][0]["affects_completeness"] is False, stage


def test_locate_is_reused_verbatim_by_the_prompt_projection():
    """The same isolation rule must hold for the compact evidence_refs projection."""
    context = ChunkContextManager([ExtractionChunk("source-A", "甲组承担巡视。\n乙组承担复核。")])
    assert locate([{"span_id": "source-A:L1"}, {"span_id": "source-A:L404"}], context, "source-A") == [
        {"quote": "甲组承担巡视。\n", "source_block_ids": ["source-A"], "start_char": 0, "end_char": 8}]
