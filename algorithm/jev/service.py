"""Decision service. This build does not load checkpoint weights."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from algorithm.runtime_profile import is_remote_only

from .config import get_jev_config
from .contract import TASKS, question, render_state


@dataclass(frozen=True)
class Judgment:
    state_id: str
    choice: str
    probabilities: dict[str, float]
    confidence: float
    status: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_id": self.state_id,
            "choice": self.choice,
            "probabilities": dict(self.probabilities),
            "confidence": self.confidence,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Prediction:
    status: str
    reason: str
    judgments: list[Judgment] = field(default_factory=list)


Predictor = Callable[[str, Sequence[Mapping[str, Any]]], Sequence[Judgment]]


class DecisionService:
    """Score states for one task.

    A directory in ``KG_JEV_MODEL_PATH`` is not loaded. Domain weights are not
    part of this build, and the published game checkpoint must not be used.
    Tests inject ``predictor`` to exercise decided judgments.
    """

    def __init__(self, predictor: Predictor | None = None) -> None:
        self._predictor = predictor

    def predict(self, task_id: str, payloads: Sequence[Mapping[str, Any]]) -> Prediction:
        if task_id not in TASKS:
            raise ValueError(f"未知 JEV 任务: {task_id}")
        config = get_jev_config()
        if not config["enabled"]:
            return Prediction(status="skipped", reason="disabled")
        if is_remote_only():
            return Prediction(status="skipped", reason="remote_only")
        model_path = config["model_path"]
        if not model_path or not Path(model_path).is_dir():
            return Prediction(status="skipped", reason="checkpoint_missing")
        if self._predictor is None:
            return Prediction(status="skipped", reason="domain_checkpoint_not_ready")
        requests = [
            {
                "id": str(payload["state_id"]),
                "state": render_state(task_id, payload),
                "questions": {TASKS[task_id]["question_id"]: question(task_id)},
            }
            for payload in payloads
        ]
        judgments = [item for item in self._predictor(task_id, requests) if item.status == "decided"]
        return Prediction(status="decided", reason="", judgments=list(judgments))


def predict(task_id: str, payloads: Sequence[Mapping[str, Any]]) -> Prediction:
    return DecisionService().predict(task_id, payloads)
