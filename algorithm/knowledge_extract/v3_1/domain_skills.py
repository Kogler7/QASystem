"""Deterministic compatibility selection from external, user-owned Markdown skills.

The public harness uses the model planner. This path keeps direct low-level
prompt previews and offline algorithm tests independent of model calls.
"""
from __future__ import annotations

from typing import Any

from .skill_registry import SkillRegistry


def select_domain_policy(*, kind: str, route: dict[str, Any], task: str) -> dict[str, Any]:
    registry = SkillRegistry()
    selections = [{"skill_id": entry["skill_id"], "sections": entry["sections"]}
                  for entry in registry.matching_catalog(task, kind)]
    profiles = registry.render(selections, task)
    return {"profile_version": "skill-files-v1", "route": route,
            "default_profiles": list(profiles), "profiles": profiles, "scope": "current_chunk"}
