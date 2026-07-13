import os
from functools import lru_cache

from sentence_transformers import SentenceTransformer


DEFAULT_MODEL_PATH = "/models/bge-small-zh-v1.5"


@lru_cache(maxsize=2)
def _load_model(model_path: str) -> SentenceTransformer:
    return SentenceTransformer(model_path, local_files_only=True)


def _model_path(model_path: str | None = None) -> str:
    return model_path or os.getenv("MODEL_PATH") or DEFAULT_MODEL_PATH


def text_to_vector(text: str, model_path: str | None = None) -> list[float]:
    """Encode one text with the configured local SentenceTransformer model."""
    value = (text or "").strip()
    if not value:
        raise ValueError("文本不能为空")
    vector = _load_model(_model_path(model_path)).encode(value, convert_to_numpy=True)
    return vector.tolist()


def batch_text_to_vectors(texts: list[str], model_path: str | None = None) -> list[list[float]]:
    """Encode multiple texts with one batched model call."""
    values = [str(text) for text in texts]
    if not values:
        raise ValueError("文本列表不能为空")
    vectors = _load_model(_model_path(model_path)).encode(values, convert_to_numpy=True)
    return vectors.tolist()
