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
    r'^\s*(?:(?P<abstract>abstract)\s+)?class\s+(?:"(?P<quoted>[^"]+)"|(?P<name>[^\s<{]+))'
    r'(?:\s+as\s+(?:"(?P<quoted_alias>[^"]+)"|(?P<alias>[^\s<{]+)))?(?P<tail>.*)$',
    re.IGNORECASE,
)
_STEREOTYPE = re.compile(r'<<\s*(.*?)\s*>>')
_PROPERTY = re.compile(
    r'^\s*[+\-#~]?\s*(?P<name>.+?)\s*:\s*(?P<data_type>[^/]+?)'
    r'(?:\s*//\s*(?P<comment>.*))?\s*$'
)
_PROPERTY_ID = re.compile(r'^(?P<display>.*?)\s*\((?P<identifier>[A-Za-z_][\w.]*)\)\s*$')
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


@dataclass
class OntologyClass:
    display_name: str
    alias: str | None = None
    stereotypes: tuple[str, ...] = ()
    properties: list[OntologyProperty] = field(default_factory=list)
    identity_properties: tuple[str, ...] = ()
    is_abstract: bool = False

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
    unresolved: list[dict[str, Any]] = field(default_factory=list)
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
                    is_abstract=bool(header.group("abstract")),
                )
                classes.append(current)
                in_body = "{" in line and "}" not in line
                continue

            if current is None or not in_body:
                continue
            if "}" in line:
                current = None
                in_body = False
                continue

            property_match = _PROPERTY.match(line)
            if not property_match:
                continue
            raw_name = property_match.group("name").strip()
            property_id_match = _PROPERTY_ID.match(raw_name)
            display_name = raw_name
            identifier = raw_name
            if property_id_match:
                display_name = property_id_match.group("display").strip()
                identifier = property_id_match.group("identifier")
            current.properties.append(
                OntologyProperty(
                    display_name=display_name,
                    identifier=identifier,
                    data_type=property_match.group("data_type").strip(),
                    comment=(property_match.group("comment") or "").strip(),
                )
            )

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
                left_symbol = inheritance_match.group("left")
                right_symbol = inheritance_match.group("right")
                left = provisional.resolve_class(left_symbol)
                right = provisional.resolve_class(right_symbol)
                if left is None or right is None:
                    provisional.unresolved.append(
                        {
                            "kind": "inheritance",
                            "line": stripped,
                            "left": _unquote(left_symbol),
                            "right": _unquote(right_symbol),
                            "unresolved_endpoints": [
                                endpoint
                                for endpoint in (_unquote(left_symbol), _unquote(right_symbol))
                                if provisional.resolve_class(endpoint) is None
                            ],
                        }
                    )
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
            source_symbol = relation_match.group("source")
            target_symbol = relation_match.group("target")
            source_class = provisional.resolve_class(source_symbol)
            target_class = provisional.resolve_class(target_symbol)
            if source_class is None or target_class is None:
                provisional.unresolved.append(
                    {
                        "kind": "relation",
                        "line": stripped,
                        "source": _unquote(source_symbol),
                        "target": _unquote(target_symbol),
                        "relation": relation_match.group("label").split("//", 1)[0].strip().strip('"'),
                        "unresolved_endpoints": [
                            endpoint
                            for endpoint in (_unquote(source_symbol), _unquote(target_symbol))
                            if provisional.resolve_class(endpoint) is None
                        ],
                    }
                )
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
            unresolved=provisional.unresolved,
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

    def descendants(self, class_symbol: Any) -> set[str]:
        """Return all declared classes that inherit from ``class_symbol``."""
        target = self.resolve_class(class_symbol)
        if target is None:
            return set()
        return {
            ontology_class.identifier
            for ontology_class in self.classes
            if target.identifier in self.ancestors(ontology_class.identifier)
        }

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

    def identity_properties_for(self, class_symbol: Any, _trail: set[str] | None = None) -> tuple[str, ...]:
        """Effective identity with inheritance, safe against inheritance cycles."""
        ontology_class = self.resolve_class(class_symbol)
        if ontology_class is None:
            return ()
        if ontology_class.identity_properties:
            return ontology_class.identity_properties
        trail = set(_trail or ())
        if ontology_class.identifier in trail:
            return ()
        next_trail = {*trail, ontology_class.identifier}
        for parent_id in self.parents.get(ontology_class.identifier, ()):
            inherited = self.identity_properties_for(parent_id, next_trail)
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
        for entity in entities:
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

            identity_name = ""
            for identity_property in self.identity_properties_for(ontology_class.identifier):
                identity_name = coerce_non_empty_name(properties.get(identity_property))
                if identity_name:
                    break
            property_name = coerce_non_empty_name(properties.get("name"))
            resolved_name = identity_name or original_name or property_name
            entity["name"] = resolved_name
            if resolved_name:
                properties["name"] = resolved_name
            if original_name:
                endpoint_names[(ontology_class.identifier, canonical_name(original_name))] = resolved_name
            if resolved_name:
                endpoint_names[(ontology_class.identifier, canonical_name(resolved_name))] = resolved_name

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
