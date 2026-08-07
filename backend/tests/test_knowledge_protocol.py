from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


EXPECTED_BUSINESS_ROUTES = {
    ("POST", "/api/v1/pipelines/knowledge-ingestion"),
    ("POST", "/api/v1/documents/parse"),
    ("POST", "/api/v1/documents/chunk"),
    ("POST", "/api/v1/graphs/extract"),
    ("POST", "/api/v1/graphs/merge"),
    ("POST", "/api/v1/embeddings/texts"),
    ("POST", "/api/v1/embeddings/graphs"),
    ("POST", "/api/v1/graphs/confidence"),
    ("POST", "/api/knowledge/precipitation"),
    ("POST", "/api/component/nl2cypher/text-to-vectors"),
    ("POST", "/api/component/kg-update/calculate-confidence"),
}


def test_main_app_exposes_protocol_routes(protocol_module):
    module = importlib.import_module("backend.app.main")
    actual = {
        (method, route.path)
        for route in module.app.routes
        if route.path not in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
        for method in (getattr(route, "methods", None) or [])
    }
    assert EXPECTED_BUSINESS_ROUTES <= actual
    assert ("GET", "/health") in actual
    assert ("GET", "/file/{file_id}/{file_name}") not in actual
    assert ("GET", "/api/v1/artifacts/{artifact_id}") not in actual
    assert ("POST", "/api/component/nl2cypher/text-to-vector") not in actual

    deprecated = {
        route.path: getattr(route, "deprecated", False)
        for route in module.app.routes
    }
    assert deprecated["/api/knowledge/precipitation"] is True
    assert deprecated["/api/component/nl2cypher/text-to-vectors"] is True
    assert deprecated["/api/component/kg-update/calculate-confidence"] is True


def test_batch_vector_preserves_empty_strings(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/component/nl2cypher/text-to-vectors",
        json={
            "items": [
                {"text": "第一段"},
                {"text": ""},
                {"text": "第二段"},
            ]
        },
    )
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["count"] == 3
    assert [item["vector"] for item in body["data"]["items"]] == [
        [0.0, 1.0],
        [1.0, 1.0],
        [2.0, 1.0],
    ]
    assert all("text" not in item for item in body["data"]["items"])
    assert "Deprecated" in body["info"]
    assert "message" not in body


def test_v1_text_embeddings_preserve_item_order(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/v1/embeddings/texts",
        json={"items": [{"text": "第一段"}, {"text": ""}]},
    )
    body = response.json()
    assert body["code"] == 0
    assert "message" not in body
    assert body["data"]["dimension"] == 2
    assert [item["vector"] for item in body["data"]["items"]] == [
        [0.0, 1.0],
        [1.0, 1.0],
    ]
    assert all("text" not in item for item in body["data"]["items"])


def test_v1_text_embeddings_reject_unknown_index_field(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/v1/embeddings/texts",
        json={"items": [{"index": 1, "text": "A"}]},
    )
    assert response.status_code == 422


def test_v1_document_chunk_returns_offsets(protocol_module):
    body = TestClient(protocol_module.app).post(
        "/api/v1/documents/chunk",
        json={"file_name": "sample.md", "markdown": "第一段。第二段。", "options": {"size": 500}},
    ).json()
    assert body["code"] == 0
    assert body["data"]["items"][0] == {
        "text": "第一段。第二段。",
        "range": [0, 8],
    }


def test_v1_document_parse_returns_markdown_without_storage(protocol_module):
    body = TestClient(protocol_module.app).post(
        "/api/v1/documents/parse",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
    ).json()
    assert body["code"] == 0
    assert body["data"]["markdown"] == "# 测试文档\n测试内容"
    assert body["data"]["parser"]["final_engine"] == "pandas"


def test_v1_graph_extraction_preserves_input_order(protocol_module):
    body = TestClient(protocol_module.app).post(
        "/api/v1/graphs/extract",
        json={
            "ontology": "class Term",
            "items": [
                {"text": "A"},
                {"text": "B"},
            ],
        },
    ).json()
    assert body["code"] == 0
    assert body["data"]["graph"] == {"nodes": [], "relationships": []}
    assert "graphs" not in body["data"]
    assert "ranges" not in body["data"]


def test_v1_graph_extraction_rejects_chunk_ranges(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/v1/graphs/extract",
        json={
            "ontology": "class Term",
            "items": [{"text": "A", "ranges": [0, 1]}],
        },
    )
    assert response.status_code == 422


def test_v1_graph_embeddings_use_stable_serialization(protocol_module):
    body = TestClient(protocol_module.app).post(
        "/api/v1/embeddings/graphs",
        json={"items": [{"graph": {"relationships": [], "nodes": []}}]},
    ).json()
    assert body["code"] == 0
    assert body["data"]["items"][0]["serialized_text"] == '{"nodes": [], "relationships": []}'


def test_v1_graph_merge_deduplicates_nodes_and_relationships(protocol_module):
    relationship = {"from_uid": "Term::a", "to_uid": "Term::b", "type": "RELATED"}
    body = TestClient(protocol_module.app).post(
        "/api/v1/graphs/merge",
        json={
            "items": [
                {
                    "graph": {
                        "nodes": [{"uid": "Term::a", "label": "Term", "properties": {"name": "A"}}],
                        "relationships": [relationship],
                    },
                },
                {
                    "graph": {
                        "nodes": [
                            {"uid": "Term::a", "label": "Term", "properties": {"description": "merged"}},
                            {"uid": "Term::b", "label": "Term", "properties": {"name": "B"}},
                        ],
                        "relationships": [relationship],
                    },
                },
            ]
        },
    ).json()
    assert body["code"] == 0
    assert body["data"]["count"] == 2
    assert len(body["data"]["graph"]["nodes"]) == 2
    assert len(body["data"]["graph"]["relationships"]) == 1


def test_v1_graph_merge_discards_relationships_missing_nodes(protocol_module):
    body = TestClient(protocol_module.app).post(
        "/api/v1/graphs/merge",
        json={
            "items": [
                {
                    "graph": {
                        "nodes": [{"uid": "Term::a", "label": "Term", "properties": {"name": "A"}}],
                        "relationships": [{"from_uid": "Term::a", "to_uid": "Term::missing", "type": "RELATED"}],
                    },
                }
            ]
        },
    ).json()
    assert body["code"] == 0
    assert body["data"]["graph"]["nodes"] == [
        {"uid": "Term::a", "label": "Term", "properties": {"name": "A"}}
    ]
    assert body["data"]["graph"]["relationships"] == []


def test_batch_vector_rejects_empty_list(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/component/nl2cypher/text-to-vectors",
        json={"items": []},
    )
    assert response.status_code == 422


def test_deprecated_confidence_reports_unavailable_capability(protocol_module):
    graph = {"nodes": [{"uid": "n1", "label": "Term", "properties": {}}], "relationships": []}
    response = TestClient(protocol_module.app).post(
        "/api/component/kg-update/calculate-confidence",
        json={"graph": graph},
    )
    body = response.json()
    assert response.status_code == 503
    assert body["code"] == 8002
    assert body["error"]["type"] == "EXTERNAL_CAPABILITY_UNAVAILABLE"
    assert body["error"]["retryable"] is False
    assert "Deprecated" in body["info"]


def test_v1_confidence_reports_unavailable_capability(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/v1/graphs/confidence",
        json={"graph": {"nodes": [], "relationships": []}},
    )
    body = response.json()
    assert response.status_code == 503
    assert body["code"] == 8002
    assert body["error"]["type"] == "EXTERNAL_CAPABILITY_UNAVAILABLE"
    assert body["error"]["details"]["capability"] == "confidence.calculate"
    assert "message" not in body


def test_precipitation_requires_ontology(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/knowledge/precipitation",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
    )
    assert response.status_code == 422


def test_deprecated_pipeline_uses_v1_fields_and_response_shape(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/knowledge/precipitation",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={
            "ontology": "@startuml\nclass Term\n@enduml",
            "options": '{"chunking":{"strategy":"symbols","size":500}}',
        },
    )
    body = response.json()
    assert body["code"] == 0
    assert body["error"] is None
    assert body["data"]["artifacts"] is None
    assert body["data"]["extraction"]["graph"] == {"nodes": [], "relationships": []}
    assert body["data"]["extraction"]["ranges"] == [[0, len("# 测试文档\n测试内容")]]
    assert body["data"]["stats"]["extraction"]["chunks_extracted"] == 1
    assert body["data"]["stats"]["extraction"]["attempts"] == []
    assert body["data"]["stats"]["extraction"]["scheduler"] == {
        "max_concurrency": 4,
        "qps": 0.0,
        "qpm": 0,
    }
    assert body["data"]["stats"]["chunking"]["vector_source"] == "text_chunk"
    assert body["data"]["stats"]["document"]["parser"] == {
        "preferred_engine": "pandas",
        "final_engine": "pandas",
    }
    assert "parser" not in body["data"]
    assert "Deprecated" in body["info"]


def test_v1_pipeline_returns_modern_artifact_and_graph_shape(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "@startuml\nclass Term\n@enduml"},
    )
    body = response.json()
    assert body["code"] == 0
    assert body["info"] == "知识沉淀完成"
    assert "message" not in body
    assert body["data"]["summary"] == "节点数: 0"
    assert body["data"]["artifacts"] is None
    assert body["data"]["extraction"]["graph"] == {"nodes": [], "relationships": []}


def test_v1_pipeline_can_return_unmerged_chunk_graphs(protocol_module):
    body = TestClient(protocol_module.app).post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "class Term", "options": '{"extraction":{"merge":false}}'},
    ).json()
    assert body["code"] == 0
    assert "graph" not in body["data"]["extraction"]
    assert body["data"]["extraction"]["graphs"] == [
        {"nodes": [], "relationships": []}
    ]


def test_pipeline_reports_graph_write_unavailable(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={
            "ontology": "class Term",
            "options": '{"extraction":{"merge":false},"storage":{"persist_graph":true}}',
        },
    )
    body = response.json()
    assert response.status_code == 503
    assert body["code"] == 8002
    assert body["error"]["type"] == "EXTERNAL_CAPABILITY_UNAVAILABLE"
    assert body["error"]["details"]["capability"] == "graph.write"


def test_v1_graph_extraction_can_skip_deduplication(protocol_module):
    body = TestClient(protocol_module.app).post(
        "/api/v1/graphs/extract",
        json={
            "ontology": "class Term",
            "items": [{"text": "A"}, {"text": "B"}],
            "options": {"merge": False},
        },
    ).json()
    assert body["code"] == 0
    assert "graph" not in body["data"]
    assert body["data"]["graphs"] == [
        {"nodes": [], "relationships": []},
        {"nodes": [], "relationships": []},
    ]


def test_v1_pipeline_rejects_invalid_options(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "class Term", "options": '{"chunking":{"size":0}}'},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 1000


def test_pipeline_returns_file_service_metadata_when_configured(protocol_module, monkeypatch):
    from backend.app.services import knowledge_precipitation as pipeline_module

    captured = {}

    def store_vectors(items, _base_url, _knowledge_space_id, _file_id):
        captured["items"] = items
        return {"success": True, "inserted_count": len(items), "ids": ["vector-0"]}

    monkeypatch.setenv("FILE_SERVICE_URL", "https://files.example")
    monkeypatch.setattr(pipeline_module, "store_vectors", store_vectors)
    body = TestClient(protocol_module.app).post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "class Term"},
    ).json()
    assert body["code"] == 0
    assert body["data"]["artifacts"]["markdown"] == {
        "id": "id-sample.md",
        "name": "sample.md",
        "path": "/storage/sample.md",
        "url": "/files/sample.md",
    }
    assert set(body["data"]["artifacts"]) == {"markdown", "vector_ids"}
    assert body["data"]["artifacts"]["vector_ids"] == ["vector-0"]
    assert body["data"]["extraction"]["graph"] == {"nodes": [], "relationships": []}
    assert captured["items"][0]["text"] == "# 测试文档\n测试内容"
    assert "serialized_text" not in captured["items"][0]
    assert body["data"]["stats"]["chunking"]["vector_source"] == "text_chunk"


def test_pipeline_reports_file_service_failure(protocol_module, monkeypatch):
    from backend.app.services import knowledge_precipitation as pipeline_module

    monkeypatch.setenv("FILE_SERVICE_URL", "https://files.example")
    monkeypatch.setattr(
        pipeline_module,
        "upload_file",
        lambda *_args, **_kwargs: {"success": False, "error": "unavailable"},
    )
    response = TestClient(protocol_module.app).post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "class Term"},
    )
    body = response.json()
    assert response.status_code == 502
    assert body["code"] == 7000
    assert body["error"]["type"] == "FILE_SERVICE_FAILED"


def test_pipeline_reports_vector_store_failure_with_extraction_partial_data(protocol_module, monkeypatch):
    from backend.app.services import knowledge_precipitation as pipeline_module

    monkeypatch.setenv("FILE_SERVICE_URL", "https://files.example")
    monkeypatch.setattr(
        pipeline_module,
        "store_vectors",
        lambda *_args, **_kwargs: {"success": False, "error": "unavailable"},
    )
    response = TestClient(protocol_module.app).post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "class Term"},
    )
    body = response.json()
    assert response.status_code == 502
    assert body["code"] == 7001
    assert body["error"]["type"] == "VECTOR_STORE_FAILED"
    assert body["data"]["artifacts"]["markdown"]["id"] == "id-sample.md"
    assert body["data"]["extraction"]["graph"] == {"nodes": [], "relationships": []}


def test_pipeline_rejects_vector_id_count_mismatch(protocol_module, monkeypatch):
    from backend.app.services import knowledge_precipitation as pipeline_module

    monkeypatch.setenv("FILE_SERVICE_URL", "https://files.example")
    monkeypatch.setattr(
        pipeline_module,
        "store_vectors",
        lambda *_args, **_kwargs: {"success": True, "inserted_count": 1, "ids": []},
    )
    response = TestClient(protocol_module.app).post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "class Term"},
    )
    body = response.json()
    assert response.status_code == 502
    assert body["code"] == 7002
    assert body["error"]["type"] == "VECTOR_ID_COUNT_MISMATCH"
    assert body["error"]["retryable"] is False


def test_deprecated_pipeline_accepts_v1_structured_options(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/knowledge/precipitation",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={
            "ontology": "@startuml\nclass Term\n@enduml",
            "options": (
                '{"parser":{"engine":"auto","mineru_options":{"language":"ch",'
                '"ocr_mode":"on","table_enable":false,"formula_enable":false}}}'
            ),
        },
    )

    assert response.status_code == 200
    assert response.json()["code"] == 0


def test_deprecated_pipeline_rejects_invalid_v1_options(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/knowledge/precipitation",
        files={"file": ("sample.pdf", b"%PDF-test", "application/pdf")},
        data={
            "ontology": "@startuml\nclass Term\n@enduml",
            "options": '{"parser":{"engine":"vlm","mineru_options":{"ocr_mode":"sometimes"}}}',
        },
    )
    body = response.json()

    assert response.status_code == 400
    assert body["code"] == 1000
    assert body["error"]["stage"] == "validation"
    assert "Deprecated" in body["info"]


def test_deprecated_pipeline_rejects_coerced_v1_option_types(protocol_module):
    response = TestClient(protocol_module.app).post(
        "/api/knowledge/precipitation",
        files={"file": ("sample.pdf", b"%PDF-test", "application/pdf")},
        data={
            "ontology": "@startuml\nclass Term\n@enduml",
            "options": '{"parser":{"mineru_options":{"table_enable":"false"}}}',
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
    }


def test_pipeline_returns_parser_execution_status_at_data_level(protocol_module):
    from backend.app.schemas import ParserConfig, PipelineOptions
    from backend.app.services.algorithm_capabilities import DocumentParsingService
    from backend.app.services.knowledge_precipitation import (
        KnowledgeIngestionRequest,
        KnowledgePrecipitationService,
    )

    class FallbackProcessor:
        def process_file(self, *_args, **_kwargs):
            return {
                "success": True,
                "content": "# Parsed",
                "metadata": {
                    "model_used": "mineru",
                    "engine_selection": {
                        "preferred_engine": "docling",
                        "requested_engine": "mineru",
                        "request_role": "fallback_candidate",
                    },
                    "fallback_from": {
                        "engine": "docling",
                        "error": "Docling 解析失败",
                    },
                    "mineru_options": {
                        "language": "ch",
                        "ocr_mode": "on",
                        "table_enable": True,
                        "formula_enable": False,
                    },
                },
            }

    parser_config = ParserConfig(
        engine="mineru",
        mineru_options={
            "language": "ch",
            "ocr_mode": "on",
            "table_enable": True,
            "formula_enable": False,
        },
    )
    expected_status = {
        "preferred_engine": "docling",
        "requested_engine": "mineru",
        "request_handling": "fallback_candidate",
        "final_engine": "mineru",
        "fallback": {
            "from_engine": "docling",
            "reason": "Docling 解析失败",
        },
    }

    parsed = DocumentParsingService(FallbackProcessor).parse(
        "demo.docx",
        b"document",
        parser_config,
    )
    assert parsed["parser"] == expected_status

    service = KnowledgePrecipitationService(processor_factory=FallbackProcessor)
    result = service.process(
        KnowledgeIngestionRequest(
            file_name="demo.docx",
            content=b"document",
            ontology="class Term",
            options=PipelineOptions(
                parser=parser_config,
            ),
        )
    )

    parser_status = result["stats"]["document"]["parser"]
    assert parser_status == expected_status
    assert "mineru_options" not in parser_status
    assert "parser" not in result


def test_document_processor_is_reused_and_parse_error_is_specific(protocol_module):
    from backend.app.services.knowledge_precipitation import (
        KnowledgeIngestionRequest,
        KnowledgePrecipitationService,
        PipelineError,
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
    request = KnowledgeIngestionRequest(
        file_name="sample.doc",
        content=b"test",
        ontology="@startuml\nclass Term\n@enduml",
    )

    for _ in range(2):
        try:
            service.process(request)
        except PipelineError as exc:
            assert exc.info == "文档解析失败：缺少 LibreOffice（soffice）"
        else:
            raise AssertionError("expected PipelineError")

    assert FailingProcessor.instances == 1
    assert FailingProcessor.calls == 2
