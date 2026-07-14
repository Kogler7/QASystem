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
    assert body["data"]["stats"]["document"]["parser"] == {
        "preferred_engine": "pandas",
        "final_engine": "pandas",
    }


def test_precipitation_accepts_structured_parser_config(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/knowledge/precipitation",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={
            "ontology": "@startuml\nclass Term\n@enduml",
            "parser_config": (
                '{"engine":"auto","mineru_options":{"language":"ch",'
                '"ocr_mode":"on","table_enable":false,"formula_enable":false}}'
            ),
        },
    )

    assert response.status_code == 200
    assert response.json()["code"] == 0


def test_precipitation_rejects_invalid_parser_config(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/knowledge/precipitation",
        files={"file": ("sample.pdf", b"%PDF-test", "application/pdf")},
        data={
            "ontology": "@startuml\nclass Term\n@enduml",
            "parser_config": '{"engine":"vlm","mineru_options":{"ocr_mode":"sometimes"}}',
        },
    )
    body = response.json()

    assert response.status_code == 400
    assert body["code"] == 1000
    assert body["error"]["stage"] == "validation"


def test_precipitation_rejects_coerced_parser_config_types(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/knowledge/precipitation",
        files={"file": ("sample.pdf", b"%PDF-test", "application/pdf")},
        data={
            "ontology": "@startuml\nclass Term\n@enduml",
            "parser_config": '{"mineru_options":{"table_enable":"false"}}',
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == 1000


def test_parser_metadata_is_returned_as_compact_summary(protocol_module):
    from backend.app.services.knowledge_precipitation import KnowledgePrecipitationService

    summary = KnowledgePrecipitationService._parser_summary(
        {
            "model_used": "mineru",
            "engine_selection": {
                "preferred_engine": "docling",
                "requested_engine": "mineru",
                "request_role": "fallback_candidate",
            },
            "fallback_from": {"engine": "docling", "error": "parse failed"},
            "mineru_options": {
                "language": "ch",
                "ocr_mode": "on",
                "table_enable": True,
                "formula_enable": False,
            },
            "file_path": "/internal/path/that/must/not/leak",
            "allowed_engines": ["internal", "noise"],
        }
    )

    assert summary == {
        "preferred_engine": "docling",
        "final_engine": "mineru",
        "requested_engine": "mineru",
        "request_handling": "fallback_candidate",
        "fallback": {"from_engine": "docling", "reason": "parse failed"},
        "mineru_options": {
            "language": "ch",
            "ocr_mode": "on",
            "table_enable": True,
            "formula_enable": False,
        },
    }


def test_document_processor_is_reused_and_parse_error_is_specific(protocol_module):
    from backend.app.services.knowledge_precipitation import (
        KnowledgePrecipitationService,
        PipelineError,
        PrecipitationRequest,
    )

    class FailingProcessor:
        instances = 0
        calls = 0

        def __init__(self):
            type(self).instances += 1

        def process_file(self, *_args, **_kwargs):
            type(self).calls += 1
            return {"success": False, "error": "缺少 LibreOffice（soffice）"}

    service = KnowledgePrecipitationService(processor_factory=FailingProcessor)
    request = PrecipitationRequest(
        file_name="sample.doc",
        content=b"test",
        ontology="@startuml\nclass Term\n@enduml",
    )

    for _ in range(2):
        try:
            service.process(request)
        except PipelineError as exc:
            assert exc.message == "文档解析失败：缺少 LibreOffice（soffice）"
        else:
            raise AssertionError("expected PipelineError")

    assert FailingProcessor.instances == 1
    assert FailingProcessor.calls == 2
