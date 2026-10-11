from copy import deepcopy
import json
from pathlib import Path
import time

import pytest

from algorithm.knowledge_extract.v2.ontology_schema import OntologySchema
from algorithm.knowledge_extract.v3_1 import ExtractionChunk, extract_aggregate
from algorithm.knowledge_extract.v3_1.controller import Controller
from algorithm.knowledge_extract.v3_1.harness import Harness
from algorithm.knowledge_extract.v3_1.evidence import locate
from algorithm.knowledge_extract.v3_1.prompts import messages
from algorithm.knowledge_extract.v3_1.tests.test_semantic_contracts import (
    ScriptedLLM, SemanticDecider, _entity, _empty, _relation, _catalog_id,
)


def fixture_harness():
    ontology = """@startuml
class A {
code: String
detail: String
when: String
negated: Boolean
}
' @identity A: code
A --> "0..1" A : refersTo
note right of A
跨片引用仍须保持条件和否定。
end note
@enduml"""
    old_text = "既有记录详细说明。" * 500
    current_text = "当前片段引用编号X1，只有设备断电时才执行。"
    chunks = [ExtractionChunk("source-A", old_text), ExtractionChunk("source-B", current_text)]
    schema = OntologySchema.parse(ontology)
    harness = Harness(ontology=ontology, chunks=chunks, schema=schema, allowed={"A"},
                      controller=Controller(None, deadline=time.monotonic() + 10, timeout=1, max_calls=1),
                      decider=SemanticDecider(), mode="permissive")
    evidence = locate({"quote": old_text}, harness.context, "source-A")
    for index in range(30):
        key = f"A::x{index}"
        harness.accept({"id": key, "type": "A", "identity": f"X{index}", "display_name": f"记录{index}",
                        "properties": {"code": f"X{index}", "detail": old_text, "when": "只有设备断电时", "negated": True},
                        "evidence": deepcopy(evidence), "source_chunk": "source-A", "proposal_id": f"source-A:e{index}",
                        "context_property_ids": ["when", "negated"]})
    return harness, old_text, current_text


def test_entity_discovery_payload_does_not_repeat_full_ontology_or_history():
    harness, old_text, current_text = fixture_harness()
    payload = harness.payload("source-B", stage="entities")
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["ontology"] == harness.ontology
    assert "ontology_contract" not in payload
    assert old_text not in serialized
    assert serialized.count(current_text) == 1
    assert all(set(entity["properties"]) == {"code", "when", "negated"} for entity in payload["entities"])
    assert all("evidence" not in entity and entity["evidence_refs"] for entity in payload["entities"])
    assert len(payload["entities"]) == 30  # No hidden top-k cutoff of cross-chunk aliases.


def test_relation_context_keeps_identity_and_llm_selected_condition_without_repeating_source():
    harness, old_text, _ = fixture_harness()
    payload = harness.payload("source-B", stage="relations", expanded=True)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert serialized.count(old_text) == 1
    assert all(entity["properties"] == {"code": f"X{index}", "when": "只有设备断电时", "negated": True}
               for index, entity in enumerate(payload["entities"]))
    assert all("detail" in entity["available_property_ids"] for entity in payload["entities"])
    assert '"0..1"' in payload["ontology"]
    assert "跨片引用仍须保持条件和否定" in payload["ontology"]


def test_attribute_context_only_sends_requested_entities_and_never_truncates_source():
    harness, old_text, current_text = fixture_harness()
    payload = harness.payload("source-B", stage="attributes", expanded=True, candidate_ids=["A::x1"])
    assert [entity["candidate_id"] for entity in payload["entities"]] == ["A::x1"]
    assert ["".join(span["text"] for span in source["spans"]) for source in payload["sources"]] == [old_text, current_text]


def test_repair_and_resolve_compact_evidence_using_original_source_references():
    harness, old_text, _ = fixture_harness()
    record = {"task_id": "r1", "kind": "relation_support", "claim": {
        "source": harness.objects["A::x1"], "target": {"candidate_id": "A::x2"}},
        "evidence": harness.objects["A::x1"]["evidence"]}
    compact = harness.prompt_record(record, "source-A")
    assert "evidence" not in compact
    assert compact["evidence_refs"][0]["end_char"] == len(old_text)
    assert "evidence" not in compact["claim"]["source"]
    assert harness.referenced_entity_ids(record) == ["A::x1", "A::x2"]


def test_malformed_context_selection_preserves_information_conservatively():
    harness, _, _ = fixture_harness()
    obj = harness.objects["A::x1"]
    assert set(harness.context_property_ids(obj, {"wrong": True})) == set(obj["properties"])


def test_verifier_contract_keeps_claimed_field_semantics_and_identity_only():
    harness, _, _ = fixture_harness()
    before = deepcopy(harness.contract)
    task = {"claim": {"type": "A", "properties": {"when": "断电时"}}}
    projected = harness.task_contract([task])
    assert {prop["id"] for prop in projected["classes"][0]["properties"]} == {"code", "when"}
    assert harness.contract == before
    choice = harness.task_contract([{"claim": {"types": ["A"], "properties": {"when": "断电时"}}}])
    assert {prop["id"] for prop in choice["classes"][0]["properties"]} == {"code", "when", "negated", "detail"}


def test_source_observation_is_retained_without_false_partial_or_repair():
    text = "此处按原文记录疑似笔误。"

    def model(stage, _payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a", "A", "原文事项", text)], "issues": [{
                "kind": "source_observation", "affects_completeness": False,
                "message": "存在疑似笔误但事实已按原文忠实保留，无未完成事项。",
                "evidence": [{"chunk_id": "source-A", "quote": text}],
            }]}
        return _empty(stage)

    llm = ScriptedLLM(model)
    result = extract_aggregate("@startuml\nclass A\n@enduml", [ExtractionChunk("source-A", text)],
                               llm=llm, decider=SemanticDecider(), use_scheduler=False)
    assert result["status"] == "succeeded"
    assert result["profile"]["source_observation_count"] == 1
    assert result["run_trace"]["issues"][0]["status"] == "observed"
    assert result["warnings"][0]["severity"] == "info"
    assert not any(call["task"] == "repair" for call in llm.calls)


@pytest.mark.parametrize("issue", [
    "原文疑似笔误但已忠实保留", {}, {"kind": "source_observation", "affects_completeness": False},
    {"kind": "source_observation", "affects_completeness": "false", "message": "提醒"},
    {"kind": "unknown", "affects_completeness": False, "message": "提醒"},
    {"kind": "unresolved_fact", "affects_completeness": True, "message": "关键条件无法表达"},
])
def test_unknown_or_blocking_issue_contract_stays_visible_as_a_coverage_gap(issue):
    text = "甲存在。"
    llm = ScriptedLLM(lambda stage, _payload, _context: {"entities": [_entity("a", "A", "甲", text)], "issues": [issue]}
                      if stage == "entities" else _empty(stage))
    result = extract_aggregate("@startuml\nclass A\n@enduml", [ExtractionChunk("source-A", text)],
                               llm=llm, decider=SemanticDecider(), use_scheduler=False, max_repair_rounds=0)
    # 声明式缺口不改图谱可信度，但必须报出来且保持 error 级别告警。
    assert any(value.get("code") == "SEMANTIC_SCOPE_UNRESOLVED" for value in result["coverage_gaps"])
    assert any(w.get("code") == "SEMANTIC_SCOPE_UNRESOLVED" and w.get("severity") == "error"
               for w in result["warnings"])
    assert result["verification"]["coverage_complete"] is False
    assert result["verification"]["valid"] is True
    assert result["status"] == "succeeded"


def test_informational_note_does_not_clear_a_real_unresolved_task():
    text = "甲可能存在。"

    def model(stage, _payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a", "A", "甲", text)]}
        if stage == "resolve":
            return {"decisions": [], "issues": [{"kind": "source_observation", "affects_completeness": False,
                                                   "message": "仅记录原文格式", "evidence": [{"chunk_id": "source-A", "quote": text}]}]}
        return _empty(stage)

    result = extract_aggregate("@startuml\nclass A\n@enduml", [ExtractionChunk("source-A", text)],
                               llm=ScriptedLLM(model), decider=SemanticDecider(lambda _: "insufficient"),
                               use_scheduler=False, max_repair_rounds=0)
    assert result["status"] == "failed"
    assert result["profile"]["source_observation_count"] == 1
    assert any(task["status"] == "unresolved" for task in result["run_trace"]["tasks"])


def test_resolve_reported_blocker_is_not_silently_ignored_after_supported_decision():
    text = "甲存在，但另有未完成条件。"

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a", "A", "甲", text)]}
        if stage == "resolve":
            return {"decisions": [{"task_id": task["task_id"], "selected": "supported", "context_complete": True,
                                   "evidence": [{"chunk_id": "source-A", "quote": text}]} for task in payload["tasks"]],
                    "issues": [{"kind": "unresolved_fact", "affects_completeness": True, "message": "另有条件无法表达"}]}
        return _empty(stage)

    result = extract_aggregate("@startuml\nclass A\n@enduml", [ExtractionChunk("source-A", text)],
                               llm=ScriptedLLM(model), decider=SemanticDecider(lambda _: "insufficient"),
                               use_scheduler=False, max_repair_rounds=0)
    assert result["raw"]["entities"]
    # 支持判定不能掩盖模型声明的缺口：缺口留在覆盖里，实体图照常发布。
    assert any(value.get("code") == "SEMANTIC_SCOPE_UNRESOLVED" for value in result["coverage_gaps"])
    assert result["verification"]["coverage_complete"] is False
    assert result["status"] == "succeeded"


@pytest.mark.parametrize("repair,other_blocker", [(False, False), (True, False), (True, True)])
def test_nonidentity_bad_array_keeps_entity_and_relations_and_requires_real_field_repair(repair, other_blocker):
    text = "甲亦称甲方、首方，甲负责乙。"
    ontology = "@startuml\nclass A {\naliases: String[] // 展示时分号分隔\n}\nclass B\nA --> B : responsible\n@enduml"

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a", "A", "甲", text, properties={"aliases": "甲方;首方"}), _entity("b", "B", "乙", text)],
                    "issues": ([{"kind": "unresolved_fact", "affects_completeness": True, "message": "另外一项作用域问题仍未解决"}]
                               if other_blocker else [])}
        if stage == "relations":
            return {"relations": [_relation(payload, "甲", "responsible", "乙", text)]}
        if stage == "repair" and repair:
            return {"updates": [{"candidate_id": _catalog_id(payload, "甲"), "properties": {"aliases": ["甲方", "首方"]},
                                 "context_property_ids": [], "evidence": [{"chunk_id": "source-A", "quote": text}]}]}
        return _empty(stage)

    decider = SemanticDecider()
    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=decider, use_scheduler=False, max_repair_rounds=int(repair))
    assert len(result["raw"]["entities"]) == 2
    assert result["raw"]["relations"]
    assert any(task.kind == "entity_support" for task in decider.tasks)
    assert not any(issue.get("code") == "INVALID_ENTITY_EVIDENCE_OR_NAME" for issue in result["run_trace"]["issues"])
    bad = next(issue for issue in result["run_trace"]["issues"] if issue["code"] == "INVALID_PROPERTY_SCHEMA")
    assert bad["status"] == ("resolved" if repair else "unresolved")
    entity = next(entity for entity in result["raw"]["entities"] if entity["name"] == "甲")
    assert entity["properties"] == ({"aliases": ["甲方", "首方"]} if repair else {})
    # 实体已发布；字段/作用域缺口只影响覆盖率，不否决整图。
    assert result["verification"]["valid"] is True
    assert result["verification"]["coverage_complete"] is (repair and not other_blocker)
    assert result["status"] == "succeeded"


def test_invalid_identity_array_remains_blocking_and_does_not_change_identity():
    text = "甲的代码包含X和Y。"
    ontology = "@startuml\nclass A {\ncode: String[]\n}\n' @identity A: code\n@enduml"
    llm = ScriptedLLM(lambda stage, _payload, _context: {"entities": [_entity("a", "A", "甲", text, properties={"code": "X;Y"})]}
                      if stage == "entities" else _empty(stage))
    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=llm,
                               decider=SemanticDecider(), use_scheduler=False, max_repair_rounds=0)
    assert result["status"] == "failed"
    assert not result["raw"]["entities"]
    assert any(issue["code"] == "INVALID_PROPERTY_SCHEMA" for issue in result["coverage_gaps"])
    assert not any(issue["code"] == "INVALID_ENTITY_EVIDENCE_OR_NAME" for issue in result["coverage_gaps"])


def test_empty_optional_array_field_is_missing_without_semantic_coercion():
    text = "甲存在。"
    llm = ScriptedLLM(lambda stage, _payload, _context: {"entities": [_entity("a", "A", "甲", text, properties={"aliases": ""})]}
                      if stage == "entities" else _empty(stage))
    result = extract_aggregate("@startuml\nclass A {\naliases: String[]\n}\n@enduml", [ExtractionChunk("source-A", text)],
                               llm=llm, decider=SemanticDecider(), use_scheduler=False)
    assert result["status"] == "succeeded"
    assert result["raw"]["entities"][0]["properties"] == {}


def test_related_catalog_resolves_chunk_local_and_multiple_alias_hops():
    harness, _, _ = fixture_harness()
    harness.aliases["source-A:old"] = "A::gone"
    harness.aliases["A::gone"] = "A::x1"
    assert harness.referenced_entity_ids({"candidate_id": "old"}, chunk="source-A") == ["A::x1"]
    assert harness.referenced_entity_ids({"candidate_id": "source-A:old"}, chunk="source-A") == ["A::x1"]


@pytest.mark.parametrize("requested,rerun", [("a", True), ("unknown-source", True), ("a", False)])
def test_new_repair_context_is_read_in_actual_stage_or_stays_unresolved(requested, rerun):
    chunks = [ExtractionChunk(key, f"片段{key}原文") for key in "abcd"]
    seen = []

    def model(stage, payload, context):
        chunk = context["chunk_id"]
        if stage == "entities" and chunk == "d":
            seen.append({source["chunk_id"] for source in payload["sources"]})
            response = {"entities": [_entity("x", "A", "事项", "片段d原文", chunk="d")]}
            if len(seen) == 1:
                response["issues"] = [{"kind": "unresolved_fact", "affects_completeness": True, "message": "需核验相关上下文"}]
            return response
        if stage == "repair":
            return {"context_requests": [requested], "resolved_issue_ids": [issue["id"] for issue in payload["issues"]] if rerun else []}
        return _empty(stage)

    result = extract_aggregate("@startuml\nclass A\n@enduml", chunks, llm=ScriptedLLM(model),
                               decider=SemanticDecider(), use_scheduler=False)
    if requested == "a" and rerun:
        assert "a" in seen[-1]
        assert result["status"] == "succeeded"
    else:
        assert result["status"] == "partial"
        assert any(issue["code"] == "REPAIR_CONTEXT_NOT_READ" for issue in result["coverage_gaps"])


def test_empty_entity_rerun_does_not_clear_an_invalid_field_by_omission():
    text = "甲又称甲方、首方。"
    seen = 0

    def model(stage, payload, _context):
        nonlocal seen
        if stage == "entities":
            seen += 1
            return {"entities": [_entity("a", "A", "甲", text, properties={"aliases": "甲方;首方"} if seen == 1 else {})]}
        if stage == "repair":
            return {"resolved_issue_ids": [issue["id"] for issue in payload["issues"]]}
        return _empty(stage)

    result = extract_aggregate("@startuml\nclass A {\naliases: String[]\n}\n@enduml", [ExtractionChunk("source-A", text)],
                               llm=ScriptedLLM(model), decider=SemanticDecider(), use_scheduler=False)
    assert seen == 2
    # 空重跑不能把无效字段标成已修复：字段问题仍在覆盖缺口里，实体本身照常发布。
    assert any(issue["code"] == "INVALID_PROPERTY_SCHEMA" and issue["status"] == "unresolved"
               for issue in result["coverage_gaps"])
    assert result["verification"]["coverage_complete"] is False
    assert result["status"] == "succeeded"


@pytest.mark.parametrize("fix_field", [False, True])
def test_compact_repair_rename_preserves_hidden_properties_and_reconciles_field_issue(fix_field):
    text = "甲方称为甲组织，也称首方；甲有详细说明。"
    ontology = "@startuml\nclass A {\naliases: String[]\ndetail: String\n}\n@enduml"

    def model(stage, payload, _context):
        if stage == "entities":
            item = _entity("a", "A", "甲方", text, properties={"aliases": "甲组织;首方", "detail": "甲有详细说明"})
            item["context_property_ids"] = []
            return {"entities": [item]}
        if stage == "repair":
            assert payload["entities"][0]["properties"] == {}
            return {"updates": [{"candidate_id": _catalog_id(payload, "甲方"), "name": "甲组织",
                                 "properties": {"aliases": ["甲组织", "首方"]} if fix_field else {},
                                 "context_property_ids": [], "evidence": [{"chunk_id": "source-A", "quote": text}]}]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)], llm=ScriptedLLM(model),
                               decider=SemanticDecider(), use_scheduler=False)
    assert len(result["raw"]["entities"]) == 1
    entity = result["raw"]["entities"][0]
    assert entity["name"] == "甲组织"
    assert entity["properties"]["detail"] == "甲有详细说明"
    issue = next(issue for issue in result["run_trace"]["issues"] if issue["code"] == "INVALID_PROPERTY_SCHEMA")
    assert issue["status"] == ("resolved" if fix_field else "unresolved")
    # 字段是否真被修复决定覆盖率，而不是已发布图谱的可信度。
    assert result["verification"]["coverage_complete"] is fix_field
    assert result["status"] == "succeeded"


def test_semantic_property_selection_normalizes_labels_and_unknown_preserves_context():
    harness, _, _ = fixture_harness()
    harness.schema = OntologySchema.parse("@startuml\nclass A {\n条件 (when): String\n说明 (detail): String\n}\n@enduml")
    obj = {"type": "A", "properties": {"when": "只有设备断电时", "detail": "说明"}}
    assert harness.context_property_ids(obj, ["条件"]) == ["when"]
    assert set(harness.context_property_ids(obj, ["unknown"])) == {"when", "detail"}
    assert set(harness.context_property_ids(obj, ["条件", "unknown"])) == {"when", "detail"}
    assert harness.context_property_ids(obj, []) == []


def test_cross_chunk_canonical_evidence_survives_compact_repair_rename():
    ontology = "@startuml\nclass A {\naliases: String[]\ndetail: String\n}\n@enduml"
    chunks = [ExtractionChunk("a", "甲方有详细说明。"), ExtractionChunk("b", "甲方也称甲组织、首方。")]

    def model(stage, payload, context):
        chunk = context["chunk_id"]
        if stage == "entities":
            props = {"detail": "甲方有详细说明"} if chunk == "a" else {"aliases": "甲组织;首方"}
            item = _entity("x", "A", "甲方", chunks[0 if chunk == "a" else 1].text, chunk=chunk, properties=props)
            item["context_property_ids"] = []
            return {"entities": [item]}
        if stage == "repair":
            return {"updates": [{"candidate_id": _catalog_id(payload, "甲方"), "name": "甲组织",
                                 "properties": {"aliases": ["甲组织", "首方"]}, "context_property_ids": [],
                                 "evidence": [{"chunk_id": "b", "quote": chunks[1].text}]}]}
        return _empty(stage)

    result = extract_aggregate(ontology, chunks, llm=ScriptedLLM(model), decider=SemanticDecider(), use_scheduler=False)
    assert result["status"] == "succeeded"
    assert result["raw"]["entities"][0]["name"] == "甲组织"
    assert result["raw"]["entities"][0]["properties"] == {"detail": "甲方有详细说明", "aliases": ["甲组织", "首方"]}
    evidence = result["intermediate_model"]["objects"][0]["evidence"]
    assert {source for item in evidence for source in item["source_block_ids"]} == {"a", "b"}
    assert all(chunks[0 if item["source_block_ids"] == ["a"] else 1].text[item["start_char"]:item["end_char"]] == item["quote"] for item in evidence)


@pytest.mark.parametrize("stage", ["resolve", "repair"])
def test_distant_nested_evidence_references_include_original_source(stage):
    chunks = [ExtractionChunk(key, f"片段{key}的完整原文。") for key in "abcd"]
    captured = []

    def model(actual_stage, payload, _context):
        assert actual_stage == stage
        captured.append(payload)
        return _empty(actual_stage)

    ontology = "@startuml\nclass A\n@enduml"
    harness = Harness(ontology=ontology, chunks=chunks, schema=OntologySchema.parse(ontology), allowed={"A"},
                      controller=Controller(ScriptedLLM(model), deadline=time.monotonic() + 10, timeout=1, max_calls=2),
                      decider=SemanticDecider(lambda _: "insufficient"), mode="permissive")
    nested = {"condition_entity": {"type": "A", "evidence": locate({"quote": chunks[0].text}, harness.context, "a")}}
    if stage == "resolve":
        task = harness.task("relation_support", nested, locate({"quote": chunks[3].text}, harness.context, "d"), "d")
        harness.decide([task])
        record = captured[0]["tasks"][0]
    else:
        harness.issue("SEMANTIC_SCOPE_UNRESOLVED", "d", "relations", claim=nested)
        harness.repair()
        record = captured[0]["issues"][0]
    sources = {source["chunk_id"]: "".join(span["text"] for span in source["spans"]) for source in captured[0]["sources"]}
    assert sources["a"] == chunks[0].text
    refs = record["claim"]["condition_entity"]["evidence_refs"]
    assert all(sources[ref["chunk_id"]][ref["start_char"]:ref["end_char"]] == chunks[0].text for ref in refs)
    assert json.dumps(captured[0], ensure_ascii=False).count(chunks[0].text) == 1


def test_compaction_keeps_quote_when_referenced_original_is_not_in_supplied_sources():
    harness, old_text, _ = fixture_harness()
    record = {"nested": {"evidence": harness.objects["A::x1"]["evidence"]}}
    compact = harness.prompt_record(record, "source-B", available_sources={"source-B"})
    assert compact["nested"]["evidence"][0]["quote"] == old_text
    assert "evidence_refs" not in compact["nested"]
