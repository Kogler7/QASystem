from __future__ import annotations

from enum import IntEnum
from typing import Any
from collections.abc import Callable


class ToolCode(IntEnum):
    SUCCESS = 0
    INVALID_ARGUMENT = 3000
    KNOWLEDGE_FAILED = 3100
    GRAPH_FAILED = 3200
    DATA_FAILED = 3300
    VISUALIZATION_FAILED = 3400
    TOOL_NOT_IMPLEMENTED = 3900
    INTERNAL_ERROR = 3999


def tool_success(data: Any, message: str = "success") -> dict[str, Any]:
    return {"code": 0, "message": message, "data": data, "error": None}


def tool_error(code: ToolCode, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "code": int(code),
        "message": message,
        "data": None,
        "error": {"details": details},
    }


def normalize_tool_result(result: Any, failure_code: ToolCode, message: str = "success") -> dict[str, Any]:
    if isinstance(result, dict) and set(("code", "message", "data", "error")).issubset(result):
        return result
    if isinstance(result, dict) and (
        result.get("success") is False
        or result.get("pass") is False
        or bool(result.get("_error"))
    ):
        failure_message = (
            result.get("detail")
            or result.get("error_message")
            or result.get("_error")
            or result.get("error")
            or result.get("understanding")
            or "工具执行失败"
        )
        return tool_error(failure_code, str(failure_message), result)
    return tool_success(result, message)


def invoke_tool(operation: Callable[[], Any], failure_code: ToolCode, message: str) -> dict[str, Any]:
    try:
        return normalize_tool_result(operation(), failure_code, message)
    except Exception as exc:
        return tool_error(
            failure_code,
            f"{message}失败",
            {"type": type(exc).__name__, "message": str(exc)},
        )
