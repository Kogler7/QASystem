from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from .errors import RuntimeErrorInfo


class McpServerConfig(BaseModel):
    name: str
    transport: Literal["http", "streamable_http", "stdio", "sse"]
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    auth_ref: str | None = None


class SkillRef(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    path: str | None = None
    uri: str | None = None
    version: str | None = None
    inject_layer: Literal["system", "developer", "task"] = "developer"


class ToolPolicy(BaseModel):
    allow: list[str] = Field(default_factory=list)


class BudgetConfig(BaseModel):
    max_turns: int = 50
    max_tool_calls: int = 70
    max_time_s: int = 1200
    max_retries_per_tool: int = 2
    repeat_action_threshold: int = 10


class DeliveryConfig(BaseModel):
    output_schema_id: str
    json_schema: dict[str, Any]


class SessionConfig(BaseModel):
    """一次智能体会话的全量配置：模型侧由宿主注入 LLM；工具侧仅通过 MCP。"""

    session_id: str
    trace_id: str
    model: str | None = None
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)
    skills: list[SkillRef] = Field(default_factory=list)
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    tool_execution: Literal["sequential", "parallel"] = "sequential"
    delivery: DeliveryConfig | None = None


class AgentState(BaseModel):
    session_id: str
    trace_id: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    turn: int = 0
    tool_calls_used: int = 0
    started_at: float
    status: Literal["running", "completed", "failed"] = "running"
    pending_tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)


class FinalResult(BaseModel):
    status: Literal["ok", "failed"]
    payload: Any = None
    warnings: list[str] = Field(default_factory=list)
    error: RuntimeErrorInfo | None = None
    spent: dict[str, Any] = Field(default_factory=dict)
    trace_ref: str | None = None


class StreamEvent(BaseModel):
    """SSE 常规事件。"""

    type: str
    data: dict[str, Any] = {}

    def __str__(self) -> str:
        payload = {"type": self.type, **self.data}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class StreamFirstEvent(BaseModel):
    """SSE 首条 / 心跳。"""

    model_config = {"extra": "allow"}

    type: str

    def __str__(self) -> str:
        if self.type == "heartbeat":
            return ": heartbeat\n\n"
        return f"data: {self.model_dump_json(exclude_none=True)}\n\n"
