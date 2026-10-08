"""KG_JEV_* configuration. This module does not read KG_EXTRACT_JEV_*."""
from __future__ import annotations

import os


def _flag(name: str) -> bool:
    return os.getenv(name, "0").strip() != "0"


def get_jev_config() -> dict:
    raw = os.getenv("KG_JEV_CHOICE_CONFIDENCE", "0.9").strip()
    try:
        confidence = float(raw)
    except ValueError as exc:
        raise ValueError(f"KG_JEV_CHOICE_CONFIDENCE 需在 [0, 1]，收到 {raw!r}") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"KG_JEV_CHOICE_CONFIDENCE 需在 [0, 1]，收到 {confidence}")
    return {
        "enabled": _flag("KG_JEV_ENABLED"),
        "document_coref": _flag("KG_JEV_DOCUMENT_COREF"),
        "relation_support": _flag("KG_JEV_RELATION_SUPPORT"),
        "graph_align": _flag("KG_JEV_GRAPH_ALIGN"),
        "model_path": os.getenv("KG_JEV_MODEL_PATH", "").strip(),
        "choice_confidence": confidence,
    }
