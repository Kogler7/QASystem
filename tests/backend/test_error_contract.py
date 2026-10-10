from __future__ import annotations

import json

import pytest

from backend.app.responses import ErrorCode, error_response
from backend.app.schemas import ParserConfig, TextChunk, TextEmbeddingItem
from backend.app.services import algorithm_capabilities as capabilities


def test_error_response_derives_type_retryability_and_http_status():
    response = error_response(
        ErrorCode.LLM_TIMEOUT,
        "LLM 请求超时",
        stage="graph_extraction",
        details={"index": 3},
    )
    body = json.loads(response.body)

    assert response.status_code == 504
    assert body["code"] == 4002
    assert body["error"] == {
        "type": "LLM_TIMEOUT",
        "stage": "graph_extraction",
        "retryable": True,
        "details": {"index": 3},
    }
    assert body["execution"] is None


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("不支持的文件类型: .xyz", ErrorCode.UNSUPPORTED_FILE_TYPE),
        ("缺少 LibreOffice（soffice）", ErrorCode.PARSER_UNAVAILABLE),
        ("DOC 转换失败", ErrorCode.DOCUMENT_CONVERT_FAILED),
        ("解析器返回未知错误", ErrorCode.DOCUMENT_PARSE_FAILED),
    ],
)
def test_document_parser_classifies_failure_reason(reason, expected):
    class FailingProcessor:
        def process_file(self, *_args, **_kwargs):
            return {"success": False, "error": reason}

    service = capabilities.DocumentParsingService(FailingProcessor)
    with pytest.raises(capabilities.CapabilityError) as captured:
        service.parse("sample.docx", b"content", ParserConfig())

    assert captured.value.code == expected


def test_graph_extraction_classifies_invalid_llm_response(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_STRICT_CHUNK_FAILURES", "1")
    monkeypatch.setattr(
        capabilities,
        "extract_chunks",
        lambda **_kwargs: [
            {
                "index": 0,
                "raw": {},
                "graph": {"nodes": [], "relationships": []},
                "attempts": [],
                "error": "无法解析 JSON",
                "error_code": "LLM_JSON_PARSE_FAILED",
                "raw_response": "invalid",
            }
        ],
    )

    with pytest.raises(capabilities.CapabilityError) as captured:
        capabilities.GraphExtractionService().extract(
            "class Term",
            [TextChunk(text="text")],
        )

    assert captured.value.code == ErrorCode.LLM_RESPONSE_INVALID


def test_graph_extraction_reports_partial_failure(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_STRICT_CHUNK_FAILURES", "1")
    monkeypatch.setattr(
        capabilities,
        "extract_chunks",
        lambda **_kwargs: [
            {
                "index": 0,
                "raw": {},
                "graph": {"nodes": [], "relationships": []},
                "attempts": [],
                "error": None,
                "error_code": None,
                "raw_response": "",
            },
            {
                "index": 1,
                "raw": {},
                "graph": {"nodes": [], "relationships": []},
                "attempts": [],
                "error": "请求失败",
                "error_code": "LLM_REQUEST_FAILED",
                "raw_response": "",
            },
        ],
    )

    with pytest.raises(capabilities.CapabilityError) as captured:
        capabilities.GraphExtractionService().extract(
            "class Term",
            [TextChunk(text="ok"), TextChunk(text="failed")],
        )

    assert captured.value.code == ErrorCode.PARTIAL_EXTRACTION_FAILED
    assert captured.value.details["failed_items"][0]["position"] == 1


@pytest.mark.parametrize("strict", ["", "0", "1"])
@pytest.mark.parametrize(
    ("failure_code", "expected"),
    [("LLM_REQUEST_FAILED", ErrorCode.LLM_REQUEST_FAILED),
     ("LLM_TIMEOUT", ErrorCode.LLM_TIMEOUT),
     ("LLM_SCHEMA_INVALID", ErrorCode.LLM_RESPONSE_INVALID)],
)
def test_all_chunks_failed_is_terminal_even_when_degradation_is_enabled(monkeypatch, strict, failure_code, expected):
    monkeypatch.setenv("KG_EXTRACT_STRICT_CHUNK_FAILURES", strict)
    monkeypatch.setattr(capabilities, "extract_chunks", lambda **_kwargs: [
        {"index": i, "error": "抽取失败", "error_code": failure_code,
         "graph": {"nodes": [], "relationships": []}, "attempts": []}
        for i in range(2)
    ])

    with pytest.raises(capabilities.CapabilityError) as captured:
        capabilities.GraphExtractionService().extract("class Term", [TextChunk(text="A"), TextChunk(text="B")])

    assert captured.value.code == expected
    assert captured.value.details["failed_count"] == 2
    assert [item["position"] for item in captured.value.details["failed_items"]] == [0, 1]


def test_embedding_detects_vector_count_mismatch(monkeypatch):
    monkeypatch.setattr(capabilities, "batch_text_to_vectors", lambda _texts: [])

    with pytest.raises(capabilities.CapabilityError) as captured:
        capabilities.EmbeddingService().texts([TextEmbeddingItem(text="text")])

    assert captured.value.code == ErrorCode.VECTOR_COUNT_MISMATCH


@pytest.mark.parametrize("error_code", ["ONTOLOGY_EMPTY", "TARGET_SCOPE_EMPTY", "ONTOLOGY_INVALID"])
def test_graph_extraction_classifies_ontology_failure(monkeypatch, error_code):
    """空本体/零目标范围的整批失败映射为 4006（不可重试），而不是通用 502。"""
    monkeypatch.setenv("KG_EXTRACT_STRICT_CHUNK_FAILURES", "1")
    monkeypatch.setattr(
        capabilities,
        "extract_chunks",
        lambda **_kwargs: [
            {
                "index": 0,
                "raw": {},
                "graph": {"nodes": [], "relationships": []},
                "attempts": [],
                "error": "本体未解析出任何 class 声明",
                "error_code": error_code,
                "raw_response": "",
            }
        ],
    )

    with pytest.raises(capabilities.CapabilityError) as captured:
        capabilities.GraphExtractionService().extract(
            "",
            [TextChunk(text="text")],
        )

    assert captured.value.code == ErrorCode.ONTOLOGY_INVALID
