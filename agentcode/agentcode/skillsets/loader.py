from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from ..models import SkillRef

_SKILL_TOOL_NAME = "Skill"

_SKILL_TOOL_DESCRIPTION = (
    "Load a Skill to get step-by-step instructions, domain knowledge, or workflow guidance "
    "for a specific type of task. Call this BEFORE using other tools when a relevant Skill "
    "is available for the user's request."
)


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    result: dict[str, str] = {}
    for line in m.group(1).split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


class SkillLoader:
    def __init__(
        self,
        resource_resolver: Callable[[str], str] | None = None,
        skillset_dir: str | Path | None = None,
    ) -> None:
        self._resource_resolver = resource_resolver
        if skillset_dir is None:
            skillset_dir = Path(__file__).resolve().parent
        self._skillset_dir = Path(skillset_dir)
        self._path_by_id: dict[str, str] = {}

    def discover_skills(self) -> list[SkillRef]:
        """扫描 .md → 提取 frontmatter 元数据"""
        refs: list[SkillRef] = []
        for md_path in sorted(self._skillset_dir.rglob("*.md")):
            skill_id = md_path.parent.name
            try:
                with open(md_path, "r", encoding="utf-8") as fh:
                    head = fh.read(1024)
            except Exception:
                continue
            fm = _parse_frontmatter(head)
            name = fm.get("name", skill_id)
            description = fm.get("description", "")
            inject_layer = "developer"
            if fm.get("inject_layer") in ("system", "developer", "task"):
                inject_layer = fm["inject_layer"]
            ref = SkillRef(
                id=skill_id,
                name=name,
                description=description,
                path=str(md_path),
                inject_layer=inject_layer,
            )
            refs.append(ref)
            self._path_by_id[skill_id] = str(md_path)
        return refs

    def get_skill_body(self, skill_id: str) -> str:
        path = self._path_by_id.get(skill_id)
        if not path:
            raise ValueError(f"Skill not found: {skill_id}")
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()

    def build_skill_tool_definition(self) -> dict[str, Any]:
        return {
            "name": _SKILL_TOOL_NAME,
            "description": _SKILL_TOOL_DESCRIPTION,
            "input_schema": {
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "description": "Name of the Skill to load. Available: "
                        + ", ".join(sorted(self._path_by_id.keys())),
                    }
                },
                "required": ["skill"],
            },
        }

    def build_metadata_text(self, refs: list[SkillRef]) -> str:
        if not refs:
            return ""
        lines = ["## Available Skills"]
        lines.append(
            "You have a `Skill` tool. Call `Skill` with a skill name to load "
            "step-by-step instructions before using other tools."
        )
        lines.append("")
        for r in refs:
            lines.append(f"- **{r.name}** (`{r.id}`): {r.description}")
        return "\n".join(lines)

    def resolve(self, refs: list[SkillRef]) -> dict[str, list[str]]:
        layers: dict[str, list[str]] = {"system": [], "developer": [], "task": []}
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

    @property
    def skill_tool_name(self) -> str:
        return _SKILL_TOOL_NAME
