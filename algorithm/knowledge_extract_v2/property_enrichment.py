"""Evidence-gated closed-world property completion for accepted entities.

This pass deliberately runs after entity resolution.  The LLM can update only
missing ontology-declared properties on a fixed candidate-id enum; it has no
schema surface for creating entities or relations.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .candidate_generation import generate_structured
from .chunk_context import ChunkContextManager, ExtractionChunk
from .graph_utils import canonical_name
from .ontology_compiler import ExtractionPlan, PropertySpec
from .prompt_router import DocumentRoute, build_chat_messages

_EMPTY = (None, [])
_LITERAL_PROPERTY = re.compile(
    r"(?:id|identifier|code|number|no|date|time|value|unit|编号|代码|日期|时间|数值|值|单位|名称|标题|姓名|地址|电话)",
    re.IGNORECASE,
)
_SEMANTIC_PROPERTY = re.compile(
    r"(?:description|definition|purpose|scope|type|category|status|sector|responsib|"
    r"描述|定义|目的|范围|类型|类别|状态|行业|责任|职责|约束|程度|语言)",
    re.IGNORECASE,
)
_CLASSIFICATION_NAME = re.compile(r"(?:classification|分类).*(?:name|名称)", re.IGNORECASE)
_IDENTIFIER = re.compile(r"(?:identifier|unique.?id|唯一标识)", re.IGNORECASE)
_AUTHORITATIVE_SOURCES = {
    "markdown_structure",
    "document_form",
    "terminology_definition",
    "standard_semantics",
    "technical_semantics",
    "content_element",
    "normative_reference",
    "document_anchor",
}


def _warn(
    warnings: list[dict[str, Any]] | None,
    code: str,
    message: str,
    path: str,
    value: Any = None,
) -> None:
    if warnings is None:
        return
    row: dict[str, Any] = {"code": code, "message": message, "path": path}
    if value is not None:
        row["value"] = value
    warnings.append(row)


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


def _declared_properties(plan: ExtractionPlan, entity: Any) -> dict[str, PropertySpec]:
    spec = plan.class_specs.get(str(getattr(entity, "canonical_type", "") or ""))
    if spec is None:
        return {}
    existing = {canonical_name(key) for key in (getattr(entity, "properties", None) or {})}
    return {
        prop.identifier: prop
        for prop in spec.properties
        if canonical_name(prop.identifier) not in existing
    }


def _schema(entities: list[Any], allowed: dict[str, dict[str, PropertySpec]]) -> dict[str, Any]:
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


def _catalog_row(entity: Any, allowed: dict[str, PropertySpec]) -> dict[str, Any]:
    return {
        "id": entity.candidate_id,
        "type": entity.canonical_type,
        "name": entity.name,
        "properties": dict(entity.properties),
        "missing_properties": {
            prop_id: {
                "display_name": prop.display_name,
                "data_type": prop.data_type,
                "comment": prop.comment,
            }
            for prop_id, prop in allowed.items()
        },
    }


def _entity_anchor_chunk(entity: Any, manager: ChunkContextManager) -> str | None:
    for evidence in getattr(entity, "evidence", ()) or ():
        for block_id in getattr(evidence, "source_block_ids", ()) or ():
            if manager.has_chunk(str(block_id)):
                return str(block_id)
    return None


def _normalized_literal(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _literal_supported(value: Any, text: str) -> bool:
    if isinstance(value, list):
        return bool(value) and all(_normalized_literal(item) in _normalized_literal(text) for item in value)
    return bool(_normalized_literal(value)) and _normalized_literal(value) in _normalized_literal(text)


def _may_be_semantic(prop: PropertySpec) -> bool:
    symbols = f"{prop.identifier} {prop.display_name} {prop.comment}"
    return bool(_SEMANTIC_PROPERTY.search(symbols) or _CLASSIFICATION_NAME.search(symbols))


def _requires_literal(prop: PropertySpec) -> bool:
    symbols = f"{prop.identifier} {prop.display_name}"
    return bool(_LITERAL_PROPERTY.search(symbols)) and not _may_be_semantic(prop)


def _valid_update(
    *,
    entity: Any,
    prop: PropertySpec,
    value: Any,
    quote: str,
    chunk: ExtractionChunk,
) -> bool:
    if value is None or (isinstance(value, list) and not value):
        return False
    if canonical_name(prop.identifier) in {
        canonical_name(key) for key in (getattr(entity, "properties", None) or {})
    }:
        return False
    # 允许字符串属性显式置为空字符串，用于表示“原文无值/缺失”，不删除属性。
    if value == "":
        return bool(quote and chunk.text)
    if _IDENTIFIER.search(f"{prop.identifier} {prop.display_name}") and not _literal_supported(value, quote):
        return False
    if _requires_literal(prop) and not _literal_supported(value, quote):
        return False
    # The quote was already located in this exact chunk.  Semantic properties
    # may be a conservative paraphrase/classification of that quote.
    return bool(quote and chunk.text)


def _batches(values: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(values), max(1, size)):
        yield values[index : index + max(1, size)]


def enrich_entity_properties(
    pool: Any,
    manager: ChunkContextManager,
    plan: ExtractionPlan,
    llm: Any,
    *,
    route: DocumentRoute,
    attempts: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    call_metrics: list[dict[str, Any]] | None = None,
    use_scheduler: bool = False,
    local_previous: int = 1,
    local_next: int = 1,
    same_section: bool = True,
    max_attempts: int = 3,
    retry_base_seconds: float = 0.05,
    batch_size: int = 12,
) -> dict[str, int]:
    """Fill properties without changing the resolved entity/relation universe."""
    grouped: dict[str, list[Any]] = defaultdict(list)
    allowed_by_id: dict[str, dict[str, PropertySpec]] = {}
    for entity in pool.accepted_entities():
        # Deterministic extractors own the complete property set for the
        # entities they create.  Letting the LLM fill their remaining schema
        # fields re-introduces cross-entity leakage and turns optional fields
        # into false positives under a complete golden set.
        if str(getattr(entity, "source", "") or "") in _AUTHORITATIVE_SOURCES:
            continue
        declared = _declared_properties(plan, entity)
        anchor = _entity_anchor_chunk(entity, manager)
        if not declared or anchor is None:
            continue
        grouped[anchor].append(entity)
        allowed_by_id[str(entity.candidate_id)] = declared

    applied = rejected = calls = 0
    for chunk_id in manager.chunk_ids:
        candidates = grouped.get(chunk_id, [])
        if not candidates:
            continue
        chunk = manager.get_chunk(chunk_id)
        neighbors = [
            item
            for item in manager.local_context(
                chunk_id,
                previous=local_previous,
                next=local_next,
                same_section=same_section,
            )
            if item.chunk_id != chunk_id
        ]
        local_context = json.dumps(
            [item.to_dict() for item in neighbors],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        for batch_index, batch in enumerate(_batches(candidates, batch_size), start=1):
            batch_allowed = {
                str(entity.candidate_id): allowed_by_id[str(entity.candidate_id)]
                for entity in batch
            }
            output_schema = _schema(batch, batch_allowed)
            messages = build_chat_messages(
                task="property",
                plan=plan,
                route=route,
                chunk=chunk,
                local_context=local_context,
                known_entities=[
                    _catalog_row(entity, batch_allowed[str(entity.candidate_id)])
                    for entity in batch
                ],
                output_schema=output_schema,
            )
            parsed = generate_structured(
                llm,
                messages,
                output_schema,
                use_scheduler=use_scheduler,
                attempts=attempts,
                pass_name="property",
                chunk_id=f"{chunk_id}:batch-{batch_index}",
                max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds,
                call_metrics=call_metrics,
            )
            calls += 1
            if parsed is None:
                continue
            entities_by_id = {str(entity.candidate_id): entity for entity in batch}
            seen_ids: set[str] = set()
            for update_index, update in enumerate(parsed.get("updates") or []):
                entity_id = str(update.get("entity_id") or "")
                entity = entities_by_id.get(entity_id)
                path = f"property.{chunk_id}.batch-{batch_index}.updates[{update_index}]"
                if entity is None or entity_id in seen_ids:
                    rejected += 1
                    _warn(warnings, "PROPERTY_UPDATE_ENTITY_INVALID", "属性更新实体不在固定批次中或重复", path, entity_id)
                    continue
                seen_ids.add(entity_id)
                quote = str((update.get("evidence") or {}).get("quote") or "")
                if manager.find_span(chunk_id, quote) is None:
                    rejected += 1
                    _warn(warnings, "PROPERTY_EVIDENCE_INVALID", "属性证据无法在当前 chunk 逐字定位", path, quote)
                    continue
                accepted_values: dict[str, Any] = {}
                for prop_id, value in dict(update.get("properties") or {}).items():
                    prop = batch_allowed[entity_id].get(prop_id)
                    if prop is None or not _valid_update(
                        entity=entity,
                        prop=prop,
                        value=value,
                        quote=quote,
                        chunk=chunk,
                    ):
                        rejected += 1
                        _warn(
                            warnings,
                            "PROPERTY_UPDATE_UNSUPPORTED",
                            "属性未声明、已有值或缺少足够字面/语义证据，已拒绝",
                            f"{path}.properties.{prop_id}",
                            value,
                        )
                        continue
                    accepted_values[prop_id] = value
                if accepted_values:
                    entity.properties.update(accepted_values)
                    applied += len(accepted_values)
    return {"calls": calls, "applied": applied, "rejected": rejected}


__all__ = ["enrich_entity_properties"]
