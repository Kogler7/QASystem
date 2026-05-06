from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .errors import RuntimeErrorInfo


class McpServerConfig(BaseModel):
    name: str
    transport: Literal["http", "stdio"]
    url: str | None = None
    command: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    auth_ref: str | None = None


class SkillRef(BaseModel):
    id: str
    path: str | None = None
    uri: str | None = None
    version: str | None = None
    inject_layer: Literal["system", "developer", "task"] = "developer"


class ToolPolicy(BaseModel):
    allow: list[str] = Field(default_factory=list)


class BudgetConfig(BaseModel):
    max_turns: int = 8
    max_tool_calls: int = 20
    max_time_s: int = 120
    max_retries_per_tool: int = 1
    repeat_action_threshold: int = 3


class DeliveryConfig(BaseModel):
    output_schema_id: str
    json_schema: dict[str, Any]


class HitlConfig(BaseModel):
    tool_globs: list[str] = Field(default_factory=list)
    gate: list[Literal["before_tool", "after_tool", "before_finalize"]] = Field(
        default_factory=list
    )
    require_explicit_approve: bool = True


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
    hitl: HitlConfig | None = None


class AgentState(BaseModel):
    session_id: str
    trace_id: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    turn: int = 0
    tool_calls_used: int = 0
    started_at: float
    status: Literal["running", "awaiting_human", "completed", "failed"] = "running"
    pending_tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)


class FinalResult(BaseModel):
    status: Literal["ok", "needs_human", "failed"]
    payload: Any = None
    warnings: list[str] = Field(default_factory=list)
    error: RuntimeErrorInfo | None = None
    spent: dict[str, Any] = Field(default_factory=dict)
    trace_ref: str | None = None
    checkpoint_id: str | None = None
