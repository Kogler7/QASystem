import os

from algorithm.embedding_runtime import encode, get_sentence_transformer
from algorithm.runtime_profile import is_remote_only


DEFAULT_MODEL_PATH = "/models/bge-small-zh-v1.5"


def _load_model(model_path: str):
    if is_remote_only():
        class RemoteEmbeddingModel:
            def encode(self, values, convert_to_numpy=True):
                import numpy as np

                items = [values] if isinstance(values, str) else list(values)
                vectors = batch_text_to_vectors(items, model_path=model_path)
                return np.asarray(vectors, dtype=float)

        return RemoteEmbeddingModel()
    return get_sentence_transformer(model_path, local_files_only=True)


def _model_path(model_path: str | None = None) -> str:
    return model_path or os.getenv("VECTOR_MODEL_PATH") or DEFAULT_MODEL_PATH


def text_to_vector(text: str, model_path: str | None = None) -> list[float]:
    """Encode one text using the configured local or remote backend."""
    value = (text or "").strip()
    if not value:
        raise ValueError("文本不能为空")
    vector = encode(_model_path(model_path), value, local_files_only=True)
    return vector.tolist()


def batch_text_to_vectors(texts: list[str], model_path: str | None = None) -> list[list[float]]:
    """Encode multiple texts with one batched local or remote model call."""
    values = [str(text) for text in texts]
    if not values:
        raise ValueError("文本列表不能为空")
    vectors = encode(_model_path(model_path), values, local_files_only=True)
    return vectors.tolist()
