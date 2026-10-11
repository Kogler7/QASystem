"""Bounded, transport-only Clef decisions; semantic admission belongs to the harness.

An unavailable service is never a negative semantic answer. Only complete, valid
choice distributions become decisions. We neither truncate evidence nor infer
token length from characters; the service validates the complete encoded input.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import random
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Literal

from external_services.clef import ClefFlashClient, ClefServiceError


# A timed-out caller cannot cancel an already-running synchronous HTTP request.
# Its daemon retains this process-wide permit until that request really returns.
_IN_FLIGHT = threading.Lock()
_PROJECTION_MARKER = "__clef_task_projection__"


@dataclass(frozen=True)
class DecisionTask:
    task_id: str
    kind: str
    state: dict[str, Any]
    instructions: str
    criteria: dict[str, str]
    source_chunk_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionResult:
    task_id: str
    status: Literal["decided", "unavailable"] = "decided"
    selected: str | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    margin: float = 0.0
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _json(value: Any, *, sort_keys: bool = True) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=sort_keys,
                      separators=(",", ":"))


class ClefDecider:
    def __init__(
        self, client: Any = None, timeout: float = 10, max_calls: int = 128,
        max_questions: int = 16,
    ) -> None:
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Clef timeout must be positive and finite")
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 0:
            raise ValueError("Clef max_calls must be a nonnegative integer")
        # 上限跟服务端 CLEF_MAX_QUESTIONS（默认 16）一致：每次调用多带几个问题
        # 不改变语义，但把串行调用数与墙钟时间按倍数压下来。
        if (isinstance(max_questions, bool) or not isinstance(max_questions, int)
                or not 1 <= max_questions <= 16):
            raise ValueError("Clef max_questions must be between 1 and 16")
        self.client = client if client is not None else ClefFlashClient(timeout=timeout)
        self.timeout = float(timeout)
        self.max_calls = max_calls
        self.max_questions = max_questions
        self.calls = 0
        self.late_responses = 0
        self._serial = threading.Lock()
        self._cache: dict[str, DecisionResult] = {}
        try:
            parameters = inspect.signature(self.client.systemone).parameters.values()
            self._timeout_keyword = any(
                parameter.name == "timeout" or parameter.kind == parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            self._timeout_keyword = False

    def decide(
        self, tasks: list[DecisionTask], *, deadline: float, trace: list[dict[str, Any]],
    ) -> dict[str, DecisionResult]:
        """Resolve tasks within an absolute ``time.monotonic()`` deadline.

        Calls, split requests and retries share this object's lifetime budget.
        The object is intended for one document and retains only a local cache.
        Injected clients may implement ``systemone(request, *, timeout=...)``;
        a simple ``systemone(request)`` is also supported with a wall-clock wait.
        """
        tasks = list(tasks)
        if not math.isfinite(deadline):
            raise ValueError("Clef deadline must be finite")
        if len({task.task_id for task in tasks}) != len(tasks):
            raise ValueError("Clef decision task IDs must be unique")
        if not tasks:
            return {}
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._serial.acquire(timeout=max(0, remaining)):
            return self._unavailable(tasks, "deadline_exhausted")
        try:
            return self._decide(tasks, deadline, trace)
        finally:
            self._serial.release()

    def _decide(self, tasks, deadline, trace):
        results: dict[str, DecisionResult] = {}
        groups: OrderedDict[str, list[DecisionTask]] = OrderedDict()
        for task in tasks:
            try:
                if (not isinstance(task.task_id, str) or not task.task_id
                        or not isinstance(task.kind, str)
                        or not isinstance(task.state, dict)
                        or not isinstance(task.instructions, str)
                        or not isinstance(task.criteria, dict)
                        or not 1 <= len(task.criteria) <= 32
                        or any(not isinstance(k, str) or not k or not isinstance(v, str)
                               for k, v in task.criteria.items())
                        or any(not isinstance(chunk_id, str) for chunk_id in task.source_chunk_ids)):
                    raise ValueError("Invalid task")
                state_key = _json(task.state)
            except (TypeError, ValueError, OverflowError):
                results.update(self._unavailable([task], "invalid_task"))
                continue
            try:
                cache_key = self._cache_key(task)
            except (TypeError, ValueError, OverflowError):
                self._trace(trace, [task], "invalid_state_projection", 0.0)
                results.update(self._unavailable([task], "invalid_state_projection"))
                continue
            cached = self._cache.get(cache_key)
            if cached is not None:
                results[task.task_id] = DecisionResult(
                    task_id=task.task_id, selected=cached.selected,
                    probabilities=dict(cached.probabilities), margin=cached.margin,
                    reason="valid_choice_distribution",
                    details={"cache_hit": True, "call_indices": []},
                )
                self._trace(trace, [task], "cache_hit", 0.0)
            else:
                # Freeze the submitted state even if a caller later changes a candidate.
                groups.setdefault(state_key, []).append(copy.deepcopy(task))
        for group in groups.values():
            for start in range(0, len(group), self.max_questions):
                batch = group[start:start + self.max_questions]
                resolved = self._batch(batch, deadline, trace)
                results.update(resolved)
                for task in batch:
                    if resolved[task.task_id].status == "decided":
                        self._cache[self._cache_key(task)] = resolved[task.task_id]
        return results

    def _cache_key(self, task):
        # A shared manifest can bind two task IDs to different claims/evidence.
        # Cache the actual per-task projection, not the shared batch state alone.
        # All public shared context, option order and question semantics survive.
        state = self._project_state(task.state, [task])
        material = [getattr(self.client, "model", None), task.kind, state,
                    task.instructions, list(task.criteria.items()), task.source_chunk_ids]
        return hashlib.sha256(_json(material).encode("utf-8")).hexdigest()

    @staticmethod
    def _unavailable(tasks, reason, **details):
        return {task.task_id: DecisionResult(
            task_id=task.task_id, status="unavailable", reason=reason,
            details=dict(details),
        ) for task in tasks}

    @staticmethod
    def _trace(trace, tasks, kind, elapsed_seconds, **details):
        trace.append({
            "provider": "clef", "type": kind,
            "task_ids": [task.task_id for task in tasks],
            "source_chunk_ids": list(dict.fromkeys(
                chunk_id for task in tasks for chunk_id in task.source_chunk_ids
            )),
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 6), **details,
        })

    def _batch(self, tasks, deadline, trace, *, depth=0, attempt=0, prior_calls=()):
        if deadline <= time.monotonic():
            return self._unavailable(tasks, "deadline_exhausted", call_indices=list(prior_calls))
        if self.calls >= self.max_calls:
            return self._unavailable(tasks, "call_budget_exhausted", call_indices=list(prior_calls))
        try:
            state = self._project_state(tasks[0].state, tasks)
        except (TypeError, ValueError):
            self._trace(trace, tasks, "invalid_state_projection", 0.0)
            return self._unavailable(tasks, "invalid_state_projection", call_indices=list(prior_calls))
        payload = {
            "state": state,
            "questions": {task.task_id: {
                "type": "choice", "instructions": task.instructions,
                "criteria": task.criteria,
            } for task in tasks},
        }
        body, failure, call_index, elapsed = self._call(payload, tasks, deadline, trace)
        call_indices = prior_calls + ((call_index,) if call_index is not None else ())
        if failure is None:
            try:
                results = self._validate_answers(
                    body, tasks, call_indices, getattr(self.client, "model", None),
                )
            except (TypeError, ValueError, OverflowError):
                self._trace(trace, tasks, "invalid_response", elapsed, call_index=call_index)
                return self._unavailable(tasks, "invalid_response", call_indices=list(call_indices))
            self._trace(trace, tasks, "success", elapsed, call_index=call_index)
            return results
        kind = failure["error_kind"]
        if kind == "input_too_long" and len(tasks) > 1 and depth < 2:
            middle = len(tasks) // 2
            results = self._batch(tasks[:middle], deadline, trace, depth=depth + 1,
                                  prior_calls=call_indices)
            results.update(self._batch(tasks[middle:], deadline, trace, depth=depth + 1,
                                       prior_calls=call_indices))
            return results
        if failure.get("retryable") and attempt == 0:
            delay = max(0.0, failure.get("retry_after") or 0.0) + random.uniform(0.025, 0.075)
            if self.calls >= self.max_calls:
                return self._unavailable(tasks, "call_budget_exhausted", **failure,
                                         call_indices=list(call_indices))
            if delay >= deadline - time.monotonic():
                return self._unavailable(tasks, "deadline_exhausted", **failure,
                                         call_indices=list(call_indices))
            self._trace(trace, tasks, "retry_wait", 0.0, delay_seconds=delay,
                        call_index=call_index)
            time.sleep(delay)
            return self._batch(tasks, deadline, trace, depth=depth, attempt=1,
                               prior_calls=call_indices)
        return self._unavailable(tasks, kind, **failure, call_indices=list(call_indices))

    @staticmethod
    def _project_state(state, tasks):
        """Project explicitly declared task dependencies, never arbitrary state.

        The harness opts in with a versioned local manifest. Claims and evidence
        values remain complete; shared ontology and every other public field are
        retained. Splitting questions can therefore remove unrelated task state
        without truncating source text or guessing which evidence is relevant.
        """
        if _PROJECTION_MARKER not in state:
            return state
        marker = state[_PROJECTION_MARKER]
        if (not isinstance(marker, dict) or type(marker.get("version")) is not int
                or marker["version"] != 1 or not isinstance(marker.get("tasks"), dict)
                or not isinstance(state.get("claims"), dict)
                or not isinstance(state.get("evidence_sets"), dict)):
            raise ValueError("Invalid state projection manifest")
        selected = {"claims": set(), "evidence_sets": set()}
        for task in tasks:
            dependencies = marker["tasks"].get(task.task_id)
            if not isinstance(dependencies, dict):
                raise ValueError("Missing task dependency declaration")
            for field, dependency_field in (("claims", "claim_ids"), ("evidence_sets", "evidence_set_ids")):
                identifiers = dependencies.get(dependency_field)
                if (not isinstance(identifiers, list) or not identifiers
                        or any(not isinstance(identifier, str) or identifier not in state[field]
                               for identifier in identifiers)):
                    raise ValueError("Invalid task dependencies")
                selected[field].update(identifiers)
        projected = {key: copy.deepcopy(value) for key, value in state.items()
                     if key not in {_PROJECTION_MARKER, "claims", "evidence_sets"}}
        for field, identifiers in selected.items():
            projected[field] = {key: copy.deepcopy(value) for key, value in state[field].items()
                                if key in identifiers}
        return projected

    def _call(self, payload, tasks, deadline, trace):
        started = time.monotonic()
        call_deadline = min(deadline, started + self.timeout)
        wait_budget = max(0.0, call_deadline - started)
        if wait_budget <= 0:
            return None, {"error_kind": "deadline_exhausted"}, None, 0.0
        if not _IN_FLIGHT.acquire(timeout=wait_budget):
            kind = "deadline_exhausted" if time.monotonic() >= deadline else "in_flight_busy"
            self._trace(trace, tasks, kind, time.monotonic() - started)
            return None, {"error_kind": kind}, None, time.monotonic() - started
        timeout = call_deadline - time.monotonic()
        if timeout <= 0:
            _IN_FLIGHT.release()
            kind = "deadline_exhausted" if time.monotonic() >= deadline else "in_flight_busy"
            return None, {"error_kind": kind}, None, time.monotonic() - started
        self.calls += 1
        call_index = self.calls
        self._trace(trace, tasks, "request", 0.0, call_index=call_index,
                    timeout_seconds=timeout)
        done = threading.Event()
        box: dict[str, Any] = {}
        observation = threading.Lock()

        def invoke():
            try:
                kwargs = {"timeout": timeout} if self._timeout_keyword else {}
                box["body"] = self.client.systemone(payload, **kwargs)
            except ClefServiceError as exc:
                # Exception text, response bodies, URLs and headers are never persisted.
                box["failure"] = {
                    "error_kind": exc.error_kind, "status_code": exc.status_code,
                    "retryable": exc.retryable, "retry_after": exc.retry_after,
                }
            except Exception:
                box["failure"] = {"error_kind": "client_error", "retryable": False}
            finally:
                _IN_FLIGHT.release()
                with observation:
                    if box.get("abandoned"):
                        self.late_responses += 1
                        self._trace(trace, tasks, "late_response", time.monotonic() - started,
                                    call_index=call_index)
                    box["finished"] = True
                    done.set()

        threading.Thread(target=invoke, name="clef-decision-http", daemon=True).start()
        completed = done.wait(timeout)
        elapsed = time.monotonic() - started
        if not completed or time.monotonic() >= deadline:
            with observation:
                box["abandoned"] = True
                if box.get("finished"):
                    self.late_responses += 1
                    self._trace(trace, tasks, "late_response", elapsed, call_index=call_index)
            kind = "deadline_exhausted" if time.monotonic() >= deadline else "timeout"
            failure = {"error_kind": kind, "retryable": kind == "timeout",
                       "in_flight": not done.is_set()}
            self._trace(trace, tasks, kind, elapsed, call_index=call_index)
            return None, failure, call_index, elapsed
        failure = box.get("failure")
        self._trace(trace, tasks, failure["error_kind"] if failure else "response", elapsed,
                    call_index=call_index, **(failure or {}))
        return box.get("body"), failure, call_index, elapsed

    @staticmethod
    def _validate_answers(body, tasks, call_indices, expected_model=None):
        if not isinstance(body, dict) or "error" in body or body.get("code", 0) != 0:
            raise ValueError("Invalid response")
        if expected_model is not None and body.get("model") != expected_model:
            raise ValueError("Unexpected model")
        answers = body.get("answers")
        if not isinstance(answers, dict) or set(answers) != {task.task_id for task in tasks}:
            raise ValueError("Incomplete answers")
        results = {}
        for task in tasks:
            answer = answers[task.task_id]
            if not isinstance(answer, dict) or answer.get("type") != "choice":
                raise ValueError("Invalid answer type")
            probabilities = answer.get("probabilities")
            if (not isinstance(probabilities, dict)
                    or probabilities.keys() != task.criteria.keys()):
                raise ValueError("Incomplete probabilities")
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not 0 <= value <= 1 or not math.isfinite(value)
                   for value in probabilities.values()):
                raise ValueError("Invalid probabilities")
            # Each service value is rounded to four decimal places (half-unit bound).
            if abs(math.fsum(probabilities.values()) - 1.0) > len(probabilities) * 0.00005 + 1e-9:
                raise ValueError("Invalid probability sum")
            selected = answer.get("choice")
            if (not isinstance(selected, str) or selected not in probabilities
                    or probabilities[selected] != max(probabilities.values())):
                raise ValueError("Choice is not a maximum-probability option")
            ranked = sorted(probabilities.values(), reverse=True)
            results[task.task_id] = DecisionResult(
                task_id=task.task_id, selected=selected,
                probabilities={key: float(value) for key, value in probabilities.items()},
                margin=float(ranked[0] - ranked[1] if len(ranked) > 1 else ranked[0]),
                reason="valid_choice_distribution",
                details={"cache_hit": False, "call_indices": list(call_indices)},
            )
        return results
