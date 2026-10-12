"""Request-scoped snapshots of user-maintained skills."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any

import yaml

STAGES = frozenset({"entity", "property", "repair"})
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SECTION = re.compile(r"^## rules:(all|entity|property|repair)[ \t]*$", re.MULTILINE)


class SkillConfigurationError(ValueError):
    """Invalid or unreadable skill configuration; never an empty extraction."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    version: str
    stages: tuple[str, ...]
    route_hints: tuple[str, ...]
    content: str
    sha256: str
    sections: tuple[tuple[str, str], ...]

    def available_sections(self, stage: str) -> dict[str, str]:
        if stage not in self.stages:
            return {}
        return {name: body for name, body in self.sections if name in {"all", stage}}


class SkillRegistry:
    """Load once per request, so file edits cannot mix revisions within a run."""

    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory) if directory is not None else Path(__file__).with_name("skills")
        root = self.directory.resolve()
        self._skills: dict[str, Skill] = {}
        if not root.exists():
            return  # An absent skill library is the system-only path.
        if not root.is_dir():
            raise SkillConfigurationError(f"Skill directory is not a directory: {root}")
        for path in sorted(root.glob("*/[Ss][Kk][Ii][Ll][Ll].[Mm][Dd]")):
            if path.parent.name.startswith("."):
                continue
            if not path.resolve().is_relative_to(root):
                raise SkillConfigurationError(f"Skill escapes configured directory: {path.name}")
            try:
                raw = path.read_bytes()
                if len(raw) > 128_000:
                    raise ValueError("skill exceeds 128 KB")
                content = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
                match = re.match(r"\A---\s*\n(.*?)\n---[ \t]*(?:\n|$)", content, re.DOTALL)
                if not match:
                    raise ValueError("YAML frontmatter is required")
                meta = yaml.safe_load(match[1])
                if not isinstance(meta, dict):
                    raise ValueError("frontmatter must be an object")
                name, description = meta.get("name"), meta.get("description")
                if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
                    raise ValueError("invalid skill name")
                if not isinstance(description, str) or not description.strip():
                    raise ValueError("description is required")
                stages, hints = meta.get("stages"), meta.get("route_hints", [])
                if not isinstance(stages, list) or not stages or any(s not in STAGES for s in stages):
                    raise ValueError("stages must list supported extraction stages")
                if not isinstance(hints, list) or any(not isinstance(h, str) for h in hints):
                    raise ValueError("route_hints must be a string array")
                version = meta.get("version")
                if not isinstance(version, str) or not version.strip():
                    raise ValueError("version must be a nonempty quoted string")
                body = content[match.end():]
                headings = list(_SECTION.finditer(body))
                sections = []
                for index, heading in enumerate(headings):
                    end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
                    rule = body[heading.end():end].strip()
                    if not rule or heading[1] in dict(sections):
                        raise ValueError("rule sections must be nonempty and unique")
                    sections.append((heading[1], rule))
                if not sections or any(not ({"all", stage} & dict(sections).keys()) for stage in stages):
                    raise ValueError("each supported stage needs an applicable rule section")
                if name in self._skills:
                    raise ValueError(f"duplicate skill name: {name}")
                self._skills[name] = Skill(name, description.strip(), version, tuple(stages), tuple(hints),
                                           content, hashlib.sha256(raw).hexdigest(), tuple(sections))
            except (OSError, UnicodeError, ValueError, TypeError, yaml.YAMLError) as exc:
                raise SkillConfigurationError(f"Invalid skill {path}: {exc}") from exc

    def catalog(self, stage: str) -> list[dict[str, Any]]:
        return [{"skill_id": s.name, "description": s.description, "version": s.version,
                 "sections": list(s.available_sections(stage)), "route_hints": list(s.route_hints)}
                for s in self._skills.values() if s.available_sections(stage)]

    def matching_catalog(self, stage: str, kind: str) -> list[dict[str, Any]]:
        """Only the program-selected document family is eligible, including custom IDs."""
        if kind in {"generic", "simple"}:
            return []
        return [entry for entry in self.catalog(stage)
                if kind == entry["skill_id"] or kind in entry["route_hints"]]

    def read_skill(self, skill_id: str, stage: str) -> dict[str, Any]:
        skill = self._skills.get(skill_id)
        if skill is None or not skill.available_sections(stage):
            raise SkillConfigurationError(f"Unknown or inapplicable skill: {skill_id}")
        return {"skill_id": skill.name, "version": skill.version, "sha256": skill.sha256,
                "content": skill.content, "available_sections": list(skill.available_sections(stage))}

    def render(self, selections: list[dict[str, Any]], stage: str) -> dict[str, Any]:
        profiles: dict[str, Any] = {}
        for selection in selections:
            if not isinstance(selection, dict) or set(selection) != {"skill_id", "sections"}:
                raise SkillConfigurationError("A selection must contain only skill_id and sections")
            name, sections = selection["skill_id"], selection["sections"]
            if not isinstance(name, str):
                raise SkillConfigurationError("skill_id must be a string")
            loaded = self.read_skill(name, stage)
            available = self._skills[name].available_sections(stage)
            if (name in profiles or not isinstance(sections, list) or not sections
                    or any(not isinstance(s, str) or s not in available for s in sections)
                    or len(set(sections)) != len(sections)):
                raise SkillConfigurationError(f"Invalid or duplicate sections for {name}")
            profiles[name] = {"extraction_rules": "\n\n".join(available[s] for s in sections),
                              "skill_version": loaded["version"], "skill_sha256": loaded["sha256"],
                              "sections": list(sections)}
        return profiles
