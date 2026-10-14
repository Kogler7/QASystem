from __future__ import annotations

import os

import httpx

from algorithm.NL_to_cypher import vector
from algorithm.runtime_profile import is_remote_only
from backend.app.execution import supervisor
from external_services.models import RemoteEmbeddingClient


def test_remote_profile_disables_gpu_and_limits_cpu_workers(monkeypatch):
    monkeypatch.setenv("ALGORITHM_RUNTIME_PROFILE", "remote-only")
    assert is_remote_only() is True
    assert supervisor._gpu_ids() == []
    assert supervisor._auto_cpu_workers(32, 'auto') == 1
    assert supervisor._auto_cpu_workers(32, '3') == 3


def test_remote_embedding_preserves_input_order_and_dimension(monkeypatch):
    monkeypatch.setenv("EMBEDDING_SERVICE_URL", "https://models.example")
    monkeypatch.setenv("EMBEDDING_MODEL", "embed-v1")

    def fake_post(url, **kwargs):
        assert url == "https://models.example/v1/embeddings"
        assert kwargs["json"] == {"input": ["a", "b"], "model": "embed-v1"}
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [2, 3]},
                    {"index": 0, "embedding": [1, 2]},
                ]
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    client = RemoteEmbeddingClient()
    assert client.encode(["a", "b"]) == [[1.0, 2.0], [2.0, 3.0]]


def test_remote_vector_path_does_not_load_sentence_transformer(monkeypatch):
    monkeypatch.setenv("ALGORITHM_RUNTIME_PROFILE", "remote-only")
    monkeypatch.setenv("EMBEDDING_SERVICE_URL", "https://models.example")

    monkeypatch.setattr(
        vector,
        "encode",
        lambda _path, values, **_kwargs: __import__("numpy").array([[1.0, 2.0] for _ in values]),
    )
    assert vector.batch_text_to_vectors(["a", "b"]) == [[1.0, 2.0], [1.0, 2.0]]

