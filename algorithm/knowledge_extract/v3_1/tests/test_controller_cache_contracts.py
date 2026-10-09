"""Absolute call budgets and task-specific Clef cache semantics."""
from __future__ import annotations

import copy
from dataclasses import replace
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from algorithm.knowledge_extract.v3_1 import controller
from algorithm.knowledge_extract.v3_1.decisions import ClefDecider, DecisionTask


class Clock:
    def __init__(self):
        self.now = 100.0

    def advance(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now


class ImmediateThread:
    def __init__(self, *, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


def fake_runtime(monkeypatch, *, prompt_seconds=0.0):
    clock = Clock()

    def messages(_stage, _payload):
        clock.advance(prompt_seconds)
        return [{"role": "user", "content": "Source facts"}]

    monkeypatch.setattr(controller, "time", SimpleNamespace(
        monotonic=clock.monotonic, strftime=time.strftime))
    monkeypatch.setattr(controller, "threading", SimpleNamespace(
        Thread=ImmediateThread, Event=threading.Event, Lock=threading.Lock))
    monkeypatch.setattr(controller, "messages", messages)
    return clock


@pytest.mark.parametrize("document_seconds,request_seconds", [(.06, 1.0), (1.0, .06)])
def test_prompt_and_response_share_absolute_document_and_request_deadline(
    monkeypatch, document_seconds, request_seconds,
):
    clock = fake_runtime(monkeypatch, prompt_seconds=.04)

    def model(_messages):
        clock.advance(.04)
        return {"entities": []}

    control = controller.Controller(model, deadline=clock.now + document_seconds,
                                    timeout=request_seconds, max_calls=1)
    with pytest.raises(controller.CallFailure, match="LLM_TIMEOUT"):
        control.call("entities", "source", {})
    assert control.attempts[0]["type"] == "timeout"
    assert control.attempts[0]["request_sent"] is True
    assert control.late_responses == 1


def test_prompt_exhaustion_does_not_send_model_request(monkeypatch):
    clock = fake_runtime(monkeypatch, prompt_seconds=.07)
    requests = []
    control = controller.Controller(lambda messages: requests.append(messages),
                                    deadline=clock.now + .06, timeout=1, max_calls=1)
    with pytest.raises(controller.CallFailure, match="LLM_TIMEOUT"):
        control.call("entities", "source", {})
    assert requests == []
    assert control.attempts[0]["request_sent"] is False
    assert control.attempts[0]["type"] == "timeout"
    assert control.late_responses == 0


def test_queue_wait_is_only_unspent_wall_budget(monkeypatch):
    clock = fake_runtime(monkeypatch, prompt_seconds=.04)
    waits = []

    class Queue:
        def __init__(self, **_kwargs):
            pass

        def get(self, *, timeout):
            waits.append(timeout)
            clock.advance(timeout)
            raise queue.Empty

    class DeferredThread(ImmediateThread):
        def start(self):
            pass

    monkeypatch.setattr(controller, "queue", SimpleNamespace(Queue=Queue, Empty=queue.Empty))
    monkeypatch.setattr(controller.threading, "Thread", DeferredThread)
    control = controller.Controller(lambda _messages: {}, deadline=clock.now + .06,
                                    timeout=1, max_calls=1)
    with pytest.raises(controller.CallFailure, match="LLM_TIMEOUT"):
        control.call("entities", "source", {})
    assert waits == pytest.approx([.02])
    assert control.attempts[0]["elapsed_seconds"] == pytest.approx(.06)


def test_scheduler_wait_still_uses_same_absolute_deadline(monkeypatch):
    clock = fake_runtime(monkeypatch, prompt_seconds=.04)
    requests = []

    class Slot:
        def __enter__(self):
            clock.advance(.03)

        def __exit__(self, *_args):
            pass

    scheduler = SimpleNamespace(request_slot=Slot)
    control = controller.Controller(lambda messages: requests.append(messages),
                                    deadline=clock.now + .06, timeout=1, max_calls=1,
                                    scheduler=scheduler)
    with pytest.raises(controller.CallFailure, match="LLM_TIMEOUT"):
        control.call("entities", "source", {})
    assert requests == []
    assert control.attempts[0]["request_sent"] is False


def test_response_decoding_cannot_commit_after_deadline(monkeypatch):
    clock = fake_runtime(monkeypatch)

    class ExpensiveResponse(dict):
        def __deepcopy__(self, _memo):
            clock.advance(.07)
            return {"entities": []}

    control = controller.Controller(lambda _messages: ExpensiveResponse(),
                                    deadline=clock.now + .06, timeout=1, max_calls=1)
    with pytest.raises(controller.CallFailure, match="LLM_TIMEOUT"):
        control.call("entities", "source", {})
    assert control.attempts[0]["type"] == "timeout"
    assert control.late_responses == 1


def test_response_consumed_before_deadline_succeeds(monkeypatch):
    clock = fake_runtime(monkeypatch, prompt_seconds=.04)

    def model(_messages):
        clock.advance(.01)
        return {"entities": []}

    control = controller.Controller(model, deadline=clock.now + .06, timeout=1, max_calls=1)
    assert control.call("entities", "source", {}) == {"entities": []}
    assert control.attempts[0]["type"] == "success"
    assert control.attempts[0]["elapsed_seconds"] == pytest.approx(.05)
    assert control.late_responses == 0


CRITERIA = {"supported": "Source supports this fact", "contradicted": "Source contradicts this fact"}


def projected_state():
    return {
        "claims": {"a": {"fact": "A"}, "b": {"fact": "B"}},
        "evidence_sets": {"a": [{"quote": "A is true"}], "b": [{"quote": "B is false"}]},
        "ontology_context": {"meaning": "Check the source fact"},
        "__clef_task_projection__": {"version": 1, "tasks": {
            "first": {"claim_ids": ["a"], "evidence_set_ids": ["a"]},
            "second": {"claim_ids": ["b"], "evidence_set_ids": ["b"]},
            "same": {"claim_ids": ["a"], "evidence_set_ids": ["a"]},
        }},
    }


def task(identifier, state):
    return DecisionTask(identifier, "relation_support", state, "Check the projected claim",
                        dict(CRITERIA), ("source",))


class ProjectedClient:
    model = "clef-flash"

    def __init__(self):
        self.requests = []

    def systemone(self, request, *, timeout):
        self.requests.append(copy.deepcopy(request))
        selected = "supported" if set(request["state"]["claims"]) == {"a"} else "contradicted"
        return {"model": self.model, "answers": {
            identifier: {"type": "choice", "choice": selected,
                         "probabilities": {option: float(option == selected) for option in CRITERIA}}
            for identifier in request["questions"]}}


def decide(decider, item):
    return decider.decide([item], deadline=time.monotonic() + 1, trace=[])[item.task_id]


def test_same_full_state_with_different_task_dependencies_does_not_reuse_answer():
    client = ProjectedClient()
    decider = ClefDecider(client)
    state = projected_state()
    first = decide(decider, task("first", state))
    second = decide(decider, task("second", state))
    assert first.selected == "supported"
    assert second.selected == "contradicted"
    assert second.details["cache_hit"] is False
    assert len(client.requests) == 2


def test_semantically_identical_projection_reuses_answer_across_task_ids():
    client = ProjectedClient()
    decider = ClefDecider(client)
    state = projected_state()
    decide(decider, task("first", state))
    same = decide(decider, task("same", state))
    assert same.selected == "supported"
    assert same.details["cache_hit"] is True
    assert len(client.requests) == 1


@pytest.mark.parametrize("field", ["evidence", "shared_context", "instructions", "criteria", "sources", "kind", "model"])
def test_projected_cache_keeps_every_semantic_input(field):
    client = ProjectedClient()
    decider = ClefDecider(client)
    state = projected_state()
    decide(decider, task("first", state))
    changed_state = copy.deepcopy(state)
    changed = task("same", changed_state)
    if field == "evidence":
        changed_state["evidence_sets"]["a"] = [{"quote": "Changed source"}]
    elif field == "shared_context":
        changed_state["ontology_context"] = {"meaning": "A new definition"}
    elif field == "instructions":
        changed = replace(changed, instructions="Check a different question")
    elif field == "criteria":
        changed = replace(changed, criteria=dict(reversed(list(CRITERIA.items()))))
    elif field == "sources":
        changed = replace(changed, source_chunk_ids=("other_source",))
    elif field == "kind":
        changed = replace(changed, kind="entity_support")
    elif field == "model":
        client.model = "other-clef"
    result = decide(decider, changed)
    assert result.details["cache_hit"] is False
    assert len(client.requests) == 2


def test_missing_task_projection_cannot_hit_another_tasks_cache():
    client = ProjectedClient()
    decider = ClefDecider(client)
    state = projected_state()
    decide(decider, task("first", state))
    result = decide(decider, task("undeclared", state))
    assert result.status == "unavailable"
    assert result.reason == "invalid_state_projection"
    assert len(client.requests) == 1
