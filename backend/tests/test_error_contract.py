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


def test_graph_extraction_tolerates_partial_failure(monkeypatch):
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

    batch = capabilities.GraphExtractionService().extract(
        "class Term",
        [TextChunk(text="ok"), TextChunk(text="failed")],
    )

    assert batch.failures[0]["position"] == 1
    assert batch.graph_data()["failures"] == batch.failures
    assert batch.graph == {"nodes": [], "relationships": []}


def test_graph_extraction_raises_when_all_chunks_fail(monkeypatch):
    def failed_item(index, error_code):
        return {
            "index": index,
            "raw": {},
            "graph": {"nodes": [], "relationships": []},
            "attempts": [],
            "error": "请求失败",
            "error_code": error_code,
            "raw_response": "",
        }

    monkeypatch.setattr(
        capabilities,
        "extract_chunks",
        lambda **_kwargs: [failed_item(0, "LLM_REQUEST_FAILED"), failed_item(1, "LLM_REQUEST_FAILED")],
    )

    with pytest.raises(capabilities.CapabilityError) as captured:
        capabilities.GraphExtractionService().extract(
            "class Term",
            [TextChunk(text="a"), TextChunk(text="b")],
        )

    assert captured.value.code == ErrorCode.LLM_REQUEST_FAILED
    assert captured.value.info == "全部文本切片知识抽取失败"


def test_embedding_detects_vector_count_mismatch(monkeypatch):
    monkeypatch.setattr(capabilities, "batch_text_to_vectors", lambda _texts: [])

    with pytest.raises(capabilities.CapabilityError) as captured:
        capabilities.EmbeddingService().texts([TextEmbeddingItem(text="text")])

    assert captured.value.code == ErrorCode.VECTOR_COUNT_MISMATCH
