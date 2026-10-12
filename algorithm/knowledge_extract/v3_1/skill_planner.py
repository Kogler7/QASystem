"""Deterministic, request-scoped skill compilation after V2-compatible routing."""
from __future__ import annotations

from copy import deepcopy
import threading
from typing import Any

from .skill_registry import SkillConfigurationError, SkillRegistry, STAGES


def compose_domain_policy(route: dict[str, Any], stages: tuple[str, ...],
                          planner: SkillAssemblyPlanner | None = None) -> dict[str, Any]:
    """Preserve whole rule sections; deduplicate only the same registered section.

    A preview also uses one snapshot for every stage. Runtime callers pass their
    request-scoped planner so verification and repair cannot reload changed files.
    """
    planner = planner if planner is not None else SkillAssemblyPlanner()
    blocks = {}
    for stage in stages:
        policy = planner.assemble({"task": {"operation": stage}, "ontology": {},
                                   "domain_policy": {"route": route}}, stage=stage)["domain_policy"]
        for name, profile in policy.get("profiles", {}).items():
            for block in profile["rule_blocks"]:
                key = (name, block["version"], block["section"])
                if key in blocks and blocks[key] != block:
                    raise SkillConfigurationError("Conflicting rule section in request snapshot")
                blocks[key] = deepcopy(block)
    return {"rule_blocks": list(blocks.values())}


class SkillAssemblyPlanner:
    """Compile all stage policies once; never ask a model to select skills.

    Create a new instance for each extraction request. The registry snapshots
    files at construction; chunks and batches only receive copies of policies.
    """

    def __init__(self, registry: SkillRegistry | None = None):
        self.registry = registry if registry is not None else SkillRegistry()
        self._lock = threading.Lock()
        self._route: dict[str, Any] | None = None
        self._policies: dict[str, dict[str, Any]] = {}
        self._audit: list[dict[str, Any]] = []

    def prepare(self, route: dict[str, Any]) -> None:
        with self._lock:
            if self._route is not None:
                if route != self._route:
                    raise SkillConfigurationError("Cannot change the request-scoped skill route")
                return
            policies = {}
            stage_selections = {}
            loaded = {}
            for stage in sorted(STAGES):
                selections = [{"skill_id": entry["skill_id"], "sections": entry["sections"]}
                              for entry in self.registry.matching_catalog(stage, route["kind"])]
                profiles = self.registry.render(selections, stage)
                policies[stage] = {
                    "profile_version": "deterministic-skills-v1", "route": deepcopy(route),
                    "default_profiles": list(profiles), "profiles": profiles, "scope": "current_chunk",
                }
                stage_selections[stage] = selections
                for name, profile in profiles.items():
                    loaded[name] = {"skill_id": name, "version": profile["skill_version"],
                                    "sha256": profile["skill_sha256"]}
            self._policies = policies
            self._audit = [{"route": deepcopy(route), "read_skills": list(loaded.values()),
                            "stages": stage_selections}]
            self._route = deepcopy(route)

    def assemble(self, user: dict[str, Any], *, stage: str) -> dict[str, Any]:
        """Copy the precompiled policy into this call, preserving all other data."""
        if stage not in STAGES:
            raise ValueError(f"Unsupported extraction stage: {stage}")
        self.prepare(user["domain_policy"]["route"])
        result = deepcopy(user)
        result["domain_policy"] = deepcopy(self._policies[stage])
        return result

    def audit(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._audit)
