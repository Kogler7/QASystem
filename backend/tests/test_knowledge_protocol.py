from __future__ import annotations

import importlib
import inspect

from fastapi.testclient import TestClient


EXPECTED_ROUTES = {
    ("POST", "/api/knowledge/precipitation"),
    ("POST", "/api/component/nl2cypher/text-to-vector"),
    ("POST", "/api/component/nl2cypher/text-to-vectors"),
    ("POST", "/api/component/kg-update/calculate-confidence"),
}


def test_main_app_exposes_only_protocol_and_health(protocol_module):
    module = importlib.import_module("backend.app.main")
    actual = {
        (method, route.path)
        for route in module.app.routes
        if route.path not in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
        for method in (getattr(route, "methods", None) or [])
    }
    assert actual == EXPECTED_ROUTES | {("GET", "/health")}
    assert any(route.__class__.__name__ == "Mount" for route in module.app.routes)

    with TestClient(module.app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_backend_protocol_router_exposes_only_four_routes(protocol_module):
    module = importlib.import_module("backend.app.routers.knowledge_precipitation")
    actual = {
        (method, route.path)
        for route in module.router.routes
        for method in route.methods
    }
    assert actual == EXPECTED_ROUTES


def test_precipitation_signature_matches_protocol(protocol_module):
    parameters = inspect.signature(protocol_module.knowledge_precipitation).parameters
    assert {
        "file",
        "lang",
        "parser_engine",
        "chunk_character",
        "chunk_size",
        "chunk_type",
        "similarity",
        "ontology",
        "save_to_db",
    }.issubset(parameters)


def test_single_text_vector_contract(protocol_module, monkeypatch):
    monkeypatch.setattr(protocol_module, "single_text_to_vector", lambda text: [0.25, -0.5])
    response = TestClient(protocol_module.app).post(
        "/api/component/nl2cypher/text-to-vector",
        json={"text": "知识空间"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "code": 0,
        "info": "文本转向量完成",
        "message": "文本转向量完成",
        "data": {"text": "知识空间", "vector": [0.25, -0.5]},
    }


def test_single_text_vector_rejects_blank_text(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/component/nl2cypher/text-to-vector",
        json={"text": "  "},
    )
    assert response.status_code == 400


def test_batch_text_vector_contract(protocol_module, monkeypatch):
    monkeypatch.setattr(
        protocol_module,
        "batch_text_to_vectors",
        lambda texts: [[float(index), 1.0] for index, _ in enumerate(texts)],
    )
    response = TestClient(protocol_module.app).post(
        "/api/component/nl2cypher/text-to-vectors",
        json={"texts": ["第一段", " ", "第二段"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"] == {"count": 2, "vectors": [[0.0, 1.0], [1.0, 1.0]]}


def test_confidence_contract_without_relationships(protocol_module):
    graph = {"nodes": [{"uid": "n1", "label": "Term", "properties": {}}], "relationships": []}
    response = TestClient(protocol_module.app).post(
        "/api/component/kg-update/calculate-confidence",
        json={"graph": graph},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["total_count"] == 0
    assert body["data"]["graph"]["nodes"] == graph["nodes"]
    assert body["data"]["details"] == {}


def test_precipitation_success_contract(protocol_module, monkeypatch):
    graph = {
        "nodes": [
            {
                "uid": "term::测试",
                "label": "Term",
                "properties": {"name": "测试"},
            }
        ],
        "relationships": [],
    }

    monkeypatch.setattr(
        protocol_module,
        "extract_v2",
        lambda **_kwargs: {"graph": graph},
    )
    monkeypatch.setattr(protocol_module, "chunk_document", lambda *_args, **_kwargs: ["测试内容"])
    monkeypatch.setattr(protocol_module, "batch_text_to_vectors", lambda _texts: [[0.1, 0.2]])
    monkeypatch.setattr(
        protocol_module,
        "upload_md_to_file_service",
        lambda _content, file_name, *_args, **_kwargs: {
            "success": True,
            "file_url": f"/knowledge/{file_name}",
        },
    )

    response = TestClient(protocol_module.app).post(
        "/api/knowledge/precipitation",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"lang": "ch", "chunk_type": "1", "save_to_db": "false"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["info"] == "success"
    assert set(body["data"]) >= {
        "name",
        "understanding",
        "origin_url",
        "vector_url",
        "extract_url",
        "graphs",
    }
    assert body["data"]["name"] == "sample"
    assert body["data"]["graphs"]["graphs"][0]["graph"]["nodes"] == graph["nodes"]


def test_precipitation_stores_graph_when_requested(protocol_module, monkeypatch):
    stored = []
    graph = {"nodes": [], "relationships": []}

    monkeypatch.setattr(protocol_module, "extract_v2", lambda **_kwargs: {"graph": graph})
    monkeypatch.setattr(protocol_module, "chunk_document", lambda *_args, **_kwargs: ["内容"])
    monkeypatch.setattr(protocol_module, "batch_text_to_vectors", lambda _texts: [[0.1]])
    monkeypatch.setattr(
        protocol_module,
        "upload_md_to_file_service",
        lambda *_args, **_kwargs: {"success": True, "file_url": "/file.md"},
    )
    monkeypatch.setattr(
        protocol_module,
        "store_extraction_to_neo4j",
        lambda **kwargs: stored.append(kwargs["graph"]) or {"success": True},
    )

    response = TestClient(protocol_module.app).post(
        "/api/knowledge/precipitation",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"save_to_db": "true"},
    )

    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert stored == [graph]
