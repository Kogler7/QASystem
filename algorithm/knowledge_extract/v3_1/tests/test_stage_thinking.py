"""Stage policy survives retries and concurrent calls without mutating the client."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json
import threading

import pytest

from algorithm.knowledge_extract.v3_1.candidate_generation import generate_structured
from algorithm.knowledge_extract.v3_1.llm_client import OpenAICompatibleLLM


SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}}},
          "required": ["items"], "additionalProperties": False}
MESSAGES = [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": json.dumps({
    "task": {"operation": "entity"}, "domain_policy": "", "ontology": {},
    "documents": "source", "output_contract": {"success_schema": SCHEMA},
})}]


def response(text='{"items": ["ok"]}', finish="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def client_for(outcomes=None, **policy):
    calls = []
    pending = list(outcomes or [response()])
    def create(**kwargs):
        calls.append(kwargs)
        outcome = pending.pop(0) if len(pending) > 1 else pending[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return OpenAICompatibleLLM(sdk, model="deepseek-v4-flash-0731", max_tokens=128,
                               disable_thinking=True, **policy), calls


@pytest.mark.parametrize("stage,enabled", [
    ("entity", False), ("property", False), ("skill_selection", False),
    ("entity_verification", True), ("entity_admission", True), ("property_verification", True),
    ("evidence_verification", True), ("candidate_identity", True),
    ("property_conflict", True),
    ("repair_entity", True), ("repair_property", True),
    ("recover_entity", False), ("repair_json", False), ("unknown", False), (None, False),
])
def test_stage_selects_explicit_wire_parameter_and_trace(stage, enabled):
    llm, calls = client_for()
    result = llm.generate_structured(SCHEMA, MESSAGES, {"pass_name": stage})
    mode = "enabled" if enabled else "disabled"
    assert calls[0]["extra_body"] == {"thinking": {"type": mode}}
    assert result.to_trace()["thinking"] == {
        "requested": mode, "parameter": "thinking", "sent": mode, "fallback": False}
    assert llm.disable_thinking is True


def test_semantic_override_can_be_disabled_and_plain_callable_keeps_base_policy():
    llm, calls = client_for(semantic_thinking=False)
    llm.generate_structured(SCHEMA, MESSAGES, {"pass_name": "entity_verification"})
    llm(MESSAGES)
    assert all(c["extra_body"] == {"thinking": {"type": "disabled"}} for c in calls)


@pytest.mark.parametrize("stage", ["entity", "property", "skill_selection", "property_conflict", "repair_property"])
@pytest.mark.parametrize("failure", [
    TimeoutError("temporary network timeout"), response("broken JSON"),
    response('{"items": [3]}'), response('{"items": [', finish="length"),
])
def test_format_truncation_schema_and_network_retries_keep_original_stage(stage, failure):
    llm, calls = client_for([failure, response()])
    metrics = []
    result = generate_structured(llm, MESSAGES, SCHEMA, use_scheduler=False, attempts=[],
        pass_name=stage, chunk_id="c1", max_attempts=2, retry_base_seconds=0, call_metrics=metrics)
    assert result == {"items": ["ok"]}
    mode = "enabled" if stage in {"property_conflict", "repair_property"} else "disabled"
    assert len(calls) == 2
    assert [c["extra_body"] for c in calls] == [{"thinking": {"type": mode}}] * 2
    assert metrics[-1]["thinking"]["sent"] == mode


@pytest.mark.parametrize("parameter", ["thinking", "enable_thinking"])
@pytest.mark.parametrize("status", [None, 400, 422])
def test_only_explicit_unsupported_thinking_error_falls_back(parameter, status):
    failure = ValueError(f"unknown parameter {parameter}")
    failure.status_code = status
    llm, calls = client_for([failure, response()],
                            thinking_parameter=parameter)
    result = llm.generate_structured(SCHEMA, MESSAGES, {"pass_name": "entity_verification"})
    assert len(calls) == 2 and "extra_body" not in calls[1]
    assert result.thinking["requested"] == "enabled"
    assert result.thinking["sent"] == "provider_default" and result.thinking["fallback"]


@pytest.mark.parametrize("failure", [ValueError("unknown response_format"),
                                     TimeoutError("temporary network timeout")])
def test_unrelated_errors_do_not_trigger_thinking_capability_fallback(failure):
    llm, calls = client_for([failure, response()])
    with pytest.raises(type(failure)):
        llm.generate_structured(SCHEMA, MESSAGES, {"pass_name": "entity_verification"})
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_outages_and_auth_failures_do_not_trigger_thinking_capability_fallback(status):
    failure = RuntimeError("thinking parameter not supported")
    failure.status_code = status
    llm, calls = client_for([failure, response()])
    with pytest.raises(RuntimeError):
        llm.generate_structured(SCHEMA, MESSAGES, {"pass_name": "entity_verification"})
    assert len(calls) == 1


def test_provider_without_toggle_reports_provider_default():
    llm, calls = client_for(thinking_parameter="none")
    result = llm.generate_structured(SCHEMA, MESSAGES, {"pass_name": "entity_verification"})
    assert "extra_body" not in calls[0]
    assert result.thinking == {"requested": "enabled", "parameter": "none",
                               "sent": "provider_default", "fallback": False}


def test_property_conflict_synthesis_is_enabled_without_enabling_ordinary_completion():
    from ..chunk_context import ChunkContextManager, ExtractionChunk
    from ..ontology_compiler import compile_plan
    from ..prompt_router import resolve_document_route
    from ..property_enrichment import enrich_entity_properties
    from .stub_llm import StubLLM

    chunks = [ExtractionChunk("a", "甲负责检查。乙负责独立。"), ExtractionChunk("b", "甲负责记录。")]
    plan = compile_plan("@startuml\nclass Person {\n + 职责 (duty): String\n}\n@enduml")
    def complete(prompt, chunk):
        rows = []
        for item in json.loads(prompt)["task"]["known_entities"]:
            conflict = item["name"] == "甲"
            rows.append({"type": "Person", "name": item["name"],
                "properties": {"duty": "检查并记录" if conflict else "独立"},
                "evidence": {"quote": "甲负责检查。" if conflict else "乙负责独立。",
                    "property_quotes": {"duty": ["甲负责检查。", "甲负责记录。"] if conflict else ["乙负责独立。"]}}})
        return {"entities": rows}
    stub = StubLLM(property_handler=complete, verification_handler=lambda *_: "yes")
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return response(stub(kwargs["messages"]))
    llm = OpenAICompatibleLLM(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        model="deepseek-v4-flash-0731", max_tokens=512, disable_thinking=True)
    attempts, metrics = [], []
    rows, _ = enrich_entity_properties([
        ("a", [{"type": "Person", "name": "甲", "properties": {"duty": "检查"},
                "evidence": {"quote": "甲负责检查。"}},
               {"type": "Person", "name": "乙", "properties": {}, "evidence": {"quote": "乙负责独立。"}}]),
        ("b", [{"type": "Person", "name": "甲", "properties": {"duty": "记录"},
                "evidence": {"quote": "甲负责记录。"}}]),
    ], ChunkContextManager(chunks), plan, llm, route=resolve_document_route(plan, chunks),
       attempts=attempts, warnings=[], max_workers=2, max_attempts=1, call_metrics=metrics)
    assert not attempts
    assert {row["name"]: row["properties"] for row in rows} == {
        "甲": {"duty": "检查并记录"}, "乙": {"duty": "独立"}}
    seen = set()
    for call in calls:
        task = json.loads(call["messages"][-1]["content"])["task"]
        if task["operation"] == "property":
            names = {item["name"] for item in task["known_entities"]}
            assert len(names) == 1  # Different thinking policies do not share a batch.
            expected = "enabled" if names == {"甲"} else "disabled"
            assert call["extra_body"] == {"thinking": {"type": expected}}
            seen.update(names)
    assert seen == {"甲", "乙"}
    assert any(m["pass"] == "property_conflict" and m["thinking"]["sent"] == "enabled" for m in metrics)


@pytest.mark.parametrize("failure", [
    TimeoutError("temporary network timeout"), response("broken JSON"),
    response('{"type": 3}'), response('{"type":', finish="length"), None,
])
def test_post_failure_rediscovery_does_not_escalate_to_semantic_thinking(failure):
    from ..chunk_context import ExtractionChunk
    from ..split_extractor import extract_split
    from .stub_llm import StubLLM, span_item

    stub = StubLLM(repair_handler=lambda *_: {"entities": [span_item("Person", "甲")]})
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        task = json.loads(kwargs["messages"][-1]["content"])["task"]["operation"]
        if task == "entity":
            if isinstance(failure, Exception):
                raise failure
            return failure if failure is not None else response("{}")
        return response(stub(kwargs["messages"]))
    llm = OpenAICompatibleLLM(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        model="deepseek-v4-flash-0731", max_tokens=512, disable_thinking=True)
    result = extract_split("@startuml\nclass Person\n@enduml",
        [ExtractionChunk("c", "甲负责检查。", section_id="s")], llm=llm, skill_refs=[],
        max_attempts=1, max_repair_rounds=1, retry_base_seconds=0, best_effort=True)
    assert [row["name"] for row in result["raw"]["entities"]] == ["甲"]
    repaired = [c for c in calls if json.loads(c["messages"][-1]["content"])["task"]["operation"] == "repair"]
    assert len(repaired) == 1
    # A valid empty extraction creates a semantic coverage repair; protocol
    # failures remain recovery even if they also leave an uncovered block.
    expected = "enabled" if failure is None else "disabled"
    assert repaired[0]["extra_body"] == {"thinking": {"type": expected}}
    assert result["status"] == ("succeeded" if failure is None else "partial")


def test_concurrent_semantic_and_generation_calls_keep_independent_modes():
    stages = ["entity", "entity_verification", "property", "property_verification",
              "skill_selection", "candidate_identity", "entity", "repair_property"]
    barrier = threading.Barrier(len(stages))
    calls = []
    lock = threading.Lock()
    def create(**kwargs):
        barrier.wait(timeout=5)
        with lock:
            calls.append((kwargs["messages"][0]["content"], kwargs["extra_body"]))
        return response()
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    llm = OpenAICompatibleLLM(sdk, model="deepseek-v4-flash-0731", max_tokens=128, disable_thinking=True)
    def call(stage):
        return llm.generate_structured(SCHEMA, [{"role": "user", "content": stage}], {"pass_name": stage})
    with ThreadPoolExecutor(max_workers=len(stages)) as pool:
        results = list(pool.map(call, stages))
    for stage, body in calls:
        mode = "disabled" if stage in {"entity", "property", "skill_selection"} else "enabled"
        assert body == {"thinking": {"type": mode}}
    assert [r.thinking["requested"] for r in results] == [
        "disabled", "enabled", "disabled", "enabled", "disabled", "enabled", "disabled", "enabled"]
    assert llm.disable_thinking is True
