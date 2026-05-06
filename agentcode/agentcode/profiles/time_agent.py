from __future__ import annotations

import time
import uuid
from pathlib import Path

from ..models import DeliveryConfig, McpServerConfig, SessionConfig, SkillRef


def resolve_default_beijing_skill_path() -> str | None:
    here = Path(__file__).resolve().parent.parent
    candidate = here / "skillsets" / "common" / "beijing_time.md"
    return str(candidate) if candidate.is_file() else None


def build_beijing_time_config(
    mcp_url: str,
    *,
    skill_path: str | None = None,
    session_prefix: str = "time-agent",
) -> SessionConfig:
    now_ms = int(time.time() * 1000)
    resolved_skill = skill_path or resolve_default_beijing_skill_path()
    skills = []
    if resolved_skill:
        skills = [SkillRef(id="beijing-time", path=resolved_skill, inject_layer="developer")]

    return SessionConfig(
        session_id=f"{session_prefix}-{now_ms}",
        trace_id=str(uuid.uuid4()),
        mcp_servers=[McpServerConfig(name="time", transport="http", url=mcp_url)],
        skills=skills,
        tool_policy={"allow": ["time__*"]},
        budget={
            "max_turns": 12,
            "max_tool_calls": 12,
            "max_time_s": 120,
            "max_retries_per_tool": 1,
            "repeat_action_threshold": 3,
        },
        tool_execution="sequential",
        delivery=DeliveryConfig(
            output_schema_id="answer-template-v1",
            json_schema={
                "type": "object",
                "properties": {
                    "answer": {"type": "string"},
                    "style": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["answer"],
            },
        ),
    )

