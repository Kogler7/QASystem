"""Map a decided choice onto an action name. Callers must not apply the action yet."""
from __future__ import annotations

from .config import get_jev_config
from .contract import DOCUMENT_COREF, GRAPH_ALIGN, RELATION_SUPPORT, TASKS
from .service import Judgment


def route(judgment: Judgment, task_id: str, *, confidence: float | None = None) -> str:
    if task_id not in TASKS:
        raise ValueError(f"未知 JEV 任务: {task_id}")
    threshold = get_jev_config()["choice_confidence"] if confidence is None else confidence
    labels = TASKS[task_id]["labels"]
    if judgment.status != "decided" or judgment.choice not in labels or judgment.confidence < threshold:
        return "keep"
    if task_id == DOCUMENT_COREF and judgment.choice == "same_referent":
        return "merge"
    if task_id == RELATION_SUPPORT and judgment.choice == "unsupported":
        return "drop"
    if task_id == GRAPH_ALIGN and judgment.choice == "same_node":
        return "link"
    return "keep"
