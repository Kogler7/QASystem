"""Provider schemas and per-stage serialization rules, independent of domain skills."""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from .ontology_compiler import ClassSpec, PropertySpec, ExtractionPlan

ENTITY_NAME_RULES = """- mention 将直接作为最终实体 name：原文有明确名称时，必须逐字保留完整名称及内部空格、大小写、标点，不自行改写、缩写或概括。
- 当前提供片段明确说明同一实体的全称和简称时，优先选择原文中实际出现的完整全称；不得凭常识扩写简称，也不得把不同实体仅凭名称相似视为同一实体。
- 仅当原文没有明确名称时，才允许依据实际证据忠实归纳 mention；不得用概括名称替换已有的原文名称。"""

ENTITY_RULES = """
- 每项包含 type、mention、properties、evidence；禁止 name 或其他键。
- 同时提取当前片段明确支持的本体属性；无证据时 properties 返回 {}，不得猜测。
- properties 的键必须使用本体属性标识（目录箭头右侧），不能使用显示名称或原文表头；只使用当前 type 声明的属性。
- 每个属性的支持引文放在 evidence.property_quotes 的同名键（字符串数组）；引文必须来自 documents 原文。
- 不输出字符偏移；程序根据 evidence.quote 定位来源，优先给出足以区分重复出现位置的原文上下文。
- 数值、单位、标量属性值、字段标签和修饰词不是独立实体；同一 type + 完全相同 mention 每个 chunk 只输出一次。
- 仍要给出 evidence.quote 作为来源（优先逐字原文片段）；确实无法逐字对应时给出最相关的原文片段，不要输出空实体。
""" + ENTITY_NAME_RULES
PROPERTY_RULES = """
- 按 known_entities 中的 type + name 补全属性，直接以 JSONL 逐行返回完整实体；不得输出 entity_id、candidate_id 或 updates。
- 每个唯一 type + name 必须返回一次，name 必须逐字保留候选阶段选定的原文名称，不得因合并而缩写、扩写、概括或改写；结合 occurrences 与全部 documents 的证据补全属性，没有可补属性时 properties 返回 {}。
- 跨切片 type 和 name 完全相同的记录由你合并为一个完整实体；同名不同 type 分别处理，不做别名或模糊名称归并；不得新增、遗漏或重命名实体。
- 合并属性时保留不同适用条件和各自证据，不用一个切片的结论覆盖另一个切片；无法消解的冲突不得擅自选值或拼造事实。
- properties 只能包含该 type 在 ontology 中声明的属性；不得加入其他类型的属性。
- 标识、名称、编号、日期、数值、单位等字面属性必须有原文依据；不得臆造流水号或默认值。
- 描述、定义、范围、类别等语义属性可以忠实归纳，但不得加入证据之外的新事实。
- 字符串属性确实缺失或无法确定时允许空字符串，不使用 null。
- evidence.quote 必须是本次提供文本中的原文；每个属性的全部支持引文放入 evidence.property_quotes 的同名键（字符串数组），不得用实体名代替属性证据。"""
def build_output_contract(schema: dict[str, Any], *, entity_task: bool,
                          task: str) -> dict[str, Any]:
    if not schema or schema.get("type") != "object":
        raise ValueError("A concrete object output schema is required")
    expected_fields = ({"type", "mention", "properties", "evidence"}
                       if entity_task else {"type", "name", "properties", "evidence"})
    root = schema.get("properties", {})
    item = root.get("entities", {}).get("items", {})
    if (set(root) != {"entities"} or root["entities"].get("type") != "array"
            or set(item.get("properties", {})) != expected_fields
            or set(item.get("required", [])) != (expected_fields - {"properties"} if entity_task else expected_fields)
            or schema.get("additionalProperties") is not False
            or item.get("additionalProperties") is not False):
        raise ValueError(f"Output schema does not match {task} stage")
    if entity_task:
        rules = ENTITY_RULES
    elif task == "property":
        rules = PROPERTY_RULES
    else:
        raise ValueError(f"Unsupported extraction task: {task}")
    return {
        "format": "jsonl",
        "success_schema": deepcopy(item),
        "empty_result": "{}",
        "constraints": ("只输出 JSONL，每行一个符合 success_schema 的实体对象；无实体候选时只输出单独一行 {}。\n"
                        if entity_task else "只输出 JSONL，每行一个符合 success_schema 的实体对象；必须覆盖所有给定的唯一 type + name，不能返回空结果。\n") + "禁止 entities 包装、外层数组、Markdown 代码块或解释文字；字符串内换行必须转义为 \\n，不得拆成多行。\n" + rules,
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
        "properties": {"quote": {"type": "string"}, "property_quotes": {
            "type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}}},
        "required": ["quote"],
        "additionalProperties": False,
    }
    entity_item = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": concrete_types},
            "mention": {"type": "string"},
            "properties": _candidate_property_schema(class_specs, concrete_types),
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
        fields = schema["properties"]["entities"]["items"]["properties"]
        type_schema = fields["type"]
        type_schema["enum"] = [item for item in type_schema["enum"] if item in allowed]
        fields["properties"] = _candidate_property_schema(plan.class_specs, type_schema["enum"])
    return schema


def _candidate_property_schema(class_specs, type_ids):
    """Expose exact identifiers to the model; per-type validation stays strict.

    A key shared by types with different data types must accept their union at
    this stage. The row's actual type is checked inside the same retry loop.
    """
    fields = {}
    for type_id in sorted(type_ids):
        for prop in class_specs[type_id].properties:
            candidate = _property_schema(prop.data_type)
            if prop.identifier not in fields:
                fields[prop.identifier] = candidate
            elif fields[prop.identifier] != candidate:
                fields[prop.identifier] = {}
    return {"type": "object", "properties": dict(sorted(fields.items())),
            "additionalProperties": False}


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
            "evidence": {"type": "object", "properties": {"quote": {"type": "string"}, "property_quotes": {
            "type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}}},
                         "required": ["quote"], "additionalProperties": False},
        },
        "required": ["type", "name", "properties", "evidence"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {"entities": {"type": "array", "items": item}},
            "required": ["entities"], "additionalProperties": False}


def validate_candidate_properties(parsed, plan):
    """Cross-type property constraints run inside the generation retry loop."""
    from .structured_output import validate_json_schema
    errors = []
    for index, row in enumerate(parsed.get("entities", [])):
        if row.get("type") not in plan.class_specs:
            continue
        schema = property_entity_schema([{"type": row["type"], "name": row.get("mention", "")}], plan)
        row_errors = validate_json_schema({"entities": [{
            "type": row["type"], "name": row.get("mention", ""),
            "properties": row.get("properties", {}), "evidence": row["evidence"],
        }]}, schema)
        errors.extend(error.replace("$.entities[0]", f"$.entities[{index}]") for error in row_errors)
    return errors
