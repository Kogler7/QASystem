"""PlantUML 本体方言的解析与抽取结果规范化。

本项目在标准 PlantUML 类图之上约定：
- 引号类名是展示名，``as Alias`` 是首选的稳定元模型类型标识；
- 为兼容现有本体，无 alias 时第一个 stereotype 可作为稳定类型标识；
- 后续 stereotype 是分类/父类型；
- 属性名末尾的 ``(PropertyId)`` 是稳定属性标识；
- 没有上述扩展标识时回退到 PlantUML 名称。

解析、标识选择和结果规范化集中在这里，避免各业务流程各自解释 PUML。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import re
from typing import Any


_CLASS_HEADER = re.compile(
    r'^\s*(?:abstract\s+)?class\s+(?:"(?P<quoted>[^"]+)"|(?P<name>[^\s<{]+))'
    r'(?:\s+as\s+(?P<alias>[^\s<{]+))?(?P<tail>.*)$',
    re.IGNORECASE,
)
_STEREOTYPE = re.compile(r'<<\s*(.*?)\s*>>')
_PROPERTY = re.compile(
    r'^\s*[+\-#~]?\s*(?P<name>.+?)\s*:\s*(?P<data_type>[^/]+?)'
    r'(?:\s*//\s*(?P<comment>.*))?\s*$'
)
_PROPERTY_ID = re.compile(r'^(?P<display>.*?)\s*\((?P<identifier>[A-Za-z_][\w.]*)\)\s*$')
_IDENTITY = re.compile(r"^\s*'\s*@identity\s+(?P<class_name>[^\s:]+)\s*:\s*(?P<property_name>[^\s]+)\s*$")


@dataclass(frozen=True)
class OntologyProperty:
    display_name: str
    identifier: str
    data_type: str
    comment: str = ""


@dataclass
class OntologyClass:
    display_name: str
    alias: str | None = None
    stereotypes: tuple[str, ...] = ()
    properties: list[OntologyProperty] = field(default_factory=list)
    identity_property: str | None = None

    @property
    def identifier(self) -> str:
        """项目本体方言的稳定类型标识。"""
        if self.alias:
            return self.alias
        if self.stereotypes:
            return self.stereotypes[0]
        return self.display_name

    @property
    def symbols(self) -> set[str]:
        return {value for value in (self.display_name, self.alias, self.identifier) if value}

    def resolve_property(self, name: str) -> str:
        key = name.strip()
        for prop in self.properties:
            if key in (prop.display_name, prop.identifier):
                return prop.identifier
        return name


@dataclass
class OntologySchema:
    classes: list[OntologyClass]
    _symbols: dict[str, OntologyClass] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._symbols = {}
        for ontology_class in self.classes:
            for symbol in ontology_class.symbols:
                self._symbols[symbol] = ontology_class

    @classmethod
    def parse(cls, source: str) -> "OntologySchema":
        classes: list[OntologyClass] = []
        current: OntologyClass | None = None
        in_body = False

        for line in (source or "").splitlines():
            header = _CLASS_HEADER.match(line)
            if header:
                display_name = (header.group("quoted") or header.group("name")).strip()
                stereotype_match = _STEREOTYPE.search(header.group("tail") or "")
                stereotypes = ()
                if stereotype_match:
                    stereotypes = tuple(
                        item.strip() for item in stereotype_match.group(1).split(",") if item.strip()
                    )
                current = OntologyClass(
                    display_name=display_name,
                    alias=header.group("alias"),
                    stereotypes=stereotypes,
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

        schema = cls(classes)
        for line in (source or "").splitlines():
            identity_match = _IDENTITY.match(line)
            if not identity_match:
                continue
            ontology_class = schema.resolve_class(identity_match.group("class_name"))
            if ontology_class is None:
                continue
            ontology_class.identity_property = ontology_class.resolve_property(identity_match.group("property_name"))
        return schema

    def resolve_class(self, symbol: str) -> OntologyClass | None:
        return self._symbols.get((symbol or "").strip())

    def prompt_catalog(self) -> str:
        if not self.classes:
            return "未解析到类声明；类型标识回退为模型输出值。"
        lines = []
        for ontology_class in self.classes:
            aliases = sorted(ontology_class.symbols - {ontology_class.identifier})
            line = f'- `{ontology_class.display_name}` -> type `{ontology_class.identifier}`'
            if aliases:
                line += f"（可解析名称：{', '.join(aliases)}）"
            lines.append(line)
            for prop in ontology_class.properties:
                if prop.display_name != prop.identifier:
                    lines.append(f"  - 属性 `{prop.display_name}` -> `{prop.identifier}`")
        return "\n".join(lines)

    @staticmethod
    def _coerce_name(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float, bool)):
            return str(value)
        return ""

    def normalize_extraction(self, extracted: dict[str, Any]) -> dict[str, Any]:
        """统一实体、属性和关系端点的标识，同时保留未知输出供后续校验。"""
        normalized = deepcopy(extracted)
        for entity in normalized.get("entities", []):
            if not isinstance(entity, dict):
                continue
            ontology_class = self.resolve_class(entity.get("type", ""))
            if ontology_class is None:
                continue
            entity["type"] = ontology_class.identifier
            properties = entity.get("properties")
            if isinstance(properties, dict):
                entity["properties"] = {
                    ontology_class.resolve_property(str(key)): value for key, value in properties.items()
                }
                properties = entity["properties"]
            else:
                properties = {}
                entity["properties"] = properties

            entity_name = self._coerce_name(entity.get("name"))
            identity_name = self._coerce_name(
                properties.get(ontology_class.identity_property) if ontology_class.identity_property else None
            )
            property_name = self._coerce_name(properties.get("name"))
            resolved_name = entity_name or identity_name or property_name
            entity["name"] = resolved_name
            if property_name or not resolved_name:
                continue
            properties["name"] = resolved_name

        for relation in normalized.get("relations", []):
            if not isinstance(relation, dict):
                continue
            for field_name in ("from_type", "to_type"):
                ontology_class = self.resolve_class(relation.get(field_name, ""))
                if ontology_class is not None:
                    relation[field_name] = ontology_class.identifier
        return normalized
