from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


Confidence = Literal["high", "medium", "low"]
QuestionType = Literal["fact", "relation", "metric", "trend", "procedure", "synthesis", "unknown"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AskRequest:
    question: str
    session_id: str | None = None
    detailed: bool = True


@dataclass
class ProblemFrame:
    question: str
    normalized_question: str
    question_type: QuestionType = "unknown"
    knowledge_space: str | None = None
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass
class LinkedEntity:
    id: str
    name: str
    score: float = 0.0
    labels: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class GraphPath:
    path_id: str
    nodes: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    score: float = 0.0


@dataclass
class EvidenceItem:
    id: str
    source_type: Literal["graph_entity", "graph_path", "text_chunk", "sql_result", "rule", "tool"]
    content: str
    score: float = 0.0
    entity_ids: list[str] = field(default_factory=list)
    path: GraphPath | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvidencePack:
    items: list[EvidenceItem] = field(default_factory=list)

    def top(self, limit: int) -> list[EvidenceItem]:
        return sorted(self.items, key=lambda item: item.score, reverse=True)[:limit]


@dataclass
class ToolTraceItem:
    tool_name: str
    input: dict[str, Any] = field(default_factory=dict)
    output_summary: str = ""
    ok: bool = True
    elapsed_ms: int | None = None


@dataclass
class GraphAnswerState:
    session_id: str
    problem_frame: ProblemFrame
    started_at: datetime = field(default_factory=_utc_now)
    linked_entities: list[LinkedEntity] = field(default_factory=list)
    graph_paths: list[GraphPath] = field(default_factory=list)
    evidence_pack: EvidencePack = field(default_factory=EvidencePack)
    tool_trace: list[ToolTraceItem] = field(default_factory=list)
    missing_info: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


@dataclass
class FinalGraphAnswer:
    answer: str
    confidence: Confidence
    problem_frame: ProblemFrame
    evidence_pack: EvidencePack
    tool_trace: list[ToolTraceItem] = field(default_factory=list)
    missing_info: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)
