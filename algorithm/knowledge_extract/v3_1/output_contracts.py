"""Provider schemas and per-stage serialization rules, independent of domain skills."""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from .ontology_compiler import ClassSpec, RelationSpec, PropertySpec, ExtractionPlan

ENTITY_RULES = """
- 每项只能包含 type、mention、start_char、end_char、evidence.quote；禁止 name、properties 或其他键。
- mention 可以是 current chunk.text 中逐字连续的原文片段，也可以是对原文的忠实归纳（例如由多条记录归纳出的规则名）。
- start_char/end_char 是 chunk-relative、左闭右开区间；mention 无法逐字定位时填 0，系统会按 evidence.quote 兜底。
- 数值、单位、标量属性值、字段标签和修饰词不是独立实体；同一 type + 完全相同 mention 每个 chunk 只输出一次。
- 仍要给出 evidence.quote 作为来源（优先逐字原文片段）；确实无法逐字对应时给出最相关的原文片段，不要输出空实体。"""
PROPERTY_RULES = """
- entity_id 只能来自 known entities；properties 只能包含该实体 type 在 ontology 中声明且当前缺失的稳定属性标识。
- 不得输出 name/type/relation，不得把属性值、同义词、修饰语或短语变成实体。
- 原文中能定位到值的属性，必须从原文提取真实值，不能错误地输出空字符串。
- 标识、名称、编号、日期、数值、单位等字面属性必须能在 current chunk 中定位；禁止臆造流水号或默认值。
- 描述、定义、范围、类别等语义属性可以忠实归纳，但不得加入证据之外的新事实。
- 如果某个字符串属性确实在原文中为空/缺失/无法确定，允许保留该属性并输出空字符串 ""，不要删除该属性。
- 不要用 null 代替字符串属性；空字符串 "" 是允许的空值表达。
- 同一个实体的 updates 中，一个属性只能出现一次；如果属性已有值，不要重复输出。
- evidence.quote 必须是 current chunk.text 中逐字连续、足以支撑本次全部属性更新的原文。"""
RELATION_PAIR_RULES = """
- 每项只能包含 pair_id、label、evidence.quote；label 只能是该 pair 的 relation_id 或 NONE。
- NONE 表示当前 chunk 没有足够证据，NONE 不产生关系。
- pair_id 必须来自候选 pair；禁止输出新端点、新 pair 或其他关系。
- evidence.quote 必须是 current chunk.text 中逐字连续的关系证据；没有证据时选择 NONE。"""


def build_output_contract(schema: dict[str, Any], *, entity_task: bool,
                          task: str, paired_relations: bool) -> dict[str, Any]:
    if not schema or schema.get("type") != "object":
        raise ValueError("A concrete object output schema is required")
    if entity_task:
        rules = ENTITY_RULES
    elif task == "property":
        rules = PROPERTY_RULES
    elif paired_relations:
        rules = RELATION_PAIR_RULES
    else:
        rules = "- 关系端点只能引用已知实体，关系必须符合本体签名；evidence.quote 尽量逐字来自当前 chunk。"
    empty = {key: [] for key, value in schema.get("properties", {}).items()
             if value.get("type") == "array"}
    return {
        "version": "v3.1-1",
        "status": "ready",
        "success_schema": deepcopy(schema),
        "empty_result": empty,
        "constraints": "只输出符合 success_schema 的 JSON object；无结果时使用 empty_result。\n" + rules,
    }


def compile_output_schemas(
    class_specs: dict[str, ClassSpec],
    allowed_types: frozenset[str],
    relation_specs: dict[str, tuple[RelationSpec, ...]],
) -> dict[str, dict[str, Any]]:
    """Build the provider-neutral schemas used by both extraction passes."""
    concrete_types = sorted(
        class_id
        for class_id in allowed_types
        if class_id in class_specs and not class_specs[class_id].is_abstract
    )
    in_scope_relations = sorted(
        {
            spec.relation_id
            for specs in relation_specs.values()
            for spec in specs
            if spec.source in allowed_types and spec.target in allowed_types
        }
    )
    # Keep the provider-facing schema deliberately small.  DeepSeek's strict
    # structured-output dialect rejects several JSON Schema keywords (such as
    # minLength), so non-empty/grounding checks belong to the deterministic
    # candidate normalizer instead of the schema.
    evidence_schema = {
        "type": "object",
        "properties": {"quote": {"type": "string"}},
        "required": ["quote"],
        "additionalProperties": False,
    }
    entity_item = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": concrete_types},
            "mention": {"type": "string"},
            "start_char": {"type": "integer"},
            "end_char": {"type": "integer"},
            "evidence": evidence_schema,
        },
        "required": ["type", "mention", "start_char", "end_char", "evidence"],
        "additionalProperties": False,
    }
    relation_item = {
        "type": "object",
        "properties": {
            "from_id": {"type": "string", "pattern": r"^entity-\d+$"},
            "rel_type": {"type": "string", "enum": in_scope_relations},
            "to_id": {"type": "string", "pattern": r"^entity-\d+$"},
            "evidence": evidence_schema,
        },
        "required": ["from_id", "rel_type", "to_id", "evidence"],
        "additionalProperties": False,
    }
    return {
        "entity_candidates": {
            "type": "object",
            "properties": {"entities": {"type": "array", "items": entity_item}},
            "required": ["entities"],
            "additionalProperties": False,
        },
        "relation_candidates": {
            "type": "object",
            "properties": {"relations": {"type": "array", "items": relation_item}},
            "required": ["relations"],
            "additionalProperties": False,
        },
    }



def output_schema_for_types(
    plan: ExtractionPlan,
    schema_name: str,
    type_ids: Iterable[str],
) -> dict[str, Any]:
    schema = deepcopy(plan.output_schemas[schema_name])
    allowed = set(type_ids)
    if schema_name == "entity_candidates":
        type_schema = schema["properties"]["entities"]["items"]["properties"]["type"]
        type_schema["enum"] = [item for item in type_schema["enum"] if item in allowed]
    else:
        relation_schema = schema["properties"]["relations"]["items"]["properties"]["rel_type"]
        relation_schema["enum"] = sorted(
            {
                spec.relation_id
                for specs in plan.relation_specs.values()
                for spec in specs
                if spec.source in allowed and spec.target in allowed
            }
        )
    return schema


def relation_pair_decision_schema(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Strict provider schema for classifying a pre-enumerated pair batch."""
    pair_ids = [str(pair["pair_id"]) for pair in pairs]
    labels = sorted({str(pair["relation_id"]) for pair in pairs} | {"NONE"})
    decision = {
        "type": "object",
        "properties": {
            "pair_id": {"type": "string", "enum": pair_ids},
            "label": {"type": "string", "enum": labels},
            "evidence": {
                "type": "object",
                "properties": {"quote": {"type": "string"}},
                "required": ["quote"],
                "additionalProperties": False,
            },
        },
        "required": ["pair_id", "label", "evidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"decisions": {"type": "array", "items": decision}},
        "required": ["decisions"],
        "additionalProperties": False,
    }


def _property_schema(data_type: str) -> dict[str, Any]:
    normalized = re.sub(r"\s+", "", data_type or "string").casefold().rstrip("?")
    is_array = normalized.endswith("[]") or normalized.startswith(("list<", "set<"))
    if is_array:
        return {"type": "array", "items": {"type": "string"}, "minItems": 1, "uniqueItems": True}
    if any(token in normalized for token in ("bool", "boolean")):
        return {"type": "boolean"}
    if any(token in normalized for token in ("int", "integer", "long")):
        return {"type": "integer"}
    if any(token in normalized for token in ("float", "double", "decimal", "number")):
        return {"type": "number"}
    return {"type": "string"}


def property_update_schema(entities: list[Any], allowed: dict[str, dict[str, PropertySpec]]) -> dict[str, Any]:
    property_specs: dict[str, PropertySpec] = {}
    for specs in allowed.values():
        property_specs.update(specs)
    return {
        "type": "object",
        "properties": {
            "updates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "entity_id": {
                            "type": "string",
                            "enum": [str(entity.candidate_id) for entity in entities],
                        },
                        "properties": {
                            "type": "object",
                            "properties": {
                                prop_id: _property_schema(prop.data_type)
                                for prop_id, prop in sorted(property_specs.items())
                            },
                            "minProperties": 1,
                            "additionalProperties": False,
                        },
                        "evidence": {
                            "type": "object",
                            "properties": {"quote": {"type": "string", "minLength": 1}},
                            "required": ["quote"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["entity_id", "properties", "evidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["updates"],
        "additionalProperties": False,
    }
