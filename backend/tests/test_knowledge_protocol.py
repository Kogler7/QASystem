from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


EXPECTED_BUSINESS_ROUTES = {
    ("POST", "/api/knowledge/precipitation"),
    ("POST", "/api/component/nl2cypher/text-to-vectors"),
    ("POST", "/api/component/kg-update/calculate-confidence"),
}


def test_main_app_exposes_protocol_health_and_artifacts(protocol_module):
    module = importlib.import_module("backend.app.main")
    actual = {
        (method, route.path)
        for route in module.app.routes
        if route.path not in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
        for method in (getattr(route, "methods", None) or [])
    }
    assert EXPECTED_BUSINESS_ROUTES <= actual
    assert ("GET", "/health") in actual
    assert ("GET", "/file/{file_id}/{file_name}") in actual
    assert ("POST", "/api/component/nl2cypher/text-to-vector") not in actual


def test_batch_vector_preserves_empty_strings(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/component/nl2cypher/text-to-vectors",
        json={"texts": ["第一段", "", "第二段"]},
    )
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["count"] == 3
    assert body["data"]["texts"] == ["第一段", "", "第二段"]
    assert [item["index"] for item in body["data"]["items"]] == [0, 1, 2]


def test_batch_vector_rejects_empty_list(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/component/nl2cypher/text-to-vectors",
        json={"texts": []},
    )
    assert response.status_code == 422


def test_confidence_contract_without_relationships(protocol_module):
    graph = {"nodes": [{"uid": "n1", "label": "Term", "properties": {}}], "relationships": []}
    body = TestClient(protocol_module.app).post(
        "/api/component/kg-update/calculate-confidence",
        json={"graph": graph},
    ).json()
    assert body["code"] == 0
    assert body["error"] is None
    assert body["data"]["total_count"] == 0


def test_precipitation_requires_ontology(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/knowledge/precipitation",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
    )
    assert response.status_code == 422


def test_precipitation_returns_complete_artifact_urls(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/knowledge/precipitation",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "@startuml\nclass Term\n@enduml", "chunk_type": "1"},
    )
    body = response.json()
    assert body["code"] == 0
    assert body["error"] is None
    assert body["data"]["origin_url"].endswith("sample.md")
    assert body["data"]["extract_url"].endswith("sample_extract.json")
    assert body["data"]["vector_url"].endswith("sample_vectors.json")
