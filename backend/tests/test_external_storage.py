from __future__ import annotations

from types import SimpleNamespace

from algorithm.knowledge_pipeline import storage


def test_upload_file_returns_normalized_metadata(monkeypatch):
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {
            "code": 0,
            "data": {
                "id": "file-1",
                "file_name": "demo.md",
                "file_path": "/srv/files/demo.md",
                "file_url": "https://files.example/demo.md",
            },
        },
    )
    monkeypatch.setattr(storage.requests, "post", lambda *_args, **_kwargs: response)

    result = storage.upload_file(
        b"content",
        "demo.md",
        "text/markdown",
        "https://files.example",
        "space-1",
    )

    assert result == {
        "success": True,
        "id": "file-1",
        "name": "demo.md",
        "path": "/srv/files/demo.md",
        "url": "https://files.example/demo.md",
    }


def test_store_vectors_posts_expected_payload_and_returns_ids(monkeypatch):
    captured = {}
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {
            "code": 0,
            "msg": "success",
            "data": {"inserted_count": 1, "ids": ["vector-1"]},
        },
    )

    def post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return response

    monkeypatch.setattr(storage.requests, "post", post)
    result = storage.store_vectors(
        [{"text": "原始文本切片", "vector": [0.1, 0.2]}],
        "https://files.example",
        "space-1",
        "file-1",
    )

    assert captured["url"] == "https://files.example/third/vectors/store"
    assert captured["json"] == {
        "items": [{"vector": [0.1, 0.2], "text": "原始文本切片"}],
        "knowledgeSpaceId": "space-1",
        "fileId": "file-1",
    }
    assert result == {"success": True, "inserted_count": 1, "ids": ["vector-1"]}
