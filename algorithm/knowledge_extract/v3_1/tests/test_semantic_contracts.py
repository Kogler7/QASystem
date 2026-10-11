"""Semantic behavior at the real harness boundary, with deterministic model peers.

These fixtures describe what the models understood. They do not teach the
harness domain words, special relation names, or a second classifier in Python.
"""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from algorithm.knowledge_extract.v2.run_state import ExtractionError
from algorithm.knowledge_extract.v3_1 import ExtractionChunk, extract, extract_aggregate
from algorithm.knowledge_extract.v3_1.decisions import ClefDecider, DecisionResult
from external_services.clef import ClefServiceError


class ScriptedLLM:
    model = "deterministic-semantic-peer"

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def generate_structured(self, *, schema, messages, run_context=None):
        assert run_context and run_context.get("response_mode") == "plain", (
            "Each logical generation must be one visible request, without SDK schema fallback"
        )
        request = json.loads(messages[-1]["content"])
        self.calls.append(deepcopy({**request, "run_context": run_context}))
        result = self.handler(request["task"], request["input"], run_context or {})
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


class SemanticDecider:
    """Inject model judgements without replacing the harness admission logic."""

    def __init__(self, selector=None):
        self.selector = selector
        self.tasks = []

    def decide(self, tasks, *, deadline, trace):
        results = {}
        for task in tasks:
            self.tasks.append(task)
            selected = self.selector(task) if self.selector else (
                "same_entity" if "same_entity" in task.criteria else "supported"
            )
            assert selected in task.criteria, (selected, task.criteria)
            remaining = 0.005 / (len(task.criteria) - 1) if len(task.criteria) > 1 else 0
            probabilities = {key: 0.995 if key == selected else remaining for key in task.criteria}
            results[task.task_id] = DecisionResult(
                task_id=task.task_id, selected=selected, probabilities=probabilities,
                margin=0.995 - remaining,
            )
        return results


def _empty(stage):
    return {
        "entities": {"entities": [], "alias_pairs": [], "issues": []},
        "attributes": {"updates": [], "issues": []},
        "relations": {"relations": [], "context_requests": [], "issues": []},
        "resolve": {"decisions": [], "issues": []},
        "repair": {"entities": [], "updates": [], "alias_pairs": [], "relations": [],
                   "resolved_issue_ids": [], "context_requests": [], "issues": []},
    }[stage]


def _entity(local_id, type_id, name, text, *, chunk="source-A", properties=None):
    return {"candidate_id": local_id, "type": type_id, "name": name,
            "properties": properties or {}, "evidence": [{"chunk_id": chunk, "quote": text}]}


def _catalog_id(payload, name):
    return next(entity["candidate_id"] for entity in payload["entities"] if entity["name"] == name)


def _relation(payload, source, relation, target, text, *, chunk="source-A", **extra):
    return {"from_id": _catalog_id(payload, source), "rel_type": relation,
            "to_id": _catalog_id(payload, target), "evidence": [{"chunk_id": chunk, "quote": text}],
            "condition_status": "unconditional", **extra}


@pytest.mark.parametrize("source_type,target_type,relations", [
    ("AgentQ", "CommitmentZ", ("issuedBy", "defines")),
    ("X_731", "Y_482", ("R_014", "R_729")),
])
def test_arbitrary_ontology_and_semantic_names_allow_two_relations_for_one_pair(
    source_type, target_type, relations,
):
    """Neither a domain type list nor V2's relation exclusions are admission rules."""
    text = "小林负责每晚巡视库房，并对这项任务承担监督责任。"
    source_name, target_name = "夜间巡视负责人", "库房晚间巡查职责"
    assert source_name not in text and target_name not in text
    ontology = (f"@startuml\nclass {source_type}\nclass {target_type}\n"
                + "\n".join(f"{source_type} --> {target_type} : {name}" for name in relations)
                + "\n@enduml")

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a", source_type, source_name, text),
                                 _entity("b", target_type, target_name, text)]}
        if stage == "relations":
            return {"relations": [_relation(payload, source_name, rel, target_name, text)
                                  for rel in relations]}
        return _empty(stage)

    decider = SemanticDecider()
    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)],
                               llm=ScriptedLLM(model), decider=decider, use_scheduler=False)

    assert result["status"] == "succeeded"
    assert {entity["name"] for entity in result["raw"]["entities"]} == {source_name, target_name}
    assert {relation["rel_type"] for relation in result["raw"]["relations"]} == set(relations)
    assert len(result["graph"]["relationships"]) == 2
    assert all(rel["from_name"] == source_name and rel["to_name"] == target_name
               for rel in result["raw"]["relations"])
    assert sum(task.kind == "relation_support" for task in decider.tasks) >= 2


def test_negative_conditional_facts_keep_their_public_condition_and_both_relations():
    ontology = """@startuml
class Constraint {
  + 适用条件 (when): String
  + 否定 (negated): Boolean
}
class Item
Constraint --> Item : governs
Constraint --> Item : inhibits
@enduml"""
    text = "温度低于零度时，不允许启动泵甲。"

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [
                _entity("rule", "Constraint", "低温启动限制", text,
                        properties={"when": "温度低于零度", "negated": True}),
                _entity("item", "Item", "泵甲", text),
            ]}
        if stage == "relations":
            rule_id = _catalog_id(payload, "低温启动限制")
            return {"relations": [
                _relation(payload, "低温启动限制", name, "泵甲", text,
                          condition_status="represented", condition_entity_ids=[rule_id])
                for name in ("governs", "inhibits")
            ]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)],
                               llm=ScriptedLLM(model), decider=SemanticDecider(), use_scheduler=False)

    assert result["status"] == "succeeded"
    rule = next(entity for entity in result["raw"]["entities"] if entity["type"] == "Constraint")
    assert rule["properties"]["when"] == "温度低于零度"
    assert rule["properties"]["negated"] is True
    assert {relation["rel_type"] for relation in result["raw"]["relations"]} == {"governs", "inhibits"}


def test_unrepresented_condition_does_not_become_an_unconditional_edge():
    ontology = "@startuml\nclass A\nclass B\nA --> B : assists\n@enduml"
    text = "只有故障时，甲组才协助乙组。"

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a", "A", "甲组", text), _entity("b", "B", "乙组", text)]}
        if stage == "relations":
            return {"relations": [_relation(payload, "甲组", "assists", "乙组", text,
                                               condition_status="unrepresented")]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)],
                               llm=ScriptedLLM(model), decider=SemanticDecider(), use_scheduler=False)

    assert result["raw"]["relations"] == []
    assert len(result["raw"]["entities"]) == 2
    # 条件无法表达 → 该关系不进图（召回缺口），实体图谱本身仍然可信。
    assert result["status"] == "succeeded"
    assert result["verification"]["valid"] is True
    assert result["verification"]["coverage_complete"] is False


def test_locatable_quote_is_not_enough_when_semantic_judge_contradicts_relation():
    ontology = "@startuml\nclass Team\nclass Work\nTeam --> Work : responsibleFor\n@enduml"
    text = "甲组不负责巡检。"

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("t", "Team", "甲组", text), _entity("w", "Work", "巡检", text)]}
        if stage == "relations":
            return {"relations": [_relation(payload, "甲组", "responsibleFor", "巡检", text)]}
        return _empty(stage)

    decider = SemanticDecider(lambda task: "contradicted" if task.kind == "relation_support" else "supported")
    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)],
                               llm=ScriptedLLM(model), decider=decider, use_scheduler=False)

    assert result["status"] == "succeeded"
    assert len(result["raw"]["entities"]) == 2
    assert result["raw"]["relations"] == []
    assert any(task.kind == "relation_support" for task in decider.tasks)


def test_insufficient_relation_is_escalated_and_supported_by_expanded_original_evidence():
    ontology = "@startuml\nclass Team\nclass Work\nTeam --> Work : responsibleFor\n@enduml"
    text = "甲组承担巡检。此项工作由该组每日执行。"
    relation_questions = []

    def choose(task):
        if task.kind == "relation_support":
            relation_questions.append(task)
            return "insufficient" if len(relation_questions) == 1 else "supported"
        return "supported"

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("t", "Team", "甲组", text), _entity("w", "Work", "巡检", text)]}
        if stage == "relations":
            return {"relations": [_relation(payload, "甲组", "responsibleFor", "巡检", "此项工作由该组每日执行。") ]}
        if stage == "resolve":
            return {"decisions": [{"task_id": task["task_id"], "selected": "supported",
                                    "context_complete": True,
                                    "evidence": [{"chunk_id": "source-A", "quote": text}]}
                                   for task in payload["tasks"]]}
        return _empty(stage)

    llm = ScriptedLLM(model)
    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)],
                               llm=llm, decider=SemanticDecider(choose), use_scheduler=False)

    assert any(call["task"] == "resolve" for call in llm.calls)
    assert result["status"] == "succeeded"
    assert len(result["raw"]["relations"]) == 1
    assert result["raw"]["relations"][0]["evidence"]["quote"] == text
    resolve_input = json.dumps(next(call["input"] for call in llm.calls if call["task"] == "resolve"))
    assert "probabilities" not in resolve_input


def test_semantic_alias_merge_preserves_relation_endpoints_and_evidence():
    ontology = "@startuml\nclass Unit\nclass Asset\nUnit --> Asset : maintains\n@enduml"
    text = "运维中心（以下简称中心）维护泵甲。"

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("full", "Unit", "运维中心", text),
                                 _entity("alias", "Unit", "中心", text),
                                 _entity("pump", "Asset", "泵甲", text)],
                    "alias_pairs": [{"left": "full", "right": "alias"}]}
        if stage == "relations":
            units = [entity for entity in payload["entities"] if entity["type"] == "Unit"]
            assert len(units) == 1, "Relations must use the semantically merged entity catalog"
            # An in-flight proposal may still refer to the pre-merge candidate ID.
            return {"relations": [{"from_id": "alias", "rel_type": "maintains", "to_id": "pump",
                                    "evidence": [{"chunk_id": "source-A", "quote": text}]}]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)],
                               llm=ScriptedLLM(model), decider=SemanticDecider(), use_scheduler=False)

    assert result["status"] == "succeeded"
    assert len(result["raw"]["entities"]) == 2
    assert len(result["raw"]["relations"]) == 1
    accepted_ids = {obj["id"] for obj in result["intermediate_model"]["objects"]}
    relation = result["intermediate_model"]["relations"][0]
    assert {relation["from"], relation["to"]} <= accepted_ids
    graph_uids = {node["uid"] for node in result["graph"]["nodes"]}
    edge = result["graph"]["relationships"][0]
    assert {edge["from_uid"], edge["to_uid"]} <= graph_uids
    assert edge["evidences"][0]["source_block_ids"] == ["source-A"]


def test_clef_http_failure_fully_resolved_by_llm_does_not_leave_terminal_failure():
    ontology = "@startuml\nclass Responsibility\n@enduml"
    text = "夜班人员每天检查备用电源。"

    class UnavailableClef:
        def systemone(self, request, *, timeout=None):
            raise ClefServiceError("service unavailable", error_kind="service_unavailable",
                                   status_code=503, retryable=False)

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("duty", "Responsibility", "备用电源夜间检查职责", text)]}
        if stage == "resolve":
            return {"decisions": [{"task_id": task["task_id"], "selected": "supported",
                                    "context_complete": True,
                                    "evidence": [{"chunk_id": "source-A", "quote": text}]}
                                   for task in payload["tasks"]]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)],
                               llm=ScriptedLLM(model), decider=ClefDecider(UnavailableClef()),
                               use_scheduler=False)

    assert result["status"] == "succeeded"
    assert not result.get("error_code")
    assert len(result["raw"]["entities"]) == 1
    serialized = json.dumps(result, ensure_ascii=False)
    assert "service_unavailable" in serialized
    assert not any(attempt.get("terminal") for attempt in result["attempts"] if attempt.get("type") != "success")


def test_failed_original_chunk_is_partial_and_public_extract_refuses_to_hide_it():
    ontology = "@startuml\nclass Duty\n@enduml"
    chunks = [ExtractionChunk("first", "甲组巡视站房。"), ExtractionChunk("second", "乙组检查电源。")]

    def model(stage, payload, context):
        if stage == "entities":
            if context["chunk_id"] == "second":
                raise TimeoutError("provider timeout")
            return {"entities": [_entity("a", "Duty", "站房巡视职责", chunks[0].text, chunk="first")]}
        return _empty(stage)

    result = extract_aggregate(ontology, chunks, llm=ScriptedLLM(model),
                               decider=SemanticDecider(), use_scheduler=False)

    assert result["status"] == "partial"
    assert len(result["raw"]["entities"]) == 1
    source_failures = [attempt for attempt in result["attempts"] if attempt.get("terminal")]
    assert source_failures and all(attempt.get("chunk") == "second" for attempt in source_failures)
    assert result["raw"]["entities"][0]["evidence"]["source_block_ids"] == ["first"]
    with pytest.raises(ExtractionError):
        extract(chunks, ontology, llm=ScriptedLLM(model), decider=SemanticDecider(), use_scheduler=False)


@pytest.mark.parametrize("bad_evidence", [
    [{"chunk_id": {"unexpected": "mapping"}, "quote": "原文中的职责。"}],
    [{"chunk_id": ["source-A"], "quote": "原文中的职责。"}],
    [{"chunk_id": "source-A", "quote": ["not a string"]}],
    [{"chunk_id": "source-A", "quote": "原文未出现的证据"}],
    [{"chunk_id": "source-A", "quote": "原文中的职责。", "start_char": "0", "end_char": 8}],
    [{"source_block_ids": "source-A", "quote": "原文中的职责。"}],
    "not a list or evidence object",
], ids=["mapping-chunk-id", "list-chunk-id", "list-quote", "invented-quote",
        "string-offset", "string-source-ids", "string-evidence"])
def test_malformed_model_evidence_cannot_abort_other_valid_facts(bad_evidence):
    ontology = "@startuml\nclass Duty\n@enduml"
    text = "甲组承担巡视。原文中的职责。"

    def model(stage, payload, _context):
        if stage == "entities":
            bad = _entity("bad", "Duty", "无效候选", text)
            bad["evidence"] = bad_evidence
            return {"entities": [_entity("good", "Duty", "巡视职责", text), bad]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)],
                               llm=ScriptedLLM(model), decider=SemanticDecider(), use_scheduler=False)

    assert [entity["name"] for entity in result["raw"]["entities"]] == ["巡视职责"]
    assert result["status"] in {"partial", "succeeded"}
    assert result["warnings"] or result.get("coverage_gaps") or not result["verification"]["valid"]
