from __future__ import annotations

from enum import IntEnum
from typing import Any

from fastapi.responses import JSONResponse


class ErrorCode(IntEnum):
    SUCCESS = 0
    INVALID_REQUEST = 1000
    DOCUMENT_PARSE_FAILED = 1001
    KNOWLEDGE_EXTRACT_FAILED = 1002
    CHUNK_FAILED = 1003
    VECTORIZE_FAILED = 1004
    STORAGE_FAILED = 1005
    CONFIDENCE_FAILED = 1006
    INTERNAL_ERROR = 1999


def payload(data: Any = None, message: str = "success", code: int = 0, error: Any = None) -> dict[str, Any]:
    return {
        "code": int(code),
        "message": message,
        "info": message,
        "data": data,
        "error": error,
    }


def error_response(
    code: ErrorCode,
    message: str,
    *,
    stage: str,
    details: Any = None,
    data: Any = None,
    status_code: int = 400,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=payload(
            data=data,
            message=message,
            code=code,
            error={"stage": stage, "details": details},
        ),
    )
