"""Deterministic compatibility selection from external, user-owned Markdown skills.

The public harness uses the model planner. This path keeps direct low-level
prompt previews and offline algorithm tests independent of model calls.
"""
from __future__ import annotations

from typing import Any

from .skill_registry import SkillRegistry


def select_domain_policy(*, kind: str, route: dict[str, Any], task: str,
                         record_table: bool, official_form: bool) -> dict[str, Any]:
    registry = SkillRegistry()
    hints = {kind}
    if record_table:
        hints.add("record_table")
    if official_form:
        hints.add("official_form")
    selections = [{"skill_id": entry["skill_id"], "sections": entry["sections"]}
                  for entry in registry.catalog(task) if hints.intersection(entry["route_hints"])]
    profiles = registry.render(selections, task)
    return {"profile_version": "skill-files-v1", "route": route,
            "default_profiles": list(profiles), "profiles": profiles, "scope": "current_chunk"}
