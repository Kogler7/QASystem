from __future__ import annotations

from enum import IntEnum
from typing import Any

from fastapi.responses import JSONResponse


class ErrorCode(IntEnum):
    SUCCESS = 0

    INVALID_REQUEST = 1000
    UNSUPPORTED_FILE_TYPE = 1001
    PAYLOAD_TOO_LARGE = 1002
    RESOURCE_NOT_FOUND = 1003

    DOCUMENT_PARSE_FAILED = 2000
    DOCUMENT_CONTENT_EMPTY = 2001
    PARSER_UNAVAILABLE = 2002
    DOCUMENT_CONVERT_FAILED = 2003

    CHUNK_FAILED = 3000
    INVALID_CHUNK_CONFIG = 3001
    CHUNK_RESULT_EMPTY = 3002
    CHUNK_OFFSET_FAILED = 3003

    GRAPH_EXTRACTION_FAILED = 4000
    LLM_REQUEST_FAILED = 4001
    LLM_TIMEOUT = 4002
    LLM_RATE_LIMITED = 4003
    LLM_RESPONSE_INVALID = 4004
    PARTIAL_EXTRACTION_FAILED = 4005
    ONTOLOGY_INVALID = 4006

    EMBEDDING_FAILED = 5000
    EMBEDDING_MODEL_UNAVAILABLE = 5001
    VECTOR_COUNT_MISMATCH = 5002

    CONFIDENCE_FAILED = 6000
    GRAPH_INVALID = 6001
    GRAPH_STORAGE_FAILED = 6002

    FILE_SERVICE_FAILED = 7000
    VECTOR_STORE_FAILED = 7001
    VECTOR_ID_COUNT_MISMATCH = 7002

    DEPENDENCY_UNAVAILABLE = 8000
    DEPENDENCY_TIMEOUT = 8001

    INTERNAL_ERROR = 9000
    CONFIGURATION_ERROR = 9001


_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.UNSUPPORTED_FILE_TYPE: 415,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.DOCUMENT_PARSE_FAILED: 422,
    ErrorCode.DOCUMENT_CONTENT_EMPTY: 422,
    ErrorCode.PARSER_UNAVAILABLE: 503,
    ErrorCode.DOCUMENT_CONVERT_FAILED: 422,
    ErrorCode.CHUNK_FAILED: 422,
    ErrorCode.INVALID_CHUNK_CONFIG: 422,
    ErrorCode.CHUNK_RESULT_EMPTY: 422,
    ErrorCode.CHUNK_OFFSET_FAILED: 500,
    ErrorCode.GRAPH_EXTRACTION_FAILED: 502,
    ErrorCode.LLM_REQUEST_FAILED: 502,
    ErrorCode.LLM_TIMEOUT: 504,
    ErrorCode.LLM_RATE_LIMITED: 429,
    ErrorCode.LLM_RESPONSE_INVALID: 502,
    ErrorCode.PARTIAL_EXTRACTION_FAILED: 502,
    ErrorCode.ONTOLOGY_INVALID: 422,
    ErrorCode.EMBEDDING_FAILED: 500,
    ErrorCode.EMBEDDING_MODEL_UNAVAILABLE: 503,
    ErrorCode.VECTOR_COUNT_MISMATCH: 500,
    ErrorCode.CONFIDENCE_FAILED: 500,
    ErrorCode.GRAPH_INVALID: 422,
    ErrorCode.GRAPH_STORAGE_FAILED: 502,
    ErrorCode.FILE_SERVICE_FAILED: 502,
    ErrorCode.VECTOR_STORE_FAILED: 502,
    ErrorCode.VECTOR_ID_COUNT_MISMATCH: 502,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.DEPENDENCY_TIMEOUT: 504,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.CONFIGURATION_ERROR: 500,
}

_RETRYABLE = {
    ErrorCode.PARSER_UNAVAILABLE,
    ErrorCode.GRAPH_EXTRACTION_FAILED,
    ErrorCode.LLM_REQUEST_FAILED,
    ErrorCode.LLM_TIMEOUT,
    ErrorCode.LLM_RATE_LIMITED,
    ErrorCode.LLM_RESPONSE_INVALID,
    ErrorCode.PARTIAL_EXTRACTION_FAILED,
    ErrorCode.EMBEDDING_FAILED,
    ErrorCode.EMBEDDING_MODEL_UNAVAILABLE,
    ErrorCode.CONFIDENCE_FAILED,
    ErrorCode.GRAPH_STORAGE_FAILED,
    ErrorCode.FILE_SERVICE_FAILED,
    ErrorCode.VECTOR_STORE_FAILED,
    ErrorCode.DEPENDENCY_UNAVAILABLE,
    ErrorCode.DEPENDENCY_TIMEOUT,
}


def payload(
    data: Any = None,
    info: str = "success",
    code: int = 0,
    error: Any = None,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": int(code),
        "info": info,
        "data": data,
        "error": error,
        "execution": execution,
    }


def deprecated_info(info: str, replacement: str) -> str:
    return f"{info}（Deprecated：请迁移至 {replacement}）"


def default_http_status(code: ErrorCode) -> int:
    return _HTTP_STATUS.get(code, 500)


def is_retryable(code: ErrorCode) -> bool:
    return code in _RETRYABLE


def error_response(
    code: ErrorCode,
    info: str,
    *,
    stage: str,
    details: Any = None,
    data: Any = None,
    status_code: int | None = None,
    execution: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code or default_http_status(code),
        content=payload(
            data=data,
            info=info,
            code=code,
            error={
                "type": code.name,
                "stage": stage,
                "retryable": is_retryable(code),
                "details": details,
            },
            execution=execution,
        ),
    )
