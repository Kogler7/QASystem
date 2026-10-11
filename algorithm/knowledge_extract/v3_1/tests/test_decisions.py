from __future__ import annotations

import copy
import json
import threading
import time

import pytest

from algorithm.knowledge_extract.v3_1 import decisions
from algorithm.knowledge_extract.v3_1.decisions import ClefDecider, DecisionTask
from external_services.clef import ClefServiceError


CRITERIA = {"supported": "Supported", "contradicted": "Contradicted", "insufficient": "Not enough evidence"}


def task(identifier="q1", *, state=None, criteria=None, instructions="Judge the candidate using only its evidence"):
    return DecisionTask(identifier, "relation_support", state or {"evidence": "private text"},
                        instructions, criteria or dict(CRITERIA), ("chunk-1",))


def response(request):
    return {"model": "clef-flash", "answers": {
        key: {"type": "choice", "choice": "supported", "confidence": 0.001,
              "probabilities": {"supported": 0.97, "contradicted": 0.01, "insufficient": 0.02}}
        for key in request["questions"]
    }, "usage": {"input_tokens": 100, "output_tokens": 0}}


class FakeClient:
    model = "clef-flash"

    def __init__(self, handler=response):
        self.handler = handler
        self.requests = []
        self.timeouts = []

    def systemone(self, request, *, timeout):
        self.requests.append(copy.deepcopy(request))
        self.timeouts.append(timeout)
        return self.handler(request)


def run(client, tasks=None, *, timeout=10, max_calls=128, seconds=2):
    decider = ClefDecider(client, timeout=timeout, max_calls=max_calls)
    trace = []
    result = decider.decide(tasks or [task()], deadline=time.monotonic() + seconds, trace=trace)
    return result, trace, decider


def test_choice_uses_distribution_not_confidence_and_keeps_semantic_thresholds_out():
    def handler(request):
        body = response(request)
        body["answers"]["q1"].update(
            choice="insufficient", confidence=1.0,
            probabilities={"supported": 0.34, "contradicted": 0.31, "insufficient": 0.35},
        )
        return body

    result, trace, decider = run(FakeClient(handler))
    answer = result["q1"]
    assert answer.status == "decided"
    assert answer.selected == "insufficient"
    assert answer.margin == pytest.approx(0.01)
    assert answer.probabilities["insufficient"] == 0.35
    assert decider.calls == 1
    assert len([item for item in trace if item["type"] == "request"]) == 1
    assert all(item["source_chunk_ids"] == ["chunk-1"] for item in trace)
    assert "private text" not in json.dumps(trace)


def test_equal_state_batched_up_to_four_and_other_states_stay_separate():
    client = FakeClient()
    tasks = [task(str(i)) for i in range(18)] + [task("separate", state={"evidence": "other"})]
    results, _, decider = run(client, tasks)
    # 单次调用的问题数上限跟服务端 CLEF_MAX_QUESTIONS（16）一致。
    assert list(map(lambda request: len(request["questions"]), client.requests)) == [16, 2, 1]
    assert set(results) == {item.task_id for item in tasks}
    assert all(item.status == "decided" for item in results.values())
    assert decider.calls == 3


def test_decider_rejects_batches_beyond_the_service_limit():
    with pytest.raises(ValueError, match="between 1 and 16"):
        ClefDecider(FakeClient(), max_questions=17)


def test_runtime_cache_covers_state_question_criteria_order_and_kind():
    client = FakeClient()
    decider = ClefDecider(client)
    trace = []
    for item in [task(), task("q2"), task("q3", instructions="Changed question"),
                 task("q4", state={"evidence": "Changed source"}),
                 task("q5", criteria=dict(reversed(list(CRITERIA.items()))))]:
        results = decider.decide([item], deadline=time.monotonic() + 2, trace=trace)
        assert results[item.task_id].status == "decided"
        if item.task_id == "q2":
            assert results["q2"].details["cache_hit"] is True
    assert decider.calls == 4
    # Cache belongs to one document decider, never a global cross-user cache.
    assert run(FakeClient())[2].calls == 1


@pytest.mark.parametrize("mutate", [
    lambda body: body.update(answers={}),
    lambda body: body["answers"].update(extra=body["answers"]["q1"]),
    lambda body: body.update(model="different"),
    lambda body: body.update(error="private remote detail"),
    lambda body: body["answers"]["q1"].update(type="noul"),
    lambda body: body["answers"]["q1"].update(choice="contradicted"),
    lambda body: body["answers"]["q1"].update(choice=["supported"]),
    lambda body: body["answers"]["q1"].pop("probabilities"),
    lambda body: body["answers"]["q1"]["probabilities"].pop("contradicted"),
    lambda body: body["answers"]["q1"]["probabilities"].update(extra=0),
    lambda body: body["answers"]["q1"]["probabilities"].update(supported=float("nan")),
    lambda body: body["answers"]["q1"]["probabilities"].update(supported=float("inf")),
    lambda body: body["answers"]["q1"]["probabilities"].update(supported=-1),
    lambda body: body["answers"]["q1"]["probabilities"].update(supported=10**400),
    lambda body: body["answers"]["q1"]["probabilities"].update(supported=True),
    lambda body: body["answers"]["q1"]["probabilities"].update(supported="0.97"),
    lambda body: body["answers"]["q1"]["probabilities"].update(supported=0.8),
])
def test_invalid_responses_are_unavailable_without_retry_or_negative_decision(mutate):
    def handler(request):
        body = response(request)
        mutate(body)
        return body

    result, trace, decider = run(FakeClient(handler))
    assert result["q1"].status == "unavailable"
    assert result["q1"].selected is None
    assert result["q1"].reason == "invalid_response"
    assert decider.calls == 1
    assert "private remote detail" not in json.dumps(trace)


def test_four_decimal_rounding_and_ties_are_valid():
    def handler(request):
        body = response(request)
        body["answers"]["q1"]["probabilities"] = dict.fromkeys(CRITERIA, 0.3333)
        return body

    result, _, _ = run(FakeClient(handler))
    assert result["q1"].status == "decided"
    assert result["q1"].margin == 0


@pytest.mark.parametrize("status", [413, 422])
def test_overlong_batches_split_at_most_two_levels_without_truncating_state(status):
    def handler(request):
        if len(request["questions"]) > 1:
            raise ClefServiceError("redacted", error_kind="input_too_long", status_code=status)
        return response(request)

    client = FakeClient(handler)
    results, trace, decider = run(client, [task(str(i)) for i in range(4)])
    assert decider.calls == 7
    assert [len(item["questions"]) for item in client.requests] == [4, 2, 1, 1, 2, 1, 1]
    assert all(item["state"] == {"evidence": "private text"} for item in client.requests)
    assert all(answer.status == "decided" for answer in results.values())
    assert len([item for item in trace if item["type"] == "request"]) == 7


def test_single_question_overflow_is_unavailable_for_context_rebuild_by_harness():
    def handler(_):
        raise ClefServiceError("redacted", error_kind="input_too_long", status_code=422)

    result, _, decider = run(FakeClient(handler))
    assert result["q1"].reason == "input_too_long"
    assert decider.calls == 1


def projection_state(count=4, quote_size=600):
    return {
        "claims": {str(i): {"candidate_id": str(i), "name": f"Entity {i}"} for i in range(count)},
        "evidence_sets": {str(i): [{"chunk_id": "chunk-1", "quote": str(i) * quote_size,
                                    "start": i * quote_size, "end": (i + 1) * quote_size}]
                          for i in range(count)},
        "ontology_context": {"classes": [{"id": "Entity", "description": "Shared class semantics"}],
                             "relations": [{"id": "associated", "description": "Shared relation semantics"}]},
        "shared_context": {"condition": "Preserve every common condition"},
        "__clef_task_projection__": {"version": 1, "tasks": {
            str(i): {"claim_ids": [str(i)], "evidence_set_ids": [str(i)]} for i in range(count)
        }},
    }


def test_token_overflow_splits_explicit_claim_and_evidence_dependencies_with_questions():
    state = projection_state()
    before = copy.deepcopy(state)

    def handler(request):
        # Reject total state size: splitting only the questions would never fix this.
        if len(json.dumps(request["state"])) > 2200:
            raise ClefServiceError("redacted", error_kind="input_too_long", status_code=422)
        return response(request)

    client = FakeClient(handler)
    results, _, decider = run(client, [task(str(i), state=state) for i in range(4)])
    assert decider.calls == 3
    assert all(answer.status == "decided" for answer in results.values())
    assert [len(request["state"]["claims"]) for request in client.requests] == [4, 2, 2]
    for request in client.requests:
        sent = request["state"]
        assert "__clef_task_projection__" not in sent
        assert set(sent["claims"]) == set(request["questions"])
        assert set(sent["evidence_sets"]) == set(request["questions"])
        assert sent["ontology_context"] == state["ontology_context"]
        assert sent["shared_context"] == state["shared_context"]
        for question_id in request["questions"]:
            assert sent["claims"][question_id] == state["claims"][question_id]
            assert sent["evidence_sets"][question_id] == state["evidence_sets"][question_id]
    assert state == before


def test_projection_preserves_shared_explicit_dependencies():
    state = projection_state(count=2)
    state["evidence_sets"]["common"] = [{"quote": "A shared condition must not disappear"}]
    for dependencies in state["__clef_task_projection__"]["tasks"].values():
        dependencies["evidence_set_ids"].append("common")

    def handler(request):
        if len(request["state"]["claims"]) > 1:
            raise ClefServiceError("redacted", error_kind="input_too_long", status_code=422)
        return response(request)

    client = FakeClient(handler)
    result, _, _ = run(client, [task(str(i), state=state) for i in range(2)])
    assert all(answer.status == "decided" for answer in result.values())
    for request in client.requests:
        assert request["state"]["evidence_sets"]["common"] == state["evidence_sets"]["common"]


@pytest.mark.parametrize("opt_in", [False, True])
def test_unprojectable_or_individually_long_state_remains_complete_and_unavailable(opt_in):
    state = projection_state(quote_size=3000 if opt_in else 600)
    if not opt_in:
        state.pop("__clef_task_projection__")

    def handler(request):
        if len(json.dumps(request["state"])) > 2200:
            raise ClefServiceError("redacted", error_kind="input_too_long", status_code=422)
        return response(request)

    client = FakeClient(handler)
    results, _, decider = run(client, [task(str(i), state=state) for i in range(4)])
    assert decider.calls == 7
    assert all(answer.reason == "input_too_long" for answer in results.values())
    for request in client.requests:
        sent = request["state"]
        if not opt_in:
            assert sent == state  # Matching field names alone never authorize pruning.
        else:
            for identifier in sent["evidence_sets"]:
                assert sent["evidence_sets"][identifier] == state["evidence_sets"][identifier]


@pytest.mark.parametrize("mutate", [
    lambda state: state["__clef_task_projection__"].update(version=True),
    lambda state: state["__clef_task_projection__"].update(version=2),
    lambda state: state["__clef_task_projection__"]["tasks"].pop("0"),
    lambda state: state["__clef_task_projection__"]["tasks"]["0"].update(claim_ids=["missing"]),
    lambda state: state["__clef_task_projection__"]["tasks"]["0"].update(evidence_set_ids=[]),
])
def test_invalid_projection_declaration_fails_before_http(mutate):
    state = projection_state(count=1)
    mutate(state)
    client = FakeClient()
    result, _, decider = run(client, [task("0", state=state)])
    assert result["0"].reason == "invalid_state_projection"
    assert decider.calls == 0


def test_real_harness_recovers_size_rejection_without_discarding_evidence():
    from algorithm.knowledge_extract.v3_1 import ExtractionChunk, extract_aggregate

    text = "Four records are A, B, C, and D. " + "Shared source context. " * 25

    class Generator:
        def generate_structured(self, *, schema, messages, run_context=None):
            request = json.loads(messages[-1]["content"])
            if request["task"] == "entities":
                return {"entities": [
                    {"candidate_id": name, "type": "Record", "name": name, "properties": {},
                     "evidence": [{"chunk_id": "source-A", "quote": text}]}
                    for name in "ABCD"
                ]}
            assert request["task"] not in {"resolve", "repair"}, "Complete split decisions must not require recovery"
            return {"updates": [], "relations": [], "issues": [], "context_requests": []}

    requests = []
    rejected_sizes = []

    class SizedService:
        def systemone(self, request, *, timeout=None):
            requests.append(copy.deepcopy(request))
            state = request["state"]
            size = len(json.dumps(state))
            if size > 3000:
                rejected_sizes.append(size)
                raise ClefServiceError("redacted", error_kind="input_too_long", status_code=422)
            for question_id in request["questions"]:
                assert state["evidence_sets"][question_id][0]["quote"] == text
            return response(request)

    result = extract_aggregate(
        "@startuml\nclass Record\n@enduml", [ExtractionChunk("source-A", text)],
        llm=Generator(), clef_client=SizedService(), use_scheduler=False, max_repair_rounds=0,
    )
    assert rejected_sizes, [(len(request["questions"]), len(json.dumps(request["state"]))) for request in requests]
    assert result["status"] == "succeeded"
    assert {entity["name"] for entity in result["raw"]["entities"]} == set("ABCD")
    assert len(requests[0]["questions"]) == 4
    assert any(len(request["state"]["claims"]) < 4 for request in requests[1:])


@pytest.mark.parametrize("kind,status", [("schema_error", 422), ("http_error", 401), ("configuration", None)])
def test_nonretryable_errors_preserved_without_retry(kind, status):
    def handler(_):
        raise ClefServiceError("private secret", error_kind=kind, status_code=status)

    result, trace, decider = run(FakeClient(handler))
    assert result["q1"].reason == kind
    assert result["q1"].status == "unavailable"
    assert decider.calls == 1
    assert "private secret" not in json.dumps(trace)


@pytest.mark.parametrize("kind,status", [("rate_limited", 429), ("service_unavailable", 503), ("timeout", None), ("connection", None)])
def test_retry_at_most_once_and_never_turn_failure_into_false(kind, status):
    def handler(_):
        raise ClefServiceError("private secret", error_kind=kind, status_code=status, retryable=True)

    result, trace, decider = run(FakeClient(handler))
    assert decider.calls == 2
    assert result["q1"].status == "unavailable"
    assert result["q1"].selected is None
    assert result["q1"].details["call_indices"] == [1, 2]
    assert "private secret" not in json.dumps(trace)


def test_429_respects_retry_after_then_can_recover():
    starts = []

    def handler(request):
        starts.append(time.monotonic())
        if len(starts) == 1:
            raise ClefServiceError("redacted", error_kind="rate_limited", status_code=429,
                                   retryable=True, retry_after=0.02)
        return response(request)

    result, _, decider = run(FakeClient(handler))
    assert decider.calls == 2
    assert starts[1] - starts[0] >= 0.02
    assert result["q1"].status == "decided"


def test_retry_after_beyond_deadline_does_not_sleep_or_retry():
    def handler(_):
        raise ClefServiceError("redacted", error_kind="rate_limited", status_code=429,
                               retryable=True, retry_after=60)

    started = time.monotonic()
    result, _, decider = run(FakeClient(handler), seconds=0.5)
    assert time.monotonic() - started < 0.3
    assert decider.calls == 1
    assert result["q1"].reason == "deadline_exhausted"


def test_shared_call_budget_includes_split_failures_and_subsequent_decide_calls():
    def handler(request):
        if len(request["questions"]) > 1:
            raise ClefServiceError("redacted", error_kind="input_too_long", status_code=413)
        return response(request)

    client = FakeClient(handler)
    result, trace, decider = run(client, [task(str(i)) for i in range(4)], max_calls=3)
    assert decider.calls == 3
    assert result["0"].status == "decided"
    assert result["1"].reason == "call_budget_exhausted"
    assert result["2"].reason == "call_budget_exhausted"
    more = decider.decide([task("new", state={"evidence": "new"})], deadline=time.monotonic()+1, trace=trace)
    assert more["new"].reason == "call_budget_exhausted"
    assert decider.calls == 3


def test_deadline_does_not_wait_for_uncooperative_client_or_allow_overlapping_http():
    release = threading.Event()
    started = threading.Event()

    class BlockingClient:
        def systemone(self, request):
            started.set()
            release.wait(2)
            return response(request)

    trace = []
    first = ClefDecider(BlockingClient())
    t0 = time.monotonic()
    try:
        result = first.decide([task()], deadline=t0 + 0.03, trace=trace)
        assert started.is_set()
        assert time.monotonic() - t0 < 0.3
        assert result["q1"].reason == "deadline_exhausted"
        assert first.calls == 1
        second = FakeClient()
        other, _, second_decider = run(second, seconds=0.03)
        assert other["q1"].status == "unavailable"
        assert second_decider.calls == 0
        assert second.requests == []
    finally:
        release.set()
        assert decisions._IN_FLIGHT.acquire(timeout=1)
        decisions._IN_FLIGHT.release()
    assert first.late_responses == 1


def test_remaining_deadline_restricts_actual_client_http_timeout():
    client = FakeClient()
    result, _, _ = run(client, timeout=10, seconds=0.5)
    assert result["q1"].status == "decided"
    assert 0 < client.timeouts[0] <= 0.5


def test_queue_wait_and_http_share_single_call_timeout():
    release = threading.Event()
    assert decisions._IN_FLIGHT.acquire(timeout=1)

    def hold_permit():
        release.wait(0.04)
        decisions._IN_FLIGHT.release()

    holder = threading.Thread(target=hold_permit, daemon=True)
    holder.start()
    try:
        client = FakeClient()
        result, _, _ = run(client, timeout=0.1, seconds=1)
        assert result["q1"].status == "decided"
        assert 0 < client.timeouts[0] <= 0.075
    finally:
        release.set()
        holder.join(timeout=1)


def test_expired_deadline_makes_no_calls_and_invalid_state_never_leaks():
    client = FakeClient()
    decider = ClefDecider(client)
    assert decider.decide([task()], deadline=time.monotonic()-1, trace=[])["q1"].reason == "deadline_exhausted"
    result = decider.decide([task(state={"private": object()})], deadline=time.monotonic()+1, trace=[])
    assert result["q1"].reason == "invalid_task"
    assert client.requests == []
