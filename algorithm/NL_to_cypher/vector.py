import os

from algorithm.embedding_runtime import encode, get_sentence_transformer


DEFAULT_MODEL_PATH = "/models/bge-small-zh-v1.5"


def _load_model(model_path: str):
    return get_sentence_transformer(model_path, local_files_only=True)


def _model_path(model_path: str | None = None) -> str:
    return model_path or os.getenv("VECTOR_MODEL_PATH") or DEFAULT_MODEL_PATH


def text_to_vector(text: str, model_path: str | None = None) -> list[float]:
    """Encode one text with the configured local SentenceTransformer model."""
    value = (text or "").strip()
    if not value:
        raise ValueError("文本不能为空")
    vector = encode(_model_path(model_path), value, local_files_only=True)
    return vector.tolist()


def batch_text_to_vectors(texts: list[str], model_path: str | None = None) -> list[list[float]]:
    """Encode multiple texts with one batched model call."""
    values = [str(text) for text in texts]
    if not values:
        raise ValueError("文本列表不能为空")
    vectors = encode(_model_path(model_path), values, local_files_only=True)
    return vectors.tolist()
