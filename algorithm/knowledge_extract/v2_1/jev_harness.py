"""Frozen seed harness for relation decisions and entity adjudication.

This module is the thing that gets edited to improve NanoJev on extraction.
It does not train the model.  Code computes a short evidence window and a few
facts; NanoJev only chooses among the criteria below.  A decision skips the
relation LLM only when the winning label is at least as confident as the
configured threshold.  Everything else stays on the existing LLM path.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .config import get_jev_config
from .jev_client import JevClient, Judgment, get_jev_client


RELATION_QUESTION_ID = "label"
ENTITY_QUESTION_ID = "verdict"
MAX_WINDOW_CHARS = 180
_SENTENCE_BOUNDARY = re.compile(r"[。！？!?\n]")

RELATION_INSTRUCTIONS = (
    "仅根据 State 中的 window 判断 relation 是否成立。"
    "same_sentence 和 char_distance 是已经算好的事实。"
    "不要使用 window 之外的信息，不要发明端点或新关系。"
    "只选择列出的候选。"
)
NONE_CRITERION = "window 只是共现、列举、否定、标题并列，或证据不足以支持该关系。"
RELATION_CRITERION = "window 中的原文支持 source 与 target 之间存在这条本体关系。"

ENTITY_INSTRUCTIONS = (
    "判断 left 与 right 是否是给定 type 下的同一实体。"
    "名称包含、括号缩写或别名可以是同一实体；只共享领域词或上下位词则不是。"
    "证据不足时选择 uncertain，不要猜测合并。"
)
ENTITY_CRITERIA = {
    "same_entity": "两个提及指向同一个真实对象，只是写法、缩写或别名不同。",
    "different_entity": "两个提及是不同对象，或只是相关、包含、上下位。",
    "uncertain": "现有名称和证据窗不足以判断，应保持为两个实体。",
}


def relation_facts(chunk_text: str, pair: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic features placed into the NanoJev state.  No model call."""
    text = chunk_text or ""
    source = str(pair.get("source_name") or "").strip()
    target = str(pair.get("target_name") or "").strip()
    relation_id = str(pair.get("relation_id") or "").strip()
    source_span = _find_span(text, source)
    target_span = _find_span(text, target, occupied=source_span)
    if source_span is not None and target_span is None:
        target_span = _find_span(text, target)
    complete = bool(source and target and relation_id and relation_id != "NONE" and source_span and target_span)
    window = ""
    clipped = False
    same_sentence = False
    distance: int | None = None
    if source_span and target_span:
        window, clipped = _window(text, source_span, target_span)
        same_sentence = _same_sentence(text, source_span, target_span)
        distance = _char_distance(source_span, target_span)
        if source not in window or target not in window:
            complete = False
    return {
        "window": window,
        "clipped": clipped,
        "source_found": source_span is not None,
        "target_found": target_span is not None,
        "same_sentence": same_sentence,
        "char_distance": distance,
        "complete": complete,
    }


def render_relation_state(pair: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    distance = facts.get("char_distance")
    return (
        f"window: {facts.get('window') or ''}\n"
        f"source: {pair.get('source_name')} ({pair.get('source_type')})\n"
        f"target: {pair.get('target_name')} ({pair.get('target_type')})\n"
        f"same_sentence: {'yes' if facts.get('same_sentence') else 'no'}\n"
        f"char_distance: {distance if distance is not None else 'unknown'}\n"
        f"relation: {pair.get('relation_id')}"
    )


def relation_question(relation_id: str) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": RELATION_INSTRUCTIONS,
        "criteria": {
            relation_id: RELATION_CRITERION,
            "NONE": NONE_CRITERION,
        },
    }


def relation_states(chunk_text: str, pairs: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(nanojev_states, gray_pairs)``.  Incomplete facts never reach the model."""
    states: list[dict[str, Any]] = []
    gray: list[dict[str, Any]] = []
    for pair in pairs:
        facts = relation_facts(chunk_text, pair)
        if not facts["complete"]:
            gray.append(_labeled(pair, decision="gray", reason="endpoints_not_in_chunk", facts=facts))
            continue
        relation_id = str(pair.get("relation_id") or "").strip()
        states.append(
            {
                "id": str(pair["pair_id"]),
                "state": render_relation_state(pair, facts),
                "questions": {RELATION_QUESTION_ID: relation_question(relation_id)},
                "_facts": facts,
            }
        )
    return states, gray


def route_relation(judgment: Judgment, relation_id: str, config: Mapping[str, Any] | None = None) -> str:
    """Skip the LLM only on a high-confidence winning label."""
    resolved = config if config is not None else get_jev_config()
    probabilities = judgment.probabilities
    if judgment.choice == relation_id and probabilities.get(relation_id, 0.0) >= float(resolved["accept"]):
        return "accept"
    if judgment.choice == "NONE" and probabilities.get("NONE", 0.0) >= float(resolved["reject"]):
        return "reject"
    return "gray"


def partition_relation_pairs(
    pairs: Iterable[Mapping[str, Any]],
    *,
    chunk_text: str,
    client: JevClient | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ontology pairs into accepted / rejected / gray.

    ``client is None`` and Jev disabled means every pair stays gray, so the
    relation LLM path is unchanged.
    """
    rows = [dict(pair) for pair in pairs]
    resolved_client = client if client is not None else get_jev_client()
    if resolved_client is None:
        return [], [], rows
    states, gray = relation_states(chunk_text, rows)
    if not states:
        return [], [], gray
    facts_by_id = {state["id"]: state.pop("_facts") for state in states}
    answers = resolved_client.predict(states)
    config = get_jev_config()
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    by_id = {str(pair["pair_id"]): pair for pair in rows}
    for state in states:
        pair = by_id[state["id"]]
        relation_id = str(pair.get("relation_id") or "")
        judgment = (answers.get(state["id"]) or {}).get(RELATION_QUESTION_ID)
        facts = facts_by_id[state["id"]]
        if judgment is None:
            gray.append(_labeled(pair, decision="gray", reason="missing_answer", facts=facts))
            continue
        decision = route_relation(judgment, relation_id, config)
        labeled = _labeled(pair, decision=decision, judgment=judgment, facts=facts)
        if decision == "accept":
            accepted.append(labeled)
        elif decision == "reject":
            rejected.append(labeled)
        else:
            gray.append(labeled)
    return accepted, rejected, gray


def render_entity_state(payload: Mapping[str, Any]) -> str:
    left = payload.get("left") if isinstance(payload.get("left"), Mapping) else {}
    right = payload.get("right") if isinstance(payload.get("right"), Mapping) else {}
    return (
        f"type: {payload.get('type')}\n"
        f"left_name: {left.get('name')}\n"
        f"right_name: {right.get('name')}\n"
        f"left_quote: {_first_quote(left)}\n"
        f"right_quote: {_first_quote(right)}"
    )


def default_entity_adjudicator(payload: dict[str, Any]) -> str:
    """Choice over same_entity / different_entity / uncertain."""
    client = get_jev_client()
    if client is None:
        return "uncertain"
    options = [str(item) for item in (payload.get("allowed_verdicts") or [])]
    if not options:
        options = ["same_entity", "different_entity", "uncertain"]
    criteria = {key: ENTITY_CRITERIA[key] for key in options if key in ENTITY_CRITERIA}
    if len(criteria) < 2:
        return "uncertain"
    answers = client.predict(
        [
            {
                "id": "entity",
                "state": render_entity_state(payload),
                "questions": {
                    ENTITY_QUESTION_ID: {
                        "type": "choice",
                        "instructions": ENTITY_INSTRUCTIONS,
                        "criteria": criteria,
                    }
                },
            }
        ]
    )
    judgment = (answers.get("entity") or {}).get(ENTITY_QUESTION_ID)
    if judgment is None or judgment.choice not in criteria:
        return "uncertain"
    config = get_jev_config()
    if judgment.choice == "uncertain" or judgment.probabilities.get(judgment.choice, 0.0) < config["choice_confidence"]:
        return "uncertain"
    return judgment.choice


def _labeled(
    pair: Mapping[str, Any],
    *,
    decision: str,
    facts: Mapping[str, Any],
    judgment: Judgment | None = None,
    reason: str = "",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source": "nanojev",
        "decision": decision,
        "relation_id": pair.get("relation_id"),
        "window": facts.get("window") or "",
        "same_sentence": bool(facts.get("same_sentence")),
        "char_distance": facts.get("char_distance"),
    }
    if reason:
        record["reason"] = reason
    if judgment is not None:
        record.update(judgment.to_dict())
    return {**pair, "judgment": record}


def _find_span(text: str, name: str, occupied: tuple[int, int] | None = None) -> tuple[int, int] | None:
    if not name:
        return None
    start = 0
    while True:
        index = text.find(name, start)
        if index < 0:
            return None
        end = index + len(name)
        if occupied is None or end <= occupied[0] or index >= occupied[1]:
            return index, end
        start = index + 1


def _window(text: str, left: tuple[int, int], right: tuple[int, int]) -> tuple[str, bool]:
    start = min(left[0], right[0])
    end = max(left[1], right[1])
    if end - start <= MAX_WINDOW_CHARS:
        return text[start:end], False
    midpoint = (start + end) // 2
    clip_start = max(0, midpoint - MAX_WINDOW_CHARS // 2)
    clip_end = min(len(text), clip_start + MAX_WINDOW_CHARS)
    clip_start = max(0, clip_end - MAX_WINDOW_CHARS)
    return text[clip_start:clip_end], True


def _same_sentence(text: str, left: tuple[int, int], right: tuple[int, int]) -> bool:
    earlier, later = (left, right) if left[0] <= right[0] else (right, left)
    return _SENTENCE_BOUNDARY.search(text[earlier[1] : later[0]]) is None


def _char_distance(left: tuple[int, int], right: tuple[int, int]) -> int:
    earlier, later = (left, right) if left[0] <= right[0] else (right, left)
    return max(0, later[0] - earlier[1])


def _first_quote(candidate: Mapping[str, Any]) -> str:
    evidence = candidate.get("evidence")
    if isinstance(evidence, list) and evidence:
        first = evidence[0]
        if isinstance(first, Mapping):
            return str(first.get("quote") or "")[:80]
    return ""


__all__ = [
    "ENTITY_INSTRUCTIONS",
    "RELATION_INSTRUCTIONS",
    "default_entity_adjudicator",
    "partition_relation_pairs",
    "relation_facts",
    "relation_question",
    "render_relation_state",
    "route_relation",
]
