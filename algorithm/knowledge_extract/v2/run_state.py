"""Internal extraction run state and typed failure semantics.

This state is intentionally richer than the public ``entities/relations``
contract.  It gives the orchestrator and evaluators one source of truth for
attempts, terminal status, stop reason, and strict versus best-effort runs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import time
from typing import Any, Iterable


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class ExtractionError(RuntimeError):
    error_code = "EXTRACTION_FAILED"

    def __init__(
        self,
        message: str,
        *,
        stage: str = "",
        details: dict[str, Any] | None = None,
        error_code: str = "",
    ):
        super().__init__(message)
        self.stage = stage
        self.details = dict(details or {})
        if error_code:
            self.error_code = error_code


class ExtractionRequestError(ExtractionError):
    error_code = "LLM_REQUEST_FAILED"


class ExtractionTimeoutError(ExtractionRequestError):
    error_code = "LLM_TIMEOUT"


class ExtractionResponseError(ExtractionError):
    error_code = "LLM_JSON_PARSE_FAILED"


class ExtractionValidationError(ExtractionError):
    error_code = "LLM_SCHEMA_INVALID"


def _coerce_usage(payload: Any) -> dict[str, Any] | None:
    if payload is None:
        return None
    if isinstance(payload, dict):
        return dict(payload)
    return {"value": str(payload)}


@dataclass(frozen=True)
class RunAttempt:
    attempt: int
    error_type: str
    pass_name: str = ""
    chunk_id: str = ""
    api: str = ""
    response_mode: str = ""
    response_status: str = ""
    finish_reason: str = ""
    incomplete_details: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] | None = None
    usage_unavailable: bool | None = None
    response_characters: int | None = None
    recovered_items: int | None = None
    next_response_mode: str = ""
    retryable: bool | None = None
    message: str = ""
    elapsed_seconds: float | None = None
    terminal: bool = True

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunAttempt":
        return cls(
            attempt=max(1, int(payload.get("attempt") or 1)),
            error_type=str(payload.get("type") or "unknown"),
            pass_name=str(payload.get("pass") or ""),
            chunk_id=str(payload.get("chunk") or ""),
            api=str(payload.get("api") or ""),
            response_mode=str(payload.get("response_mode") or ""),
            response_status=str(payload.get("response_status") or ""),
            finish_reason=str(payload.get("finish_reason") or ""),
            incomplete_details=dict(payload.get("incomplete_details") or {}),
            usage=_coerce_usage(payload.get("usage")),
            usage_unavailable=(
                bool(payload.get("usage_unavailable"))
                if payload.get("usage_unavailable") is not None
                else None
            ),
            response_characters=(
                int(payload["response_characters"])
                if payload.get("response_characters") is not None
                else None
            ),
            recovered_items=(
                int(payload["recovered_items"])
                if payload.get("recovered_items") is not None
                else None
            ),
            next_response_mode=str(payload.get("next_response_mode") or ""),
            retryable=(
                bool(payload.get("retryable"))
                if payload.get("retryable") is not None
                else None
            ),
            message=str(payload.get("message") or ""),
            elapsed_seconds=(
                float(payload["elapsed_seconds"])
                if payload.get("elapsed_seconds") is not None
                else None
            ),
            terminal=bool(payload.get("terminal", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value not in (None, "")
        }


_FAILURE_PRIORITY = {
    "incomplete": ("LLM_RESPONSE_INCOMPLETE", "LLM 响应不完整"),
    "empty": ("LLM_EMPTY_RESPONSE", "LLM 返回为空"),
    "timeout": ("LLM_TIMEOUT", "LLM 请求超时"),
    "request": ("LLM_REQUEST_FAILED", "LLM 调用失败"),
    "json": ("LLM_JSON_PARSE_FAILED", "LLM 返回内容不是有效 JSON"),
    "schema": ("LLM_SCHEMA_INVALID", "LLM 返回内容不满足结构约束"),
    "schema_validation": ("LLM_SCHEMA_INVALID", "LLM 返回内容不满足 ontology 约束"),
}


@dataclass
class ExtractionRun:
    run_id: str
    ontology_sha256: str
    markdown_sha256: str
    model: str = ""
    prompt_version: str = ""
    best_effort: bool = False
    status: RunStatus = RunStatus.PENDING
    attempts: list[RunAttempt] = field(default_factory=list)
    coverage_ledger: dict[str, dict[str, Any]] = field(default_factory=dict)
    accepted_entities: int = 0
    accepted_relations: int = 0
    stop_reason: str = ""
    error_code: str | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        ontology: str,
        markdown: str,
        model: str = "",
        prompt_version: str = "",
        best_effort: bool = False,
    ) -> "ExtractionRun":
        return cls(
            run_id=run_id,
            ontology_sha256=hashlib.sha256((ontology or "").encode("utf-8")).hexdigest(),
            markdown_sha256=hashlib.sha256((markdown or "").encode("utf-8")).hexdigest(),
            model=model,
            prompt_version=prompt_version,
            best_effort=best_effort,
        )

    def start(self) -> None:
        if self.status is not RunStatus.PENDING:
            raise RuntimeError(f"run {self.run_id} cannot start from {self.status.value}")
        self.status = RunStatus.RUNNING
        self.started_at = time.time()

    def absorb_attempts(self, attempts: Iterable[dict[str, Any]]) -> None:
        self.attempts = [RunAttempt.from_dict(attempt) for attempt in attempts]

    def register_block(self, chunk_id: str, *, block_id: str = "", kind: str = "") -> None:
        resolved_block_id = block_id or chunk_id
        entry = self.coverage_ledger.setdefault(
            resolved_block_id,
            {
                "block_id": resolved_block_id,
                "kind": kind or "chunk",
                "chunk_ids": [],
                "entity_scanned_chunks": [],
                "relation_scanned_chunks": [],
            },
        )
        if chunk_id not in entry["chunk_ids"]:
            entry["chunk_ids"].append(chunk_id)

    def mark_chunk_scan(self, chunk_id: str, pass_name: str, *, completed: bool) -> None:
        if not completed:
            return
        field_name = f"{pass_name}_scanned_chunks"
        for entry in self.coverage_ledger.values():
            if chunk_id in entry["chunk_ids"] and chunk_id not in entry[field_name]:
                entry[field_name].append(chunk_id)
                return

    def finish(
        self,
        *,
        accepted_entities: int,
        accepted_relations: int,
        stop_reason: str = "validation_complete",
        failure_code: str = "",
        failure_message: str = "",
    ) -> None:
        if self.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
            raise RuntimeError(f"run {self.run_id} is already terminal")
        if self.started_at is None:
            self.started_at = time.time()
        self.accepted_entities = max(0, int(accepted_entities))
        self.accepted_relations = max(0, int(accepted_relations))
        failure = self.primary_failure()
        if failure is None and failure_code:
            failure = (failure_code, failure_message or stop_reason)
        if failure is None:
            self.status = RunStatus.SUCCEEDED
            self.stop_reason = stop_reason
        else:
            self.error_code, self.error = failure
            has_facts = self.accepted_entities > 0 or self.accepted_relations > 0
            self.status = RunStatus.PARTIAL if self.best_effort and has_facts else RunStatus.FAILED
            self.stop_reason = "best_effort_partial" if self.status is RunStatus.PARTIAL else "required_pass_failed"
        self.finished_at = time.time()

    def primary_failure(self) -> tuple[str, str] | None:
        error_types = {attempt.error_type for attempt in self.attempts if attempt.terminal}
        for failure_type in (
            "incomplete",
            "empty",
            "timeout",
            "request",
            "json",
            "schema",
            "schema_validation",
        ):
            if failure_type in error_types:
                return _FAILURE_PRIORITY[failure_type]
        return None

    @property
    def elapsed_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def to_trace(self) -> dict[str, Any]:
        coverage_ledger = []
        for entry in self.coverage_ledger.values():
            chunk_ids = entry["chunk_ids"]
            coverage_ledger.append(
                {
                    "block_id": entry["block_id"],
                    "kind": entry["kind"],
                    "chunk_ids": list(chunk_ids),
                    "entity_scan_complete": set(entry["entity_scanned_chunks"]) == set(chunk_ids),
                    "relation_scan_complete": set(entry["relation_scanned_chunks"]) == set(chunk_ids),
                }
            )
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "best_effort": self.best_effort,
            "ontology_sha256": self.ontology_sha256,
            "markdown_sha256": self.markdown_sha256,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "coverage_ledger": coverage_ledger,
            "accepted_entities": self.accepted_entities,
            "accepted_relations": self.accepted_relations,
            "stop_reason": self.stop_reason,
            "error_code": self.error_code,
            "error": self.error,
            "elapsed_seconds": (
                round(self.elapsed_seconds, 6) if self.elapsed_seconds is not None else None
            ),
        }


__all__ = [
    "ExtractionError",
    "ExtractionRequestError",
    "ExtractionResponseError",
    "ExtractionRun",
    "ExtractionTimeoutError",
    "ExtractionValidationError",
    "RunAttempt",
    "RunStatus",
]
