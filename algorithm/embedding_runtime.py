from __future__ import annotations

from functools import lru_cache
from threading import RLock

from sentence_transformers import SentenceTransformer


@lru_cache(maxsize=4)
def get_sentence_transformer(model_path: str, *, local_files_only: bool = True) -> SentenceTransformer:
    """Share one encoder instance per model path inside an auxiliary GPU worker."""
    return SentenceTransformer(model_path, local_files_only=local_files_only)


@lru_cache(maxsize=4)
def get_encode_lock(model_path: str) -> RLock:
    return RLock()


def encode(model_path: str, values, *, local_files_only: bool = True):
    model = get_sentence_transformer(model_path, local_files_only=local_files_only)
    with get_encode_lock(model_path):
        return model.encode(values, convert_to_numpy=True)
