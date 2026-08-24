"""PlantUML ontology parsing, normalization, and extraction validation.

The project uses a small PlantUML dialect:

* ``class "display name" as StableId`` declares a model class;
* the first stereotype remains a backwards-compatible class identifier when
  no alias is present;
* ``property name (propertyId): Type`` declares a stable property identifier;
* ``' @identity Class: prop1, prop2`` declares ordered identity fallbacks;
* class inheritance and labelled associations constrain relationship types.

All model interpretation lives in this module so prompts, validation, graph
conversion, and batch merging agree on the same identifiers.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import re
from typing import Any, Iterable

from .graph_utils import canonical_name, coerce_non_empty_name


_CLASS_HEADER = re.compile(
    r'^\s*(?:abstract\s+)?class\s+(?:"(?P<quoted>[^"]+)"|(?P<name>[^\s<{]+))'
    r'(?:\s+as\s+(?:"(?P<quoted_alias>[^"]+)"|(?P<alias>[^\s<{]+)))?(?P<tail>.*)$',
    re.IGNORECASE,
)
_STEREOTYPE = re.compile(r'<<\s*(.*?)\s*>>')
_PROPERTY = re.compile(
    r'^\s*[+\-#~]?\s*(?P<name>.+?)\s*(?::\s*(?P<data_type>[^/]+?))?'
    r'(?:\s*//\s*(?P<comment>.*))?\s*$'
)
_PROPERTY_ID = re.compile(r'^(?P<display>.*?)\s*\((?P<identifier>[^()]+)\)\s*$')
_IDENTITY = re.compile(
    r"^\s*'\s*@identity\s+(?P<class_name>\"[^\"]+\"|[^:]+?)\s*:\s*(?P<properties>.+?)\s*$",
    re.IGNORECASE,
)
_TYPE_REFERENCE = r'(?P<{name}>"[^"]+"|[^\s"]+)'
_INHERITANCE = re.compile(
    r'^\s*' + _TYPE_REFERENCE.format(name="left")
    + r'\s*(?P<arrow>--\|>|<\|--)\s*'
    + _TYPE_REFERENCE.format(name="right") + r'\s*$'
)
_RELATION = re.compile(
    r'^\s*' + _TYPE_REFERENCE.format(name="source")
    + r'(?:\s+"(?P<source_cardinality>[^"]*)")?\s*'
    + r'(?P<arrow><\.{2}|\.{2}>|<--|-->|o--|--o|\*--|--\*|--|\.\.)\s*'
    + r'(?:"(?P<target_cardinality>[^"]*)"\s+)?'
    + _TYPE_REFERENCE.format(name="target")
    + r'\s*:\s*(?P<label>.+?)\s*$'
)
_NUMBER = re.compile(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)')
_PLACEHOLDER_IDENTITY = re.compile(
    r"^(?:agent|unit|obj(?:ect)?|act(?:ivity)?|info(?:rmation)?|constraint|ex(?:change)?|"
    r"entity|node|item|document|clause|section)[-_]?\d+$",
    re.IGNORECASE,
)
_PLACEHOLDER_WORDS = {"无", "待定", "未知", "null", "none", "n/a", "na", "-", "--"}
_BARE_NUMBER = re.compile(r"^\d+$|^[一二三四五六七八九十百]+$")
_BARE_ORDINAL = re.compile(
    r"^(?:第\s*[0-9一二三四五六七八九十百]+|[0-9一二三四五六七八九十百]+"
    r"(?:\.[0-9一二三四五六七八九十百]+)*|[（(][0-9一二三四五六七八九十百]+[）)])"
    r"(?:[（(][0-9一二三四五六七八九十百]+[）)]|\.[0-9一二三四五六七八九十百]+|\d+)*"
    r"[、.．:：\-—]*$"
)
_ORDINAL_PREFIX = re.compile(
    r"^(?:(?:第\s*[0-9一二三四五六七八九十百]+"
    r"|[0-9一二三四五六七八九十百]+(?:\.[0-9一二三四五六七八九十百]+)*"
    r"|[（(][0-9一二三四五六七八九十百]+[）)])\s*[、.．:：\-—、．]*)+"
)


def _is_placeholder_identity(value: Any, *, allow_bare_number: bool = False) -> bool:
    text = canonical_name(coerce_non_empty_name(value))
    if not text:
        return True
    if text.casefold() in _PLACEHOLDER_WORDS:
        return True
    if _PLACEHOLDER_IDENTITY.match(text):
        return True
    if allow_bare_number and _BARE_NUMBER.match(text):
        return True
    return False


def _strip_ordinal_prefix(value: Any) -> str:
    """Remove a leading numbering prefix (``一、``, ``1.``, ``（一）``) from text."""
    text = coerce_non_empty_name(value)
    if not text:
        return ""
    stripped = _ORDINAL_PREFIX.sub("", text).strip(" ，,。.；;")
    return stripped or text


def _unquote(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].strip()
    return value


def _key(value: Any) -> str:
    return canonical_name(_unquote(coerce_non_empty_name(value)))


def _warning(code: str, message: str, path: str, value: Any = None) -> dict[str, Any]:
    warning: dict[str, Any] = {"code": code, "message": message, "path": path}
    if value is not None:
        warning["value"] = value
    return warning


@dataclass(frozen=True)
class OntologyProperty:
    display_name: str
    identifier: str
    data_type: str
    comment: str = ""

    @property
    def symbols(self) -> set[str]:
        return {self.display_name, self.identifier}


def _parse_property_line(line: str) -> OntologyProperty | None:
    """Parse one PlantUML attribute declaration into an OntologyProperty.

    The parser accepts both the original dialect used by the validation
    dataset, ``name (identifier) // comment`` (no explicit type), and the
    documented ``name (identifier): Type // comment`` form. Missing types are
    treated as ``string`` so validation still recognizes the property.
    """
    property_match = _PROPERTY.match(line)
    if not property_match:
        return None
    raw_name = property_match.group("name").strip()
    property_id_match = _PROPERTY_ID.match(raw_name)
    display_name = raw_name
    identifier = raw_name
    if property_id_match:
        display_name = property_id_match.group("display").strip()
        identifier = property_id_match.group("identifier")
    return OntologyProperty(
        display_name=display_name,
        identifier=identifier,
        data_type=(property_match.group("data_type") or "").strip() or "string",
        comment=(property_match.group("comment") or "").strip(),
    )


@dataclass
class OntologyClass:
    display_name: str
    alias: str | None = None
    stereotypes: tuple[str, ...] = ()
    properties: list[OntologyProperty] = field(default_factory=list)
    identity_properties: tuple[str, ...] = ()

    @property
    def identifier(self) -> str:
        if self.alias:
            return self.alias
        if self.stereotypes:
            return self.stereotypes[0]
        return self.display_name

    @property
    def identity_property(self) -> str | None:
        """Compatibility accessor for callers that only support one identity."""
        return self.identity_properties[0] if self.identity_properties else None

    @property
    def symbols(self) -> set[str]:
        return {value for value in (self.display_name, self.alias, self.identifier) if value}

    def resolve_property(self, name: str) -> str:
        lookup = _key(name)
        for prop in self.properties:
            if any(_key(symbol) == lookup for symbol in prop.symbols):
                return prop.identifier
        return coerce_non_empty_name(name)


@dataclass(frozen=True)
class OntologyRelation:
    source: str
    identifier: str
    target: str
    source_cardinality: str = ""
    target_cardinality: str = ""


@dataclass
class OntologySchema:
    classes: list[OntologyClass]
    relations: list[OntologyRelation] = field(default_factory=list)
    parents: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _symbols: dict[str, OntologyClass] = field(init=False, repr=False)
    _relations: dict[str, list[OntologyRelation]] = field(init=False, repr=False)
    classes_by_id: dict[str, OntologyClass] = field(init=False)

    def __post_init__(self) -> None:
        self._symbols = {}
        self.classes_by_id = {}
        for ontology_class in self.classes:
            self.classes_by_id[ontology_class.identifier] = ontology_class
            for symbol in ontology_class.symbols:
                if _key(symbol):
                    self._symbols[_key(symbol)] = ontology_class
        self._relations = {}
        for relation in self.relations:
            self._relations.setdefault(_key(relation.identifier), []).append(relation)

    @classmethod
    def parse(cls, source: str) -> "OntologySchema":
        source = source or ""
        classes: list[OntologyClass] = []
        current: OntologyClass | None = None
        in_body = False

        for line in source.splitlines():
            header = _CLASS_HEADER.match(line)
            if header:
                display_name = (header.group("quoted") or header.group("name") or "").strip()
                stereotype_values: list[str] = []
                for stereotype_match in _STEREOTYPE.findall(header.group("tail") or ""):
                    stereotype_values.extend(
                        item.strip() for item in stereotype_match.split(",") if item.strip()
                    )
                current = OntologyClass(
                    display_name=display_name,
                    alias=(header.group("quoted_alias") or header.group("alias")),
                    stereotypes=tuple(stereotype_values),
                )
                classes.append(current)
                open_brace = line.find("{")
                close_brace = line.rfind("}")
                if open_brace != -1 and close_brace > open_brace:
                    inline_body = line[open_brace + 1 : close_brace]
                    for inline_line in inline_body.split("\\n"):
                        prop = _parse_property_line(inline_line)
                        if prop is not None:
                            current.properties.append(prop)
                in_body = "{" in line and "}" not in line
                continue

            if current is None or not in_body:
                continue
            if "}" in line:
                current = None
                in_body = False
                continue

            prop = _parse_property_line(line)
            if prop is None:
                continue
            current.properties.append(prop)

        provisional = cls(classes)
        for line in source.splitlines():
            identity_match = _IDENTITY.match(line)
            if not identity_match:
                continue
            ontology_class = provisional.resolve_class(identity_match.group("class_name"))
            if ontology_class is None:
                continue
            identities = []
            for raw_property in identity_match.group("properties").split(","):
                identifier = ontology_class.resolve_property(raw_property.strip())
                if identifier and identifier not in identities:
                    identities.append(identifier)
            ontology_class.identity_properties = tuple(identities)

        parents: dict[str, list[str]] = {}
        relations: list[OntologyRelation] = []
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("'"):
                continue

            inheritance_match = _INHERITANCE.match(line)
            if inheritance_match:
                left = provisional.resolve_class(inheritance_match.group("left"))
                right = provisional.resolve_class(inheritance_match.group("right"))
                if left is None or right is None:
                    continue
                child, parent = (
                    (left, right)
                    if inheritance_match.group("arrow") == "--|>"
                    else (right, left)
                )
                parent_ids = parents.setdefault(child.identifier, [])
                if parent.identifier not in parent_ids:
                    parent_ids.append(parent.identifier)
                continue

            relation_match = _RELATION.match(line)
            if not relation_match:
                continue
            source_class = provisional.resolve_class(relation_match.group("source"))
            target_class = provisional.resolve_class(relation_match.group("target"))
            if source_class is None or target_class is None:
                continue
            relation_name = relation_match.group("label").split("//", 1)[0].strip().strip('"')
            if not relation_name:
                continue
            reverse = relation_match.group("arrow").startswith("<")
            if reverse:
                source_class, target_class = target_class, source_class
                source_cardinality = relation_match.group("target_cardinality") or ""
                target_cardinality = relation_match.group("source_cardinality") or ""
            else:
                source_cardinality = relation_match.group("source_cardinality") or ""
                target_cardinality = relation_match.group("target_cardinality") or ""
            relations.append(
                OntologyRelation(
                    source=source_class.identifier,
                    identifier=relation_name,
                    target=target_class.identifier,
                    source_cardinality=source_cardinality,
                    target_cardinality=target_cardinality,
                )
            )

        return cls(
            classes,
            relations=relations,
            parents={identifier: tuple(values) for identifier, values in parents.items()},
        )

    def resolve_class(self, symbol: Any) -> OntologyClass | None:
        return self._symbols.get(_key(symbol))

    def resolve_relation(self, identifier: Any) -> list[OntologyRelation]:
        return list(self._relations.get(_key(identifier), []))

    def ancestors(self, class_symbol: Any) -> set[str]:
        ontology_class = self.resolve_class(class_symbol)
        if ontology_class is None:
            return set()
        visited: set[str] = set()
        pending = list(self.parents.get(ontology_class.identifier, ()))
        while pending:
            parent = pending.pop()
            if parent in visited:
                continue
            visited.add(parent)
            pending.extend(self.parents.get(parent, ()))
        return visited

    def is_assignable(self, actual: Any, expected: Any) -> bool:
        actual_class = self.resolve_class(actual)
        expected_class = self.resolve_class(expected)
        if actual_class is None or expected_class is None:
            return False
        return (
            actual_class.identifier == expected_class.identifier
            or expected_class.identifier in self.ancestors(actual_class.identifier)
        )

    def _properties_for(self, class_symbol: Any) -> list[OntologyProperty]:
        ontology_class = self.resolve_class(class_symbol)
        if ontology_class is None:
            return []
        ordered: dict[str, OntologyProperty] = {}

        def collect(identifier: str, trail: set[str]) -> None:
            if identifier in trail:
                return
            next_trail = {*trail, identifier}
            for parent in self.parents.get(identifier, ()):
                collect(parent, next_trail)
            parent_class = self.classes_by_id.get(identifier)
            if parent_class is not None:
                for prop in parent_class.properties:
                    ordered[prop.identifier] = prop

        collect(ontology_class.identifier, set())
        return list(ordered.values())

    def resolve_property(self, class_symbol: Any, property_symbol: Any) -> OntologyProperty | None:
        lookup = _key(property_symbol)
        for prop in self._properties_for(class_symbol):
            if any(_key(symbol) == lookup for symbol in prop.symbols):
                return prop
        return None

    def identity_properties_for(self, class_symbol: Any) -> tuple[str, ...]:
        ontology_class = self.resolve_class(class_symbol)
        if ontology_class is None:
            return ()
        if ontology_class.identity_properties:
            return ontology_class.identity_properties
        for parent_id in self.parents.get(ontology_class.identifier, ()):
            inherited = self.identity_properties_for(parent_id)
            if inherited:
                return inherited
        return ()

    def target_identifiers(self, target_types: Iterable[str] | None = None) -> tuple[str, ...]:
        requested = [item for item in (target_types or []) if coerce_non_empty_name(item)]
        if not requested:
            return tuple(ontology_class.identifier for ontology_class in self.classes)
        targets = [self.resolve_class(item) for item in requested]
        targets = [item for item in targets if item is not None]
        return tuple(
            ontology_class.identifier
            for ontology_class in self.classes
            if any(self.is_assignable(ontology_class.identifier, target.identifier) for target in targets)
        )

    def prompt_catalog(self, target_types: Iterable[str] | None = None) -> str:
        if not self.classes:
            return "未解析到类声明。"
        allowed = set(self.target_identifiers(target_types))
        lines: list[str] = []
        for ontology_class in self.classes:
            if ontology_class.identifier not in allowed:
                continue
            aliases = sorted(ontology_class.symbols - {ontology_class.identifier})
            line = f'- `{ontology_class.display_name}` -> type `{ontology_class.identifier}`'
            if aliases:
                line += f"（可解析名称：{', '.join(aliases)}）"
            identities = self.identity_properties_for(ontology_class.identifier)
            if identities:
                line += f"；identity 优先级：{', '.join(identities)}"
            lines.append(line)
            for prop in self._properties_for(ontology_class.identifier):
                lines.append(
                    f"  - 属性 `{prop.display_name}` -> `{prop.identifier}` ({prop.data_type})"
                )
        return "\n".join(lines) if lines else "目标类型未匹配到本体类。"

    @staticmethod
    def _coerce_value(value: Any, data_type: str) -> Any:
        if value is None:
            return None
        normalized_type = re.sub(r"\s+", "", data_type or "").casefold().rstrip("?")
        is_array = normalized_type.endswith("[]") or normalized_type.startswith("list<")
        if is_array:
            if isinstance(value, str):
                stripped = value.strip()
                if not stripped:
                    return []
                if stripped.startswith("["):
                    try:
                        parsed = json.loads(stripped)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, list):
                        return parsed
                return [stripped]
            if isinstance(value, (list, tuple, set)):
                return list(value)
            return [value]
        if normalized_type in {"float", "double", "decimal", "number"}:
            if isinstance(value, bool):
                return float(value)
            if isinstance(value, (int, float)):
                return float(value)
            match = _NUMBER.search(str(value).replace(",", ""))
            return float(match.group(0)) if match else value
        if normalized_type in {"int", "integer", "long"}:
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int):
                return value
            match = _NUMBER.search(str(value).replace(",", ""))
            return int(float(match.group(0))) if match else value
        if normalized_type in {"bool", "boolean"}:
            if isinstance(value, bool):
                return value
            lookup = canonical_name(value)
            if lookup in {"true", "1", "yes", "y", "是", "有"}:
                return True
            if lookup in {"false", "0", "no", "n", "否", "无"}:
                return False
            return value
        if normalized_type in {"str", "string", "date", "datetime", "time"}:
            return coerce_non_empty_name(value) or value
        return value

    def normalize_extraction(self, extracted: dict[str, Any]) -> dict[str, Any]:
        """Canonicalize model identifiers, identity names, and typed values."""
        normalized = deepcopy(extracted) if isinstance(extracted, dict) else {}
        entities = normalized.get("entities")
        relations = normalized.get("relations")
        if not isinstance(entities, list):
            normalized["entities"] = []
            entities = normalized["entities"]
        if not isinstance(relations, list):
            normalized["relations"] = []
            relations = normalized["relations"]

        endpoint_names: dict[tuple[str, str], str] = {}
        drop_entities: set[int] = set()
        for entity_index, entity in enumerate(entities):
            if not isinstance(entity, dict):
                continue
            original_type = entity.get("type", "")
            original_name = coerce_non_empty_name(entity.get("name"))
            ontology_class = self.resolve_class(original_type)
            if ontology_class is None:
                continue
            entity["type"] = ontology_class.identifier
            raw_properties = entity.get("properties")
            raw_properties = raw_properties if isinstance(raw_properties, dict) else {}
            properties: dict[str, Any] = {}
            for raw_name, value in raw_properties.items():
                prop = self.resolve_property(ontology_class.identifier, raw_name)
                if prop is None:
                    properties[coerce_non_empty_name(raw_name)] = value
                else:
                    properties[prop.identifier] = self._coerce_value(value, prop.data_type)
            entity["properties"] = properties

            identity_properties = self.identity_properties_for(ontology_class.identifier)
            identity_name = next(
                (
                    coerce_non_empty_name(properties.get(identity_property))
                    for identity_property in identity_properties
                    if coerce_non_empty_name(properties.get(identity_property))
                ),
                "",
            )
            property_name = coerce_non_empty_name(properties.get("name"))
            resolved_name = identity_name or original_name or property_name
            class_properties = self._properties_for(ontology_class.identifier)

            def _descriptive_candidates() -> Iterable[str]:
                """Descriptive identity candidates: real names/titles first, then
                full-text properties with numbering prefixes stripped."""
                for prop in class_properties:
                    identifier = prop.identifier.casefold()
                    if "name" not in identifier and "title" not in identifier:
                        continue
                    value = coerce_non_empty_name(properties.get(prop.identifier))
                    if value and not _is_placeholder_identity(value):
                        yield value
                for prop in class_properties:
                    identifier = prop.identifier.casefold()
                    if not any(
                        marker in identifier
                        for marker in ("text", "content", "description", "originaltext")
                    ):
                        continue
                    value = coerce_non_empty_name(properties.get(prop.identifier))
                    if not value:
                        continue
                    stripped = _strip_ordinal_prefix(value)
                    if len(stripped) >= 2 and not _is_placeholder_identity(stripped):
                        yield stripped

            candidates = list(_descriptive_candidates())
            identity_is_id = any(
                prop.identifier.casefold().endswith("id") for prop in class_properties
            )
            has_title_prop = any(
                "title" in prop.identifier.casefold() for prop in class_properties
            )
            # Bare numbers are placeholder ordinals only for unit-like ids
            # (DocumentUnit "一/二/三"); numbered structures (Section "5",
            # Clause "5.1") keep their numbering as the real identity.
            allow_bare_number = has_title_prop and any(
                "unit" in prop.casefold() or "number" in prop.casefold()
                for prop in identity_properties
            )
            if identity_is_id and candidates:
                # Id-like identities (unitId/agentId/...) carry little semantic
                # content; prefer a descriptive name/title/text value so that
                # content-equal entities match across chunks and evals.
                resolved_name = candidates[0]
            elif _is_placeholder_identity(
                resolved_name, allow_bare_number=allow_bare_number
            ) or (
                identity_is_id
                and has_title_prop
                and _is_placeholder_identity(resolved_name, allow_bare_number=True)
            ):
                # placeholder identity (agent-1 / unit-1 / 无 / 待定 / bare 序号) ->
                # fall back to the real name/title/text property when available
                for prop in class_properties:
                    identifier = prop.identifier.casefold()
                    if "name" not in identifier and "title" not in identifier:
                        continue
                    candidate = coerce_non_empty_name(properties.get(prop.identifier))
                    if candidate and not _is_placeholder_identity(candidate):
                        resolved_name = candidate
                        break
                if not resolved_name or resolved_name == identity_name:
                    for candidate in candidates:
                        resolved_name = candidate
                        break
            entity["name"] = resolved_name
            if resolved_name:
                properties["name"] = resolved_name
            if original_name:
                endpoint_names[(ontology_class.identifier, canonical_name(original_name))] = resolved_name
            if resolved_name:
                endpoint_names[(ontology_class.identifier, canonical_name(resolved_name))] = resolved_name
            # Untitled numbering fragments ("一（一）1") are not meaningful
            # structure units; drop them when no descriptive text is available.
            identifier_lower = ontology_class.identifier.casefold()
            if (
                "unit" in identifier_lower
                and _BARE_ORDINAL.match(canonical_name(resolved_name))
                and not any(
                    candidate
                    for candidate in _descriptive_candidates()
                    if len(candidate) >= 4 and candidate != resolved_name
                )
            ):
                drop_entities.add(entity_index)
        if drop_entities:
            normalized["entities"] = [
                entity
                for index, entity in enumerate(normalized.get("entities", []))
                if index not in drop_entities
            ]

        for relation in relations:
            if not isinstance(relation, dict):
                continue
            for type_field, name_field in (("from_type", "from_name"), ("to_type", "to_name")):
                ontology_class = self.resolve_class(relation.get(type_field, ""))
                if ontology_class is None:
                    continue
                relation[type_field] = ontology_class.identifier
                name = coerce_non_empty_name(relation.get(name_field))
                relation[name_field] = endpoint_names.get(
                    (ontology_class.identifier, canonical_name(name)),
                    name,
                )
            matching_relations = self.resolve_relation(relation.get("rel_type", ""))
            if matching_relations:
                relation["rel_type"] = matching_relations[0].identifier
        return normalized

    def validate_extraction(
        self,
        extracted: dict[str, Any],
        *,
        target_types: Iterable[str] | None = None,
        mode: str = "strict",
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Validate an extraction against class, property, scope, and relation rules.

        ``strict`` removes invalid items. ``permissive`` preserves them while
        still returning warnings. Missing relation endpoints are intentionally
        not rejected here because another document chunk may contain the node.
        """
        strict = mode.casefold() == "strict"
        warnings: list[dict[str, Any]] = []
        sanitized: dict[str, Any] = {"entities": [], "relations": []}
        requested_targets = [item for item in (target_types or []) if coerce_non_empty_name(item)]
        resolved_targets: list[OntologyClass] = []
        for index, target in enumerate(requested_targets):
            ontology_class = self.resolve_class(target)
            if ontology_class is None:
                warnings.append(
                    _warning(
                        "UNKNOWN_TARGET_TYPE",
                        f"目标类型 {target!r} 不在本体中",
                        f"target_types[{index}]",
                        target,
                    )
                )
            else:
                resolved_targets.append(ontology_class)

        raw_entities = extracted.get("entities", []) if isinstance(extracted, dict) else []
        if not isinstance(raw_entities, list):
            warnings.append(_warning("INVALID_ENTITIES", "entities 必须是数组", "entities"))
            raw_entities = []
        for index, raw_entity in enumerate(raw_entities):
            path = f"entities[{index}]"
            if not isinstance(raw_entity, dict):
                warnings.append(_warning("INVALID_ENTITY", "实体必须是对象", path))
                if not strict:
                    sanitized["entities"].append(raw_entity)
                continue
            entity = deepcopy(raw_entity)
            ontology_class = self.resolve_class(entity.get("type", ""))
            if ontology_class is None:
                warnings.append(
                    _warning(
                        "UNKNOWN_ENTITY_TYPE",
                        f"实体类型 {entity.get('type')!r} 不在本体中",
                        f"{path}.type",
                        entity.get("type"),
                    )
                )
                if not strict:
                    sanitized["entities"].append(entity)
                continue
            entity["type"] = ontology_class.identifier
            if resolved_targets and not any(
                self.is_assignable(ontology_class.identifier, target.identifier)
                for target in resolved_targets
            ):
                warnings.append(
                    _warning(
                        "ENTITY_OUT_OF_SCOPE",
                        f"实体类型 {ontology_class.identifier} 不在目标类型及其子类范围内",
                        f"{path}.type",
                        ontology_class.identifier,
                    )
                )
                if strict:
                    continue

            raw_properties = entity.get("properties")
            if raw_properties is None:
                raw_properties = {}
            if not isinstance(raw_properties, dict):
                warnings.append(
                    _warning("INVALID_ENTITY_PROPERTIES", "properties 必须是对象", f"{path}.properties")
                )
                raw_properties = {}
            properties: dict[str, Any] = {}
            for raw_name, value in raw_properties.items():
                if raw_name == "name":
                    properties["name"] = value
                    continue
                prop = self.resolve_property(ontology_class.identifier, raw_name)
                if prop is None:
                    warnings.append(
                        _warning(
                            "UNKNOWN_PROPERTY",
                            f"属性 {raw_name!r} 未在类型 {ontology_class.identifier} 中定义",
                            f"{path}.properties.{raw_name}",
                            raw_name,
                        )
                    )
                    if not strict:
                        properties[raw_name] = value
                    continue
                properties[prop.identifier] = self._coerce_value(value, prop.data_type)
            entity["properties"] = properties
            sanitized["entities"].append(entity)

        raw_relations = extracted.get("relations", []) if isinstance(extracted, dict) else []
        if not isinstance(raw_relations, list):
            warnings.append(_warning("INVALID_RELATIONS", "relations 必须是数组", "relations"))
            raw_relations = []
        for index, raw_relation in enumerate(raw_relations):
            path = f"relations[{index}]"
            if not isinstance(raw_relation, dict):
                warnings.append(_warning("INVALID_RELATION", "关系必须是对象", path))
                if not strict:
                    sanitized["relations"].append(raw_relation)
                continue
            relation = deepcopy(raw_relation)
            source = self.resolve_class(relation.get("from_type", ""))
            target = self.resolve_class(relation.get("to_type", ""))
            endpoint_invalid = False
            if source is None:
                endpoint_invalid = True
                warnings.append(
                    _warning(
                        "UNKNOWN_RELATION_ENDPOINT_TYPE",
                        f"关系源类型 {relation.get('from_type')!r} 不在本体中",
                        f"{path}.from_type",
                        relation.get("from_type"),
                    )
                )
            else:
                relation["from_type"] = source.identifier
            if target is None:
                endpoint_invalid = True
                warnings.append(
                    _warning(
                        "UNKNOWN_RELATION_ENDPOINT_TYPE",
                        f"关系目标类型 {relation.get('to_type')!r} 不在本体中",
                        f"{path}.to_type",
                        relation.get("to_type"),
                    )
                )
            else:
                relation["to_type"] = target.identifier

            candidates = self.resolve_relation(relation.get("rel_type", ""))
            if not candidates:
                warnings.append(
                    _warning(
                        "UNKNOWN_RELATION_TYPE",
                        f"关系类型 {relation.get('rel_type')!r} 未在本体中定义",
                        f"{path}.rel_type",
                        relation.get("rel_type"),
                    )
                )
                if strict:
                    continue
            elif source is not None and target is not None:
                signature = next(
                    (
                        candidate
                        for candidate in candidates
                        if self.is_assignable(source.identifier, candidate.source)
                        and self.is_assignable(target.identifier, candidate.target)
                    ),
                    None,
                )
                if signature is None:
                    warnings.append(
                        _warning(
                            "RELATION_ENDPOINT_TYPE_MISMATCH",
                            (
                                f"关系 {relation.get('rel_type')} 的端点 "
                                f"{source.identifier} -> {target.identifier} 不符合本体签名"
                            ),
                            path,
                        )
                    )
                    if strict:
                        continue
                else:
                    relation["rel_type"] = signature.identifier

            if endpoint_invalid and strict:
                continue
            if not coerce_non_empty_name(relation.get("from_name")) or not coerce_non_empty_name(
                relation.get("to_name")
            ):
                warnings.append(
                    _warning(
                        "RELATION_ENDPOINT_NAME_MISSING",
                        "关系两端都必须提供实体 identity/name",
                        path,
                    )
                )
                if strict:
                    continue
            sanitized["relations"].append(relation)

        return sanitized, warnings
