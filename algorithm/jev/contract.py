"""Frozen decision contract. Training records and inference requests share this text."""
from __future__ import annotations

import json
from typing import Any, Mapping

CONTRACT_VERSION = "jev-decision-v1"
SPLITS = ("train", "dev", "test")
QUOTE_LIMIT = 180

DOCUMENT_COREF = "document_coref"
RELATION_SUPPORT = "relation_support"
GRAPH_ALIGN = "graph_align"

_DOCUMENT_COREF_INSTRUCTIONS = (
    "判断两侧是否为给定 type 下的同一指称。"
    "名称包含、缩写或别名可以是同一指称。"
    "上下位、整体与部分、不同个体选 different。"
    "原文或属性不足以判断时选 uncertain。"
)
_RELATION_INSTRUCTIONS = (
    "只根据 window 判断这条本体关系是否成立。"
    "共现、列举、否定或标题并列而没有关系证据时选 unsupported。"
)
_GRAPH_ALIGN_INSTRUCTIONS = (
    "判断新节点与已有节点是否为同一指称。"
    "不同个体选 distinct。证据不足选 uncertain。"
)

TASKS: dict[str, dict[str, Any]] = {
    DOCUMENT_COREF: {
        "question_id": "verdict",
        "labels": ("same_referent", "different", "uncertain"),
        "instructions": _DOCUMENT_COREF_INSTRUCTIONS,
        "criteria": {
            "same_referent": "两侧是同一指称，只是写法、缩写或别名不同。",
            "different": "两侧是不同个体，或只是上下位、整体与部分。",
            "uncertain": "名称、属性和原文不足以判断。",
        },
    },
    RELATION_SUPPORT: {
        "question_id": "label",
        "labels": ("supported", "unsupported"),
        "instructions": _RELATION_INSTRUCTIONS,
        "criteria": {
            "supported": "window 中的原文支持 source 与 target 之间存在这条本体关系。",
            "unsupported": "window 只是共现、列举、否定或标题并列，或没有关系证据。",
        },
    },
    GRAPH_ALIGN: {
        "question_id": "verdict",
        "labels": ("same_node", "distinct", "uncertain"),
        "instructions": _GRAPH_ALIGN_INSTRUCTIONS,
        "criteria": {
            "same_node": "新节点与已有节点是同一指称。",
            "distinct": "新节点与已有节点是不同个体。",
            "uncertain": "现有名称和属性不足以判断。",
        },
    },
}


def question(task_id: str) -> dict[str, Any]:
    spec = _task(task_id)
    return {
        "type": "choice",
        "instructions": spec["instructions"],
        "criteria": dict(spec["criteria"]),
    }


def render_state(task_id: str, payload: Mapping[str, Any]) -> str:
    """Render one state. Field order is part of the training contract."""
    if task_id == DOCUMENT_COREF:
        return "\n".join(
            (
                "task: document_coref",
                f"type: {_text(payload.get('type'))}",
                f"left_name: {_text(payload.get('left_name'))}",
                f"right_name: {_text(payload.get('right_name'))}",
                f"left_properties: {_properties(payload.get('left_properties'))}",
                f"right_properties: {_properties(payload.get('right_properties'))}",
                f"left_quote: {_quote(payload.get('left_quote'))}",
                f"right_quote: {_quote(payload.get('right_quote'))}",
            )
        )
    if task_id == RELATION_SUPPORT:
        return "\n".join(
            (
                "task: relation_support",
                f"window: {_quote(payload.get('window'))}",
                f"source: {_text(payload.get('source_name'))} ({_text(payload.get('source_type'))})",
                f"target: {_text(payload.get('target_name'))} ({_text(payload.get('target_type'))})",
                f"relation: {_text(payload.get('relation'))}",
                f"relation_note: {_text(payload.get('relation_note'))}",
            )
        )
    if task_id == GRAPH_ALIGN:
        return "\n".join(
            (
                "task: graph_align",
                f"type: {_text(payload.get('type'))}",
                f"new_name: {_text(payload.get('new_name'))}",
                f"existing_name: {_text(payload.get('existing_name'))}",
                f"new_properties: {_properties(payload.get('new_properties'))}",
                f"existing_properties: {_properties(payload.get('existing_properties'))}",
                f"new_quote: {_quote(payload.get('new_quote'))}",
                f"existing_uid: {_text(payload.get('existing_uid'))}",
            )
        )
    raise ValueError(f"未知 JEV 任务: {task_id}")


def training_record(
    *,
    record_id: str,
    state_id: str,
    split: str,
    task_id: str,
    payload: Mapping[str, Any],
    gold_label: str,
    source_group_id: str,
) -> dict[str, Any]:
    """One JSONL object. ``gold_probs`` is one-hot over the task labels."""
    spec = _task(task_id)
    if split not in SPLITS:
        raise ValueError(f"split 仅支持 {' / '.join(SPLITS)}，收到 {split!r}")
    labels = spec["labels"]
    if gold_label not in labels:
        raise ValueError(f"{task_id} 的标签仅支持 {' / '.join(labels)}，收到 {gold_label!r}")
    question_id = spec["question_id"]
    state = render_state(task_id, payload)
    return {
        "id": record_id,
        "state_id": state_id,
        "split": split,
        "state": state,
        "questions": {question_id: question(task_id)},
        "gold_probs": {question_id: {label: 1.0 if label == gold_label else 0.0 for label in labels}},
        "gold_probs_kind": "observed_label",
        "metadata": {
            "task": task_id,
            "contract": CONTRACT_VERSION,
            "source_group_id": source_group_id,
        },
    }


def validate_record(record: Mapping[str, Any]) -> None:
    """Reject a JSONL object that drifts from ``training_record``."""
    task_id = str((record.get("metadata") or {}).get("task") or "")
    spec = _task(task_id)
    question_id = spec["question_id"]
    if record.get("split") not in SPLITS:
        raise ValueError("训练记录 split 无效")
    if (record.get("metadata") or {}).get("contract") != CONTRACT_VERSION:
        raise ValueError("训练记录契约版本不匹配")
    state_payload = record.get("state")
    if not isinstance(state_payload, str) or not state_payload.startswith(f"task: {task_id}"):
        raise ValueError("训练记录 state 与任务不一致")
    questions = record.get("questions") or {}
    expected = question(task_id)
    if questions.get(question_id) != expected:
        raise ValueError("训练记录题目与契约不一致")
    probs = (record.get("gold_probs") or {}).get(question_id) or {}
    if set(probs) != set(spec["labels"]):
        raise ValueError("训练记录 gold_probs 标签集合不匹配")
    if sum(float(value) for value in probs.values()) != 1.0:
        raise ValueError("训练记录 gold_probs 必须是 one-hot")


def _task(task_id: str) -> dict[str, Any]:
    try:
        return TASKS[task_id]
    except KeyError as exc:
        raise ValueError(f"未知 JEV 任务: {task_id}") from exc


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _quote(value: Any) -> str:
    return _text(value)[:QUOTE_LIMIT]


def _properties(value: Any) -> str:
    if not isinstance(value, Mapping):
        value = {}
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
