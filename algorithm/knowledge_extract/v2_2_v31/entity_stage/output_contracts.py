"""Provider schemas and per-stage serialization rules, independent of domain skills."""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from .ontology_compiler import ClassSpec, PropertySpec, ExtractionPlan

VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "supported": {"type": "string", "enum": ["yes", "no", "uncertain"]},
                    "reason": {"type": "string"},
                },
                "required": ["item_id", "supported", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

def verification_schema(items: list[dict[str, Any]]) -> dict[str, Any]:
    """The same concrete contract is sent to the model and local validator."""
    ids = [item["item_id"] for item in items]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("Verification item IDs must be nonempty and unique")
    schema = deepcopy(VERIFICATION_SCHEMA)
    verdicts = schema["properties"]["verdicts"]
    verdicts.update(minItems=len(ids), maxItems=len(ids))
    verdicts["items"]["properties"]["item_id"]["enum"] = ids
    return schema


def validate_verification_items(payload: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    expected = {item["item_id"] for item in items}
    ids = [item["item_id"] for item in payload.get("verdicts", [])]
    return [] if set(ids) == expected and len(ids) == len(expected) else ["verdicts 必须逐项且仅一次覆盖所有 item_id"]


ENTITY_NAME_RULES = """- mention 将直接作为最终实体 name：原文有明确名称时，必须逐字保留完整名称及内部空格、大小写、标点，不自行改写、缩写或概括。
- 显式对象声明中的实例标签用作该实例的 mention；其 name 等业务属性按本体另填，不用属性值替换实例标签。普通叙述中的整句动作或状态不因字面出现就成为专名。
- 当前提供片段明确说明同一实体的全称和简称时，优先选择原文中实际出现的完整全称；不得凭常识扩写简称，也不得把不同实体仅凭名称相似视为同一实体。
- 仅当原文没有明确名称时，才允许依据实际证据忠实归纳 mention；不得用概括名称替换已有的原文名称。"""

ENTITY_RULES = """
- 每项包含 type、mention、properties、evidence；禁止 name 或其他键。
- 同时提取当前片段明确支持的本体属性；无证据时 properties 返回 {}，不得猜测。
- 输出本轮全部唯一实体后立即结束；不得为同一实体换一种属性措辞或引文反复输出。
- 每个实体对象独立闭合后换行；不能在同一个对象里反复写 type、mention、properties、evidence 来表示多个实体。
- properties 的键必须使用本体属性标识（目录箭头右侧），不能使用显示名称或原文表头；只使用当前 type 声明的属性。
- 每个属性的支持引文放在 evidence.property_quotes 的同名键（字符串数组）；引文必须来自 documents 原文。
- 即使只有一条引文，也写成 {"属性标识": ["原文引文"]}，不得把数组写成字符串。
- 不输出字符偏移；程序根据 evidence.quote 定位来源，优先给出足以区分重复出现位置的原文上下文。
- evidence.quote 必须为可定位的原文片段；归纳 mention 不影响引文必须忠实原文。
- evidence.quote 优先选能定位主体的短连续引文；各属性分别给出必要引文，不重复整份表单或长段正文。
""" + ENTITY_NAME_RULES
PROPERTY_RULES = """
- 按 known_entities 中的精确 type + name 逐行返回属性变更提案；不得输出 entity_id、candidate_id 或 updates。
- 每个唯一 type + name 必须返回一次，name 必须逐字保留候选阶段选定的名称（含合法归纳名称），不得因合并而缩写、扩写、概括或改写；结合全部相关 documents 的证据补全属性，没有可补属性时 properties 返回 {}。
- properties 只含本轮新增、修改或冲突消解的属性；省略属性不表示删除，已核验旧值由程序保留。相同键的来源用于综合事实，不做别名或模糊归并。
- 合并属性时保留不同适用条件和各自证据，不用一个切片的结论覆盖另一个切片；无法消解的冲突不得擅自选值或拼造事实。
- properties 只能包含该 type 在 ontology 中声明的属性；不得加入其他类型的属性。
- 标识、名称、编号、日期、数值、单位等字面属性必须有原文依据；不得臆造流水号或默认值。
- 描述、定义、范围、类别等语义属性可以忠实归纳，但不得加入证据之外的新事实。
- 缺失、无法确定或无变更的属性优先省略；兼容的空字符串不代表删除旧值，不使用 schema 未允许的 null。
- 非空属性提案必须携带 evidence；每个属性的全部支持引文放入 evidence.property_quotes 的同名键（字符串数组），不得用实体名代替属性证据。无变更时 properties={}，不要求引文。"""
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
            or set(item.get("required", [])) != (expected_fields - {"properties"} if entity_task else expected_fields - {"evidence"})
            or schema.get("additionalProperties") is not False
            or item.get("additionalProperties") is not False):
        raise ValueError(f"Output schema does not match {task} stage")
    if entity_task:
        rules = ENTITY_RULES
    elif task in {"property", "repair"}:
        rules = PROPERTY_RULES
    else:
        raise ValueError(f"Unsupported extraction task: {task}")
    return {
        "format": "jsonl",
        "success_schema": deepcopy(item),
        **({"empty_result": "{}"} if entity_task else {}),
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
    """One delta proposal per exact key; no-change rows need no invented evidence."""
    types = {entity["type"] for entity in entities}
    if len(types) != 1:
        raise ValueError("Property batches require exactly one entity type")
    specs = {prop.identifier: prop for prop in plan.class_specs[next(iter(types))].properties}
    if all('requested_properties' in entity for entity in entities):
        requested = {key for entity in entities for key in entity['requested_properties']}
        specs = {key: prop for key, prop in specs.items() if key in requested}
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
                         "additionalProperties": False},
        },
        "required": ["type", "name", "properties"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {"entities": {"type": "array", "items": item}},
            "required": ["entities"], "additionalProperties": False}


def validate_property_proposals(payload):
    """Require source quotes for every proposed value, not for unchanged keys."""
    errors = []
    for index, row in enumerate(payload.get('entities', [])):
        quotes = (row.get('evidence') or {}).get('property_quotes') or {}
        for prop, value in row.get('properties', {}).items():
            if value in (None, '', []):
                continue
            support = quotes.get(prop) or [(row.get('evidence') or {}).get('quote', '')]
            if any(not q.strip() for q in support):
                errors.append(f'entities[{index}].evidence.property_quotes.{prop}: support quotes required')
    return errors


def validate_candidate_properties(parsed, plan, *, chunk=None):
    """Cross-type property constraints run inside the generation retry loop."""
    from .structured_output import validate_json_schema
    errors = []
    for index, row in enumerate(parsed.get("entities", [])):
        name = (row.get('mention') or '').strip()
        if not name:
            errors.append(f'$.entities[{index}].mention: must be non-empty')
        if chunk is not None:
            from .chunk_context import ChunkContextManager
            from .source_windows import source_parts
            sources = source_parts(chunk)
            quote = row.get('evidence', {}).get('quote', '')
            if not any(ChunkContextManager([source]).find_span(source.chunk_id, quote)
                       or ChunkContextManager([source]).find_span(source.chunk_id, name) for source in sources):
                errors.append(f'$.entities[{index}].evidence: neither quote nor mention has a source')
        if row.get("type") not in plan.class_specs:
            continue
        schema = property_entity_schema([{"type": row["type"], "name": row.get("mention", "")}], plan)
        row_errors = validate_json_schema({"entities": [{
            "type": row["type"], "name": row.get("mention", ""),
            "properties": row.get("properties", {}), "evidence": row["evidence"],
        }]}, schema)
        errors.extend(error.replace("$.entities[0]", f"$.entities[{index}]") for error in row_errors)
    return errors
