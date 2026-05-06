from __future__ import annotations

from typing import Any, Callable

from .models import SkillRef


class SkillLoader:
    def __init__(self, resource_resolver: Callable[[str], str] | None = None) -> None:
        self._resource_resolver = resource_resolver

    def resolve(self, refs: list[SkillRef]) -> dict[str, list[str]]:
        layers = {"system": [], "developer": [], "task": []}
        for ref in refs:
            text = self._load_skill_body(ref)
            layers[ref.inject_layer].append(f"[skill:{ref.id}] {text}")
        return layers

    def _load_skill_body(self, ref: SkillRef) -> str:
        if ref.path:
            with open(ref.path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        if ref.uri:
            if self._resource_resolver is None:
                raise ValueError(f"Skill uri requires resolver: {ref.uri}")
            return self._resource_resolver(ref.uri).strip()
        raise ValueError(f"SkillRef requires path or uri: {ref.id}")
