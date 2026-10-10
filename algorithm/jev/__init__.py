"""Domain decision service.

Callers supply task payloads. This package attaches the frozen question and
returns choice probabilities. It does not import an extractor, write a graph,
or load the published game checkpoint.
"""
from __future__ import annotations

from .config import get_jev_config
from .contract import CONTRACT_VERSION, TASKS, render_state, training_record
from .routing import route
from .service import DecisionService, Judgment, Prediction, predict

__all__ = [
    "CONTRACT_VERSION",
    "TASKS",
    "DecisionService",
    "Judgment",
    "Prediction",
    "get_jev_config",
    "predict",
    "render_state",
    "route",
    "training_record",
]
