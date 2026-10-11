"""Bounded calls whose late responses cannot mutate accepted extraction state."""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import json
import logging
import queue
import re
import threading
import time
from typing import Any

from .prompts import messages

logger = logging.getLogger(__name__)


class CallFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Controller:
    def __init__(self, llm: Any, *, deadline: float, timeout: float, max_calls: int, scheduler: Any = None):
        self.llm, self.deadline, self.timeout, self.max_calls, self.scheduler = llm, deadline, timeout, max_calls, scheduler
        self.attempts: list[dict[str, Any]] = []
        self.calls = 0
        self.late_responses = 0
        self._lock = threading.Lock()

    def call(self, stage: str, chunk_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        remaining = self.deadline - started
        if remaining <= 0:
            raise CallFailure("DOCUMENT_BUDGET_EXHAUSTED")
        if self.calls >= self.max_calls:
            raise CallFailure("LLM_CALL_BUDGET_EXHAUSTED")
        self.calls += 1
        call_deadline = min(self.deadline, started + self.timeout)
        attempt = {"provider": "llm", "pass": stage, "chunk": chunk_id, "call_index": self.calls,
                   "terminal": False, "type": "pending", "request_sent": False}
        self.attempts.append(attempt)
        result_queue: queue.Queue = queue.Queue(maxsize=1)
        abandoned = threading.Event()

        def timed_out(*, response_received: bool = False) -> None:
            abandoned.set()
            if response_received:
                with self._lock:
                    self.late_responses += 1
            attempt.update(type="timeout", elapsed_seconds=time.monotonic() - started,
                           late_response_ignored=True)
            raise CallFailure("LLM_TIMEOUT")

        call_messages = messages(stage, payload)
        # Prompt construction is part of the same absolute wall budget as
        # scheduler waiting, the model request, and response consumption.
        if time.monotonic() >= call_deadline:
            timed_out()

        def invoke() -> None:
            try:
                model_messages = deepcopy(call_messages)
                with self.scheduler.request_slot() if self.scheduler is not None else nullcontext():
                    # Waiting for the global scheduler is charged to this call's wall budget.
                    if abandoned.is_set() or time.monotonic() >= call_deadline:
                        return
                    attempt["request_sent"] = True
                    if callable(getattr(self.llm, "generate_structured", None)):
                        response = self.llm.generate_structured(
                            schema={"type": "object"}, messages=model_messages,
                            run_context={"pass_name": stage, "schema_name": "v31_" + stage,
                                         "chunk_id": chunk_id, "response_mode": "plain"},
                        )
                    else:
                        response = self.llm(model_messages)
                item = (True, response)
            except Exception as exc:
                item = (False, exc)
            if abandoned.is_set():
                with self._lock:
                    self.late_responses += 1
                return
            result_queue.put_nowait(item)

        logger.info("[%s] V3.1 stage=%s chunk=%s call=%s", time.strftime("%Y-%m-%d %H:%M:%S"), stage, chunk_id, self.calls)
        threading.Thread(target=invoke, daemon=True, name=f"kg-v33-{self.calls}").start()
        try:
            success, response = result_queue.get(timeout=max(0.0, call_deadline - time.monotonic()))
        except queue.Empty:
            timed_out()
        if time.monotonic() >= call_deadline:
            timed_out(response_received=True)
        attempt["elapsed_seconds"] = time.monotonic() - started
        if not success:
            is_timeout = isinstance(response, TimeoutError) or "timeout" in type(response).__name__.lower()
            attempt.update(type="timeout" if is_timeout else "request", exception_class=type(response).__name__)
            raise CallFailure("LLM_TIMEOUT" if is_timeout else "LLM_REQUEST_FAILED")
        if callable(getattr(response, "to_trace", None)):
            attempt["response"] = response.to_trace()
        if getattr(response, "status", "completed") not in {"completed", "succeeded", "success", ""} or getattr(response, "finish_reason", None) in {"length", "content_filter"}:
            attempt["type"] = "incomplete"
            raise CallFailure("LLM_RESPONSE_INCOMPLETE")
        try:
            if isinstance(response, dict):
                parsed = deepcopy(response)
            else:
                text = str(response).strip()
                fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
                parsed = json.loads(fence.group(1) if fence else text)
            if not isinstance(parsed, dict):
                raise ValueError("not object")
        except (ValueError, TypeError):
            attempt["type"] = "json"
            raise CallFailure("LLM_JSON_PARSE_FAILED") from None
        if time.monotonic() >= call_deadline:
            timed_out(response_received=True)
        attempt["type"] = "success"
        attempt["elapsed_seconds"] = time.monotonic() - started
        return parsed
