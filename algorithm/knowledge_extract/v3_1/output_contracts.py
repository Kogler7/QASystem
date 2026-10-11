"""Provider schemas and per-stage serialization rules, independent of domain skills."""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from .ontology_compiler import ClassSpec, PropertySpec, ExtractionPlan

ENTITY_RULES = """
- 每项只能包含 type、mention、evidence.quote；禁止 name、properties 或其他键。
- mention 可以是 current chunk.text 中逐字连续的原文片段，也可以是对原文的忠实归纳（例如由多条记录归纳出的规则名）。
- 不输出字符偏移；程序根据 evidence.quote 定位来源，优先给出足以区分重复出现位置的原文上下文。
- 数值、单位、标量属性值、字段标签和修饰词不是独立实体；同一 type + 完全相同 mention 每个 chunk 只输出一次。
- 仍要给出 evidence.quote 作为来源（优先逐字原文片段）；确实无法逐字对应时给出最相关的原文片段，不要输出空实体。"""
PROPERTY_RULES = """
- 按 known_entities 中的 type + name 补全属性，直接返回完整 entities；不得输出 entity_id、candidate_id 或 updates。
- 每个唯一 type + name 必须返回一次，保留原始 type 和 name；结合 occurrences 与全部 documents 的证据补全属性，没有可补属性时 properties 返回 {}。
- 跨切片 type 和 name 完全相同的记录由你合并为一个完整实体；同名不同 type 分别处理，不做别名或模糊名称归并；不得新增、遗漏或重命名实体。
- 合并属性时保留不同适用条件和各自证据，不用一个切片的结论覆盖另一个切片；无法消解的冲突不得擅自选值或拼造事实。
- properties 只能包含该 type 在 ontology 中声明的属性；不得加入其他类型的属性。
- 标识、名称、编号、日期、数值、单位等字面属性必须有原文依据；不得臆造流水号或默认值。
- 描述、定义、范围、类别等语义属性可以忠实归纳，但不得加入证据之外的新事实。
- 字符串属性确实缺失或无法确定时允许空字符串，不使用 null。
- evidence.quote 必须是本次提供文本中能支持该实体属性的原文。"""
def build_output_contract(schema: dict[str, Any], *, entity_task: bool,
                          task: str) -> dict[str, Any]:
    if not schema or schema.get("type") != "object":
        raise ValueError("A concrete object output schema is required")
    expected_fields = ({"type", "mention", "evidence"}
                       if entity_task else {"type", "name", "properties", "evidence"})
    root = schema.get("properties", {})
    item = root.get("entities", {}).get("items", {})
    if (set(root) != {"entities"} or root["entities"].get("type") != "array"
            or set(item.get("properties", {})) != expected_fields
            or set(item.get("required", [])) != expected_fields
            or schema.get("additionalProperties") is not False
            or item.get("additionalProperties") is not False):
        raise ValueError(f"Output schema does not match {task} stage")
    if entity_task:
        rules = ENTITY_RULES
    elif task == "property":
        rules = PROPERTY_RULES
    else:
        raise ValueError(f"Unsupported extraction task: {task}")
    empty = {key: [] for key, value in schema.get("properties", {}).items()
             if value.get("type") == "array"}
    return {
        "success_schema": deepcopy(schema),
        "empty_result": empty,
        "constraints": ("只输出符合 success_schema 的 JSON object；无实体候选时使用 empty_result。\n"
                        if entity_task else "只输出符合 success_schema 的 JSON object；必须覆盖所有给定的唯一 type + name，不能返回空集合。\n") + rules,
    }


def compile_output_schemas(
    class_specs: dict[str, ClassSpec],
    allowed_types: frozenset[str],
) -> dict[str, dict[str, Any]]:
    """Build the provider-neutral entity candidate schema."""
    concrete_types = sorted(
        class_id
        for class_id in allowed_types
        if class_id in class_specs and not class_specs[class_id].is_abstract
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
            "evidence": evidence_schema,
        },
        "required": ["type", "mention", "evidence"],
        "additionalProperties": False,
    }
    return {
        "entity_candidates": {
            "type": "object",
            "properties": {"entities": {"type": "array", "items": entity_item}},
            "required": ["entities"],
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
    return schema


def _property_schema(data_type: str) -> dict[str, Any]:
    normalized = re.sub(r"\s+", "", data_type or "string").casefold().rstrip("?")
    if normalized.endswith("[]"):
        return {"type": "array", "items": _property_schema(normalized[:-2])}
    if normalized.startswith(("list<", "set<")) and normalized.endswith(">"):
        return {"type": "array", "items": _property_schema(normalized.split("<", 1)[1][:-1])}
    if any(token in normalized for token in ("bool", "boolean")):
        return {"type": "boolean"}
    if any(token in normalized for token in ("int", "integer", "long")):
        return {"type": "integer"}
    if any(token in normalized for token in ("float", "double", "decimal", "number")):
        return {"type": "number"}
    return {"type": "string"}


def property_entity_schema(entities: list[dict[str, Any]], plan: ExtractionPlan) -> dict[str, Any]:
    """The model returns complete records; type/name pairing is also checked locally."""
    types = {entity["type"] for entity in entities}
    if len(types) != 1:
        raise ValueError("Property batches require exactly one entity type")
    specs = {prop.identifier: prop for prop in plan.class_specs[next(iter(types))].properties}
    item = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": sorted({e["type"] for e in entities})},
            "name": {"type": "string", "enum": sorted({e["name"] for e in entities})},
            "properties": {"type": "object", "properties": {
                key: _property_schema(prop.data_type) for key, prop in sorted(specs.items())
            }, "additionalProperties": False},
            "evidence": {"type": "object", "properties": {"quote": {"type": "string"}},
                         "required": ["quote"], "additionalProperties": False},
        },
        "required": ["type", "name", "properties", "evidence"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {"entities": {"type": "array", "items": item}},
            "required": ["entities"], "additionalProperties": False}
