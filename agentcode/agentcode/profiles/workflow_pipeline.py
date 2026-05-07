"""业务流水线模板：问题定义 → 方案规划（方案模型）→ 组件匹配（按方案步骤逐步调用）。

问题分解阶段默认不包含在对外路由中；需要时可自行扩展 SessionConfig。

方案规划：输入问题模型，输出 ``solution_model``（含 ``steps``）。组件匹配：每次请求携带一个 ``step``，
由编排层对方案模型中每个 step 循环调用 ``/match``。"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

from ..models import BudgetConfig, DeliveryConfig, McpServerConfig, SessionConfig, SkillRef


def _workflow_skill_root() -> Path:
    return Path(__file__).resolve().parent.parent / "skillsets" / "workflow"


def resolve_default_define_skill_path() -> str | None:
    p = _workflow_skill_root() / "problem_define.md"
    return str(p) if p.is_file() else None


def resolve_default_plan_skill_path() -> str | None:
    p = _workflow_skill_root() / "solution_model.md"
    return str(p) if p.is_file() else None


def resolve_default_match_skill_path() -> str | None:
    p = _workflow_skill_root() / "component_match.md"
    return str(p) if p.is_file() else None


def solution_step_json_schema() -> dict[str, Any]:
    """方案模型中单步结构（与组件匹配接口 ``step`` 一致）。"""
    return {
        "type": "object",
        "properties": {
            "objective": {"type": "string"},
            "inputs": {"type": "array", "items": {"type": "string"}},
            "outputs": {"type": "array", "items": {"type": "string"}},
            "constraints": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["objective", "inputs", "outputs", "constraints"],
    }


def problem_model_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "problem_model": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "占位；业务侧替换为完整问题模型 schema"},
                },
                "required": ["summary"],
            }
        },
        "required": ["problem_model"],
    }


def solution_model_json_schema() -> dict[str, Any]:
    """方案规划阶段交付：方案模型 + 步骤列表（每步可被 /match 逐条消费）。"""
    return {
        "type": "object",
        "properties": {
            "solution_model": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "items": solution_step_json_schema(),
                    },
                },
                "required": ["steps"],
            }
        },
        "required": ["solution_model"],
    }


def component_match_json_schema() -> dict[str, Any]:
    """单步组件匹配结果（占位）；业务可改为组件 id 列表、得分等。"""
    return {
        "type": "object",
        "properties": {
            "component_match": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "matches": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
                "required": ["summary", "matches"],
            }
        },
        "required": ["component_match"],
    }


def _budget_from_env() -> BudgetConfig:
    llm_timeout = int((os.getenv("AGENT_TEMPLATE_TIMEOUT_SECONDS") or os.getenv("LLM_TIMEOUT") or "60"))
    wall_s = max(120, llm_timeout)
    return BudgetConfig(
        max_turns=int(os.getenv("AGENT_WORKFLOW_MAX_TURNS") or "16"),
        max_tool_calls=int(os.getenv("AGENT_WORKFLOW_MAX_TOOL_CALLS") or "24"),
        max_time_s=int(os.getenv("AGENT_WORKFLOW_MAX_TIME_S") or str(wall_s)),
        max_retries_per_tool=int(os.getenv("AGENT_WORKFLOW_MAX_RETRIES_PER_TOOL") or "1"),
        repeat_action_threshold=int(os.getenv("AGENT_WORKFLOW_REPEAT_THRESHOLD") or "3"),
    )


def _optional_workflow_mcp() -> list[McpServerConfig]:
    url = (os.getenv("AGENT_WORKFLOW_MCP_URL") or "").strip()
    if not url:
        return []
    name = (os.getenv("AGENT_WORKFLOW_MCP_SERVER_NAME") or "workflow").strip() or "workflow"
    return [McpServerConfig(name=name, transport="http", url=url)]


def _tool_allowlist() -> list[str]:
    raw = (os.getenv("AGENT_WORKFLOW_TOOL_ALLOW") or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _base_session_kwargs() -> dict[str, Any]:
    return {
        "model": (os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or None),
        "mcp_servers": _optional_workflow_mcp(),
        "tool_policy": {"allow": _tool_allowlist()},
        "budget": _budget_from_env(),
        "tool_execution": "sequential",
    }


def build_define_session_config(
    *,
    session_id: str,
    trace_id: str,
    skill_path: str | None = None,
) -> SessionConfig:
    resolved = skill_path or (os.getenv("AGENT_DEFINE_SKILL_PATH") or "").strip() or resolve_default_define_skill_path()
    skills: list[SkillRef] = []
    if resolved:
        skills.append(SkillRef(id="workflow-define", path=resolved, inject_layer="developer"))
    return SessionConfig(
        session_id=session_id,
        trace_id=trace_id,
        skills=skills,
        delivery=DeliveryConfig(
            output_schema_id="workflow-stage-define-v1",
            json_schema=problem_model_json_schema(),
        ),
        **_base_session_kwargs(),
    )


def build_plan_session_config(
    *,
    session_id: str,
    trace_id: str,
    skill_path: str | None = None,
) -> SessionConfig:
    resolved = skill_path or (os.getenv("AGENT_PLAN_SKILL_PATH") or "").strip() or resolve_default_plan_skill_path()
    skills: list[SkillRef] = []
    if resolved:
        skills.append(SkillRef(id="workflow-plan", path=resolved, inject_layer="developer"))
    return SessionConfig(
        session_id=session_id,
        trace_id=trace_id,
        skills=skills,
        delivery=DeliveryConfig(
            output_schema_id="workflow-stage-plan-v1",
            json_schema=solution_model_json_schema(),
        ),
        **_base_session_kwargs(),
    )


def build_match_session_config(
    *,
    session_id: str,
    trace_id: str,
    skill_path: str | None = None,
) -> SessionConfig:
    resolved = skill_path or (os.getenv("AGENT_MATCH_SKILL_PATH") or "").strip() or resolve_default_match_skill_path()
    skills: list[SkillRef] = []
    if resolved:
        skills.append(SkillRef(id="workflow-match", path=resolved, inject_layer="developer"))
    return SessionConfig(
        session_id=session_id,
        trace_id=trace_id,
        skills=skills,
        delivery=DeliveryConfig(
            output_schema_id="workflow-stage-match-v1",
            json_schema=component_match_json_schema(),
        ),
        **_base_session_kwargs(),
    )


def new_workflow_ids(prefix: str) -> tuple[str, str]:
    now_ms = int(time.time() * 1000)
    return f"{prefix}-{now_ms}", str(uuid.uuid4())
