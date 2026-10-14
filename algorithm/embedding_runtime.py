from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv

import os
from functools import lru_cache
from threading import RLock

from algorithm.runtime_profile import is_remote_only


def _binding_device() -> str | None:
    """Return 'cuda' only when this process is explicitly bound to specific GPUs.

    Unbound processes (CPU workers / API process) see every GPU and would
    otherwise load the encoder on physical GPU0, which is reserved for the LLM
    service. Binding keeps the encoder on the worker's assigned GPU or on CPU.
    """
    visible = config_getenv("CUDA_VISIBLE_DEVICES", "").strip()
    if visible and visible.lower() not in {"", "none", "no", "-1", "all"}:
        return "cuda"
    return None


@lru_cache(maxsize=4)
def get_sentence_transformer(model_path: str, *, local_files_only: bool = True):
    """Share one encoder instance per model path inside an auxiliary GPU worker."""
    if is_remote_only():
        raise RuntimeError("remote-only 模式禁止加载本地 SentenceTransformer")
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(
        model_path,
        local_files_only=local_files_only,
        device=_binding_device() or "cpu",
    )


@lru_cache(maxsize=4)
def get_encode_lock(model_path: str) -> RLock:
    return RLock()


def encode(model_path: str, values, *, local_files_only: bool = True):
    if is_remote_only():
        from external_services.models import RemoteEmbeddingClient

        import numpy as np

        items = [values] if isinstance(values, str) else list(values)
        return np.asarray(
            RemoteEmbeddingClient().encode(items),
            dtype=float,
        )
    model = get_sentence_transformer(model_path, local_files_only=local_files_only)
    with get_encode_lock(model_path):
        return model.encode(values, convert_to_numpy=True)
