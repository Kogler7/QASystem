"""Phase 1 Step 2：Ontology Compiler + Target Scope Resolver + Schema QA.

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md：

- §7 Ontology Compiler：把运行时 PlantUML 本体编译为 ``ExtractionPlan``；
- §8 FeatureSpec：只做确定性推导，本体没有的元数据一律 ``None``，不猜测；
- §9 Target Scope Resolver：单类 / 多类 / 父类含子类 / 全文，未知内容策略；
- §13 Schema QA：能报告、不编造、不崩溃（AC-9），strict / permissive 两种编译模式。

Step 2 范围：class hierarchy、properties、relations、target expansion、schema QA。
按 §37 Step 2 约定，本阶段不改 LLM 流程；本模块不依赖 openai，可独立加载测试。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .output_contracts import compile_output_schemas
from ._logging import get_step_logger
from .graph_utils import canonical_name, coerce_non_empty_name
from .ontology_schema import OntologyClass, OntologyProperty, OntologySchema


logger = get_step_logger(__name__)


_ENUM_KEYWORDS = ("枚举", "enum")
_NEGATION_MARKERS = ("不是", "不要", "禁止", "不得", "不应", "不可", "不允许", "不含", "无", "非")


def _normalized_type(data_type: str) -> str:
    return re.sub(r"\s+", "", data_type or "").casefold().rstrip("?")


def _is_array_type(normalized: str) -> bool:
    return normalized.endswith("[]") or normalized.startswith("list<") or normalized.startswith("set<")


@dataclass(frozen=True)
class SchemaWarning:
    """Schema QA 检查结果（§13），与既有告警码风格一致。"""

    code: str
    message: str
    path: str
    severity: str = "warning"
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": self.severity,
            "value": self.value,
        }


@dataclass(frozen=True)
class PropertySpec:
    identifier: str
    display_name: str
    data_type: str
    source_class: str
    comment: str = ""
    inherited: bool = False
    # §8：本体未声明时保持 None，不自行猜测。
    required: bool | None = None
    multi_value: bool | None = None
    context_inferable: bool | None = None


@dataclass(frozen=True)
class ClassSpec:
    class_id: str
    display_name: str
    stereotypes: tuple[str, ...] = ()
    parents: tuple[str, ...] = ()
    children: tuple[str, ...] = ()
    properties: tuple[PropertySpec, ...] = ()
    identity_properties: tuple[str, ...] = ()
    is_abstract: bool = False
    description: str = ""


@dataclass(frozen=True)
class RelationSpec:
    relation_id: str
    source: str
    target: str
    source_cardinality: str = ""
    target_cardinality: str = ""
    display_label: str = ""

    @property
    def signature(self) -> str:
        return f"{self.source} -[{self.relation_id}]-> {self.target}"


@dataclass(frozen=True)
class IdentitySpec:
    class_id: str
    properties: tuple[str, ...]
    inherited: bool = False


@dataclass(frozen=True)
class FeatureSpec:
    feature_id: str
    owner_type: str
    kind: str  # intrinsic | relation | context
    identifier: str
    datatype: str | None = None
    required: bool | None = None
    multi_value: bool | None = None
    context_inferable: bool | None = None
    lexical_patterns: tuple[str, ...] = ()
    structural_patterns: tuple[str, ...] = ()
    relation_signature: str | None = None
    source: str = "ontology"


@dataclass(frozen=True)
class SchemaPartition:
    partition_id: str
    class_ids: tuple[str, ...]
    relation_signatures: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "partition_id": self.partition_id,
            "class_ids": list(self.class_ids),
            "relation_signatures": list(self.relation_signatures),
        }


@dataclass(frozen=True)
class TargetScope:
    """§9：目标类型展开结果。"""

    requested_types: tuple[str, ...]
    expanded_types: frozenset[str]
    include_subclasses: bool
    unknown_content_policy: str = "ignore"

    def allows(self, class_id: Any) -> bool:
        return coerce_non_empty_name(class_id) in self.expanded_types

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_types": list(self.requested_types),
            "expanded_types": sorted(self.expanded_types),
            "include_subclasses": self.include_subclasses,
            "unknown_content_policy": self.unknown_content_policy,
        }


@dataclass(frozen=True)
class ExtractionPlan:
    """§7.2：可执行的抽取计划。"""

    ontology_id: str
    allowed_types: frozenset[str]
    class_specs: dict[str, ClassSpec]
    relation_specs: dict[str, tuple[RelationSpec, ...]]
    identity_specs: dict[str, IdentitySpec]
    target_scope: TargetScope
    feature_specs: dict[str, tuple[FeatureSpec, ...]]
    output_schemas: dict[str, dict[str, Any]]
    schema_partitions: tuple[SchemaPartition, ...]
    schema_warnings: tuple[SchemaWarning, ...] = ()

    def resolve_relation(self, identifier: Any) -> tuple[RelationSpec, ...]:
        """按稳定标识（大小写不敏感）解析关系签名。"""
        return self.relation_specs.get(canonical_name(identifier), ())

    def allows(self, class_id: Any) -> bool:
        """目标范围是否允许指定类型（AC-2）。"""
        return self.target_scope.allows(class_id)

    def relevant_types(self, text: str) -> frozenset[str]:
        """Lexical cues cannot rule out a type; only caller scope is binding."""
        return self.allowed_types

    def to_dict(self) -> dict[str, Any]:
        return {
            "ontology_id": self.ontology_id,
            "allowed_types": sorted(self.allowed_types),
            "target_scope": self.target_scope.to_dict(),
            "classes": len(self.class_specs),
            "relations": sum(len(specs) for specs in self.relation_specs.values()),
            "relation_ids": sorted(self.relation_specs),
            "output_schemas": sorted(self.output_schemas),
            "schema_partitions": [partition.to_dict() for partition in self.schema_partitions],
            "warnings": [warning.to_dict() for warning in self.schema_warnings],
        }


class TargetScopeResolver:
    """§9：解析目标类型范围，支持单类 / 多类 / 父类含子类 / 全文。"""

    def resolve(
        self,
        schema: OntologySchema,
        requested_types: Iterable[str] | None = None,
        *,
        include_subclasses: bool = True,
        unknown_content_policy: str = "ignore",
        warnings: list[SchemaWarning] | None = None,
    ) -> TargetScope:
        policy = (unknown_content_policy or "ignore").strip().casefold()
        if policy not in {"ignore", "retain_unclassified"}:
            raise ValueError(
                f"unknown_content_policy 仅支持 ignore / retain_unclassified，收到 {unknown_content_policy!r}"
            )
        warning_sink = warnings if warnings is not None else []
        requested = tuple(
            dict.fromkeys(
                coerce_non_empty_name(item)
                for item in (requested_types or [])
                if coerce_non_empty_name(item)
            )
        )
        expanded: set[str] = set()
        for index, requested_type in enumerate(requested):
            ontology_class = schema.resolve_class(requested_type)
            if ontology_class is None:
                warning_sink.append(
                    SchemaWarning(
                        "UNKNOWN_TARGET_TYPE",
                        f"目标类型 {requested_type!r} 不在本体中",
                        f"target_types[{index}]",
                        "warning",
                        requested_type,
                    )
                )
                continue
            expanded.add(ontology_class.identifier)
            if include_subclasses:
                expanded.update(schema.descendants(ontology_class.identifier))
        if not requested:
            # 全文覆盖：未指定目标类型时允许本体全部已声明类型。
            expanded = {ontology_class.identifier for ontology_class in schema.classes}
        if policy == "retain_unclassified":
            element = schema.resolve_class("Element")
            if element is None:
                warning_sink.append(
                    SchemaWarning(
                        "UNCLASSIFIED_RETENTION_UNAVAILABLE",
                        "retain_unclassified 需要本体声明通用 Element 类，当前本体未声明，回退为 ignore",
                        "unknown_content_policy",
                        "warning",
                        policy,
                    )
                )
                policy = "ignore"
            else:
                expanded.add(element.identifier)
        return TargetScope(
            requested_types=requested,
            expanded_types=frozenset(expanded),
            include_subclasses=include_subclasses,
            unknown_content_policy=policy,
        )


def _comment_references(comment: str, other: OntologyProperty) -> bool:
    """注释与另一属性的展示名/稳定标识完全一致时视为语义交叉（避免子串误报）。"""
    comment_key = canonical_name(comment)
    if other.display_name and comment_key == canonical_name(other.display_name):
        return True
    return bool(other.identifier and comment_key == canonical_name(other.identifier))


def _enum_description_qa(warnings: list[SchemaWarning], ontology_class: OntologyClass, prop: OntologyProperty) -> None:
    normalized = _normalized_type(prop.data_type)
    comment = (prop.comment or "").strip()
    comment_lower = comment.casefold()
    type_is_enum = "enum" in normalized or "枚举" in normalized
    has_enum_keyword = any(keyword in comment_lower for keyword in _ENUM_KEYWORDS)
    path = f"classes.{ontology_class.identifier}.properties.{prop.identifier}"
    if type_is_enum and not comment:
        warnings.append(
            SchemaWarning(
                "INVALID_ENUM_DESCRIPTION",
                f"枚举属性 {prop.identifier!r} 未描述允许值",
                path,
                "warning",
                prop.data_type,
            )
        )
    elif has_enum_keyword and not type_is_enum and not _is_array_type(normalized):
        warnings.append(
            SchemaWarning(
                "INVALID_ENUM_DESCRIPTION",
                (
                    f"属性 {prop.identifier!r} 仅在注释中描述枚举语义，"
                    f"建议显式声明 enum 类型或数组值域（当前 {prop.data_type}）"
                ),
                path,
                "warning",
                prop.data_type,
            )
        )


def _annotation_qa(warnings: list[SchemaWarning], ontology_class: OntologyClass, prop: OntologyProperty) -> None:
    comment = (prop.comment or "").strip()
    if not comment:
        return
    path = f"classes.{ontology_class.identifier}.properties.{prop.identifier}.comment"
    for other in ontology_class.properties:
        if other is prop:
            continue
        if other.identifier == prop.identifier and other.display_name == prop.display_name:
            continue
        if _comment_references(comment, other):
            warnings.append(
                SchemaWarning(
                    "ANNOTATION_CONFLICT",
                    (
                        f"属性 {prop.identifier!r} 的注释与属性 {other.identifier!r} 的"
                        f"名称/标识语义交叉"
                    ),
                    path,
                    "warning",
                    {"property": prop.identifier, "referenced": other.identifier, "comment": comment},
                )
            )
    for marker in _NEGATION_MARKERS:
        if marker in comment and prop.display_name and prop.display_name in comment:
            warnings.append(
                SchemaWarning(
                    "ANNOTATION_CONFLICT",
                    (
                        f"属性 {prop.identifier!r} 的注释包含否定表达 {marker!r}，"
                        f"与属性名 {prop.display_name!r} 语义冲突"
                    ),
                    path,
                    "warning",
                    {"property": prop.identifier, "marker": marker, "comment": comment},
                )
            )
            break


def run_schema_qa(schema: OntologySchema, *, mode: str = "strict") -> list[SchemaWarning]:
    """§13 Schema QA：能报告、不编造、不崩溃（AC-9）。"""
    strict = (mode or "strict").strip().casefold() == "strict"
    unresolved_severity = "error" if strict else "warning"
    warnings: list[SchemaWarning] = []

    # 类重复定义
    seen_identifiers: dict[str, int] = {}
    for index, ontology_class in enumerate(schema.classes):
        key = canonical_name(ontology_class.identifier)
        if key in seen_identifiers:
            warnings.append(
                SchemaWarning(
                    "DUPLICATE_CLASS",
                    f"类 {ontology_class.identifier!r} 重复定义",
                    f"classes[{index}].identifier",
                    "error",
                    ontology_class.identifier,
                )
            )
        seen_identifiers[key] = index

    for class_index, ontology_class in enumerate(schema.classes):
        # 属性名称重复但 datatype 不一致
        properties_by_identifier: dict[str, list[OntologyProperty]] = {}
        for prop in ontology_class.properties:
            properties_by_identifier.setdefault(prop.identifier, []).append(prop)
        for identifier, props in properties_by_identifier.items():
            datatypes = {_normalized_type(prop.data_type) for prop in props}
            if len(datatypes) > 1:
                warnings.append(
                    SchemaWarning(
                        "DUPLICATE_PROPERTY_DATATYPE_MISMATCH",
                        (
                            f"属性 {identifier!r} 在类 {ontology_class.identifier!r} 中重复且"
                            f"数据类型不一致: {', '.join(sorted(datatypes))}"
                        ),
                        f"classes[{class_index}].properties.{identifier}",
                        "error",
                        sorted(datatypes),
                    )
                )
        # enum 描述与 annotation 语义检查
        for prop in ontology_class.properties:
            _enum_description_qa(warnings, ontology_class, prop)
            _annotation_qa(warnings, ontology_class, prop)

        # identity 缺失
        if not schema.identity_properties_for(ontology_class.identifier):
            warnings.append(
                SchemaWarning(
                    "MISSING_IDENTITY",
                    f"类 {ontology_class.identifier!r} 未声明 @identity",
                    f"classes[{class_index}].identity",
                    "warning",
                    ontology_class.identifier,
                )
            )

    # cardinality 缺失
    for index, relation in enumerate(schema.relations):
        if not relation.source_cardinality or not relation.target_cardinality:
            warnings.append(
                SchemaWarning(
                    "MISSING_CARDINALITY",
                    (
                        f"关系 {relation.identifier!r}（{relation.source} -> {relation.target}）"
                        "缺少一端或两端基数"
                    ),
                    f"relations[{index}]",
                    "warning",
                    relation.identifier,
                )
            )

    # 关系/继承端点引用未声明类型
    for entry in schema.unresolved:
        if entry.get("kind") == "relation":
            code = "UNRESOLVED_RELATION_ENDPOINT_TYPE"
            message = (
                f"关系 {entry.get('relation')!r} 的端点引用了未声明类型: "
                f"{', '.join(entry.get('unresolved_endpoints', []))}"
            )
            value = {
                "source": entry.get("source"),
                "target": entry.get("target"),
                "relation": entry.get("relation"),
                "unresolved_endpoints": entry.get("unresolved_endpoints", []),
                "line": entry.get("line"),
            }
        else:
            code = "UNRESOLVED_INHERITANCE_ENDPOINT"
            message = (
                "继承关系引用了未声明类型: "
                f"{', '.join(entry.get('unresolved_endpoints', []))}"
            )
            value = {"left": entry.get("left"), "right": entry.get("right"), "line": entry.get("line")}
        warnings.append(
            SchemaWarning(code, message, "unresolved", unresolved_severity, value)
        )

    # 继承环（防止后续步骤无限递归）
    for ontology_class in schema.classes:
        if ontology_class.identifier in schema.ancestors(ontology_class.identifier):
            warnings.append(
                SchemaWarning(
                    "INHERITANCE_CYCLE",
                    f"类 {ontology_class.identifier!r} 存在继承环",
                    f"classes_by_id[{ontology_class.identifier}].parents",
                    "error",
                    ontology_class.identifier,
                )
            )
    return warnings


def _derive_ontology_id(source: str) -> str:
    match = re.search(r"@startuml[ \t]+(?P<name>\S+)", source or "")
    if match:
        return match.group("name").strip()
    digest = hashlib.sha256((source or "").encode("utf-8")).hexdigest()[:12]
    return f"ontology-{digest}"


def _parse_required_properties(source: str, schema: OntologySchema) -> dict[str, set[str]]:
    required: dict[str, set[str]] = {}
    pattern = re.compile(r"^\s*'\s*@required\s+(?P<class>[^:]+):(?P<properties>.+)$", re.I)
    for line in (source or "").splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        ontology_class = schema.resolve_class(match.group("class").strip())
        if ontology_class is None:
            continue
        for symbol in re.split(r"[,，]", match.group("properties")):
            prop = schema.resolve_property(ontology_class.identifier, symbol.strip())
            if prop is not None:
                required.setdefault(ontology_class.identifier, set()).add(prop.identifier)
    return required


def _compile_class_specs(
    schema: OntologySchema,
    required_properties: dict[str, set[str]] | None = None,
) -> dict[str, ClassSpec]:
    required_properties = required_properties or {}
    children: dict[str, list[str]] = {}
    for identifier, parents in schema.parents.items():
        for parent in parents:
            children.setdefault(parent, []).append(identifier)

    def is_required(class_id: str, property_id: str, trail: set[str] | None = None) -> bool:
        if property_id in required_properties.get(class_id, set()):
            return True
        visited = set(trail or ())
        if class_id in visited:
            return False
        visited.add(class_id)
        return any(
            is_required(parent, property_id, visited)
            for parent in schema.parents.get(class_id, ())
        )

    specs: dict[str, ClassSpec] = {}
    for ontology_class in schema.classes:
        identifier = ontology_class.identifier
        own_properties = tuple(
            PropertySpec(
                identifier=prop.identifier,
                display_name=prop.display_name,
                data_type=prop.data_type,
                source_class=identifier,
                comment=prop.comment,
                required=True if is_required(identifier, prop.identifier) else None,
            )
            for prop in ontology_class.properties
        )
        inherited_properties: list[PropertySpec] = []
        seen_identifiers = {prop.identifier for prop in own_properties}
        # Use the parser's effective property order/override rules at every depth.
        # Walking only direct parents silently drops grandparent requirements.
        for prop in schema._properties_for(identifier):
            if prop.identifier in seen_identifiers:
                continue
            seen_identifiers.add(prop.identifier)
            owner = next(c.identifier for c in schema.classes
                         if any(p is prop for p in c.properties))
            inherited_properties.append(PropertySpec(
                identifier=prop.identifier, display_name=prop.display_name,
                data_type=prop.data_type, source_class=owner, comment=prop.comment,
                inherited=True,
                required=True if is_required(identifier, prop.identifier) else None,
            ))
        specs[identifier] = ClassSpec(
            class_id=identifier,
            display_name=ontology_class.display_name,
            stereotypes=ontology_class.stereotypes,
            parents=schema.parents.get(identifier, ()),
            children=tuple(sorted(children.get(identifier, []))),
            properties=own_properties + tuple(inherited_properties),
            identity_properties=schema.identity_properties_for(identifier),
            is_abstract=ontology_class.is_abstract,
            description=ontology_class.description,
        )
    return specs


def _compile_relation_specs(schema: OntologySchema) -> dict[str, tuple[RelationSpec, ...]]:
    specs: dict[str, list[RelationSpec]] = {}
    for relation in schema.relations:
        spec = RelationSpec(
            relation_id=relation.identifier,
            source=relation.source,
            target=relation.target,
            source_cardinality=relation.source_cardinality,
            target_cardinality=relation.target_cardinality,
            display_label=relation.identifier,
        )
        specs.setdefault(canonical_name(relation.identifier), []).append(spec)
    return {key: tuple(value) for key, value in specs.items()}


def _compile_identity_specs(schema: OntologySchema) -> dict[str, IdentitySpec]:
    specs: dict[str, IdentitySpec] = {}
    for ontology_class in schema.classes:
        identifier = ontology_class.identifier
        effective = schema.identity_properties_for(identifier)
        specs[identifier] = IdentitySpec(
            class_id=identifier,
            properties=effective,
            inherited=bool(effective) and not bool(ontology_class.identity_properties),
        )
    return specs


def _compile_feature_specs(
    class_specs: dict[str, ClassSpec],
    relation_specs: dict[str, tuple[RelationSpec, ...]],
) -> dict[str, tuple[FeatureSpec, ...]]:
    """§8：从本体确定性推导 intrinsic 与 relation 特征，缺省元数据保持 None。"""
    features: dict[str, list[FeatureSpec]] = {}
    for identifier, class_spec in class_specs.items():
        for prop in class_spec.properties:
            features.setdefault(identifier, []).append(
                FeatureSpec(
                    feature_id=f"{identifier}.{prop.identifier}",
                    owner_type=identifier,
                    kind="intrinsic",
                    identifier=prop.identifier,
                    datatype=prop.data_type,
                    required=prop.required,
                    multi_value=None,
                    context_inferable=None,
                    lexical_patterns=(),
                    structural_patterns=(),
                    relation_signature=None,
                    source="ontology",
                )
            )
        for specs in relation_specs.values():
            for spec in specs:
                if spec.source != identifier:
                    continue
                features.setdefault(identifier, []).append(
                    FeatureSpec(
                        feature_id=f"{identifier}.{spec.relation_id}",
                        owner_type=identifier,
                        kind="relation",
                        identifier=spec.relation_id,
                        datatype=None,
                        required=None,
                        multi_value=None,
                        context_inferable=None,
                        lexical_patterns=(),
                        structural_patterns=(),
                        relation_signature=spec.signature,
                        source="ontology",
                    )
                )
    return {key: tuple(value) for key, value in features.items()}


def _compile_schema_partitions(
    class_specs: dict[str, ClassSpec],
    allowed_types: frozenset[str],
    relation_specs: dict[str, tuple[RelationSpec, ...]],
) -> tuple[SchemaPartition, ...]:
    adjacency = {class_id: set() for class_id in allowed_types if class_id in class_specs}
    for class_id, spec in class_specs.items():
        if class_id not in adjacency:
            continue
        for related in (*spec.parents, *spec.children):
            if related in adjacency:
                adjacency[class_id].add(related)
                adjacency[related].add(class_id)
    scoped_relations: list[RelationSpec] = []
    for specs in relation_specs.values():
        for spec in specs:
            if spec.source in adjacency and spec.target in adjacency:
                adjacency[spec.source].add(spec.target)
                adjacency[spec.target].add(spec.source)
                scoped_relations.append(spec)

    components: list[tuple[str, ...]] = []
    unseen = set(adjacency)
    while unseen:
        start = min(unseen)
        stack = [start]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            unseen.discard(current)
            stack.extend(sorted(adjacency[current] - component, reverse=True))
        components.append(tuple(sorted(component)))
    components.sort()
    return tuple(
        SchemaPartition(
            partition_id=f"partition-{index:03d}",
            class_ids=component,
            relation_signatures=tuple(
                sorted(
                    spec.signature
                    for spec in scoped_relations
                    if spec.source in component and spec.target in component
                )
            ),
        )
        for index, component in enumerate(components)
    )
class OntologyCompiler:
    """§7 Ontology Compiler：本体 + 目标范围 → ExtractionPlan。"""

    def __init__(self, *, mode: str = "strict", unknown_content_policy: str = "ignore"):
        self.mode = (mode or "strict").strip().casefold()
        if self.mode not in {"strict", "permissive"}:
            raise ValueError(f"编译模式仅支持 strict / permissive，收到 {mode!r}")
        self.unknown_content_policy = unknown_content_policy

    def compile(
        self,
        ontology: str,
        *,
        target_types: Iterable[str] | None = None,
        include_subclasses: bool = True,
        ontology_id: str = "",
    ) -> ExtractionPlan:
        schema = OntologySchema.parse(ontology or "")
        warnings: list[SchemaWarning] = []
        scope = TargetScopeResolver().resolve(
            schema,
            target_types,
            include_subclasses=include_subclasses,
            unknown_content_policy=self.unknown_content_policy,
            warnings=warnings,
        )
        warnings.extend(run_schema_qa(schema, mode=self.mode))

        required_properties = _parse_required_properties(ontology, schema)
        class_specs = _compile_class_specs(schema, required_properties)
        relation_specs = _compile_relation_specs(schema)
        identity_specs = _compile_identity_specs(schema)
        feature_specs = _compile_feature_specs(class_specs, relation_specs)
        output_schemas = compile_output_schemas(class_specs, scope.expanded_types)
        schema_partitions = _compile_schema_partitions(class_specs, scope.expanded_types, relation_specs)
        return ExtractionPlan(
            ontology_id=ontology_id or _derive_ontology_id(ontology or ""),
            allowed_types=scope.expanded_types,
            class_specs=class_specs,
            relation_specs=relation_specs,
            identity_specs=identity_specs,
            target_scope=scope,
            feature_specs=feature_specs,
            output_schemas=output_schemas,
            schema_partitions=schema_partitions,
            schema_warnings=tuple(warnings),
        )


def compile_plan(
    ontology: str,
    *,
    target_types: Iterable[str] | None = None,
    include_subclasses: bool = True,
    ontology_id: str = "",
    mode: str = "strict",
    unknown_content_policy: str = "ignore",
) -> ExtractionPlan:
    """§37 Step 2 便捷入口：编译本体并解析目标范围。"""
    logger.info(
        "[步骤][本体编译] 输入: 本体=%d字符 target_types=%s include_subclasses=%s mode=%s",
        len(ontology or ""),
        list(target_types) if target_types is not None else "全部",
        include_subclasses,
        mode,
    )
    plan = OntologyCompiler(
        mode=mode,
        unknown_content_policy=unknown_content_policy,
    ).compile(
        ontology,
        target_types=target_types,
        include_subclasses=include_subclasses,
        ontology_id=ontology_id,
    )
    logger.info(
        "[步骤][本体编译] 输出: types=%d %s 关系族=%d schema_warnings=%d",
        len(plan.allowed_types),
        sorted(plan.allowed_types),
        len(getattr(plan, "relation_specs", {}) or {}),
        len(plan.schema_warnings),
    )
    return plan
