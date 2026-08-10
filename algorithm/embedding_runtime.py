from __future__ import annotations

import os
from functools import lru_cache
from threading import RLock

from sentence_transformers import SentenceTransformer


def _binding_device() -> str | None:
    """Return 'cuda' only when this process is explicitly bound to specific GPUs.

    Unbound processes (CPU workers / API process) see every GPU and would
    otherwise load the encoder on physical GPU0, which is reserved for the LLM
    service. Binding keeps the encoder on the worker's assigned GPU or on CPU.
    """
    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    if visible and visible.lower() not in {"", "none", "no", "-1", "all"}:
        return "cuda"
    return None


@lru_cache(maxsize=4)
def get_sentence_transformer(model_path: str, *, local_files_only: bool = True) -> SentenceTransformer:
    """Share one encoder instance per model path inside an auxiliary GPU worker."""
    return SentenceTransformer(
        model_path,
        local_files_only=local_files_only,
        device=_binding_device() or "cpu",
    )


@lru_cache(maxsize=4)
def get_encode_lock(model_path: str) -> RLock:
    return RLock()


def encode(model_path: str, values, *, local_files_only: bool = True):
    model = get_sentence_transformer(model_path, local_files_only=local_files_only)
    with get_encode_lock(model_path):
        return model.encode(values, convert_to_numpy=True)
