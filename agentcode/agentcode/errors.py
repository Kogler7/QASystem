from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class ErrorCode(str, Enum):
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_BAD_RESPONSE = "TOOL_BAD_RESPONSE"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    MCP_PROTOCOL_ERROR = "MCP_PROTOCOL_ERROR"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    CONSTRAINT_VIOLATION = "CONSTRAINT_VIOLATION"
    DELIVERY_NOT_SATISFIED = "DELIVERY_NOT_SATISFIED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    REPEAT_ACTION_DETECTED = "REPEAT_ACTION_DETECTED"


class RuntimeErrorInfo(BaseModel):
    code: ErrorCode
    message: str
    details: dict | None = None
