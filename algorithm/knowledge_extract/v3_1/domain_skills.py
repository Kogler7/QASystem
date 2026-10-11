"""Standalone prompt preview; pipelines reuse a request-scoped compiler."""
from __future__ import annotations

from typing import Any

from .skill_planner import SkillAssemblyPlanner

def select_domain_policy(*, kind: str, route: dict[str, Any], task: str) -> dict[str, Any]:
    if route["kind"] != kind:
        raise ValueError("Skill kind must match the document route")
    return SkillAssemblyPlanner().assemble({"domain_policy": {"route": route}}, stage=task)["domain_policy"]
