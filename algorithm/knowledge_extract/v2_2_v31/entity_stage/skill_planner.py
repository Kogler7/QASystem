"""Request-scoped selection snapshots and deterministic stage compilation."""
from __future__ import annotations

from copy import deepcopy
import threading
from typing import Any

from .skill_registry import SkillConfigurationError, SkillRegistry, STAGES


def compose_domain_policy(route: dict[str, Any], stages: tuple[str, ...],
                          planner: SkillAssemblyPlanner | None = None, *, semantic_only=False) -> dict[str, Any]:
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
                if semantic_only and block['section'] not in {'all', 'semantics'}:
                    continue
                key = (name, block["version"], block["section"])
                if key in blocks and blocks[key] != block:
                    raise SkillConfigurationError("Conflicting rule section in request snapshot")
                blocks[key] = deepcopy(block)
    return {"rule_blocks": list(blocks.values())}


class SkillAssemblyPlanner:
    """Select once before workers; compile all stages from the frozen registry.

    Create a new instance for each extraction request. The registry snapshots
    files at construction; chunks and batches only receive copies of policies.
    """

    def __init__(self, registry: SkillRegistry | None = None):
        self.registry = registry if registry is not None else SkillRegistry()
        self._lock = threading.Lock()
        self._route: dict[str, Any] | None = None
        self._policies: dict[str, dict[str, Any]] = {}
        self._audit: list[dict[str, Any]] = []
        self._selection_started = False

    def select_for_request(self, *, route, ontology, plan, chunks, llm, skill_refs=None,
                           **generation_options) -> dict[str, Any]:
        """Freeze one semantic or explicitly bound selection before any workers."""
        from .skill_selector import select_skills
        with self._lock:
            if self._route is not None or self._selection_started:
                raise SkillConfigurationError("Skill planner already belongs to an extraction request")
            self._selection_started = True
        if skill_refs is not None:
            self.registry.resolve_refs(skill_refs)
            decision = {"status": "selected" if skill_refs else "no_match", "source": "configuration",
                        "skill_refs": list(skill_refs), "reason": "Explicit request binding"}
        elif not chunks or not plan.allowed_types:
            decision = {"status": "no_match", "source": "empty_input", "skill_refs": [],
                        "reason": "No extraction input or target types"}
        else:
            decision = select_skills(registry=self.registry, ontology=ontology, plan=plan,
                                     chunks=chunks, llm=llm, **generation_options)
        from .skill_selector import SELECTION_VERSION
        decision.update(selector_version=SELECTION_VERSION, catalog_sha256=self.registry.snapshot_sha256)
        self.prepare(route, skill_refs=decision["skill_refs"], selection=decision)
        return deepcopy(decision)

    @property
    def has_selected_skills(self) -> bool:
        return bool(self._audit and self._audit[0].get("read_skills"))

    def prepare(self, route: dict[str, Any], *, skill_refs=None, selection=None) -> None:
        with self._lock:
            if self._route is not None:
                if route != self._route:
                    raise SkillConfigurationError("Cannot change the request-scoped skill route")
                if skill_refs is not None:
                    names = self.registry.resolve_refs(skill_refs)
                    previous = [entry['skill_id'] for entry in self._audit[0]['read_skills']]
                    if names != previous:
                        raise SkillConfigurationError("Cannot change the request-scoped skill selection")
                return
            names = self.registry.resolve_refs(skill_refs) if skill_refs is not None else None
            policies = {}
            stage_selections = {}
            loaded = {}
            for stage in sorted(STAGES):
                catalog = (self.registry.matching_catalog(stage, route["kind"]) if names is None else
                           [entry for entry in self.registry.catalog(stage) if entry["skill_id"] in names])
                selections = [{"skill_id": entry["skill_id"], "sections": entry["sections"]}
                              for entry in catalog]
                profiles = self.registry.render(selections, stage)
                policies[stage] = {
                    "profile_version": "request-skills-v2", "route": deepcopy(route),
                    "default_profiles": list(profiles), "profiles": profiles, "scope": "whole_request",
                }
                stage_selections[stage] = selections
                for name, profile in profiles.items():
                    loaded[name] = {"skill_id": name, "version": profile["skill_version"],
                                    "sha256": profile["skill_sha256"]}
            self._policies = policies
            self._audit = [{"route": deepcopy(route), "read_skills": list(loaded.values()),
                            "stages": stage_selections}]
            if selection is not None:
                self._audit[0]["selection"] = deepcopy(selection)
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
