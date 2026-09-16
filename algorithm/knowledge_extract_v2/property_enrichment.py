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

from .candidate_generation import generate_structured, verify_items_with_llm
from .chunk_context import ChunkContextManager, ExtractionChunk
from .graph_utils import canonical_name
from .ontology_compiler import ExtractionPlan, PropertySpec
from .prompt_router import DocumentRoute, _looks_like_record_table, build_chat_messages
from .semantic_evidence import semantic_support

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
        return True
    # 是否逐字定位不再决定丢弃：调用方先做语义比对，不一致时交由大模型核验。
    return True


def _batches(values: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(values), max(1, size)):
        yield values[index : index + max(1, size)]


_JSON_UNESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


def _unescape_json_like(value: str) -> str:
    """把模型输出里字面存在的 JSON 转义序列还原（\" -> "、\n -> 换行 等）。"""
    if "\\" not in value:
        return value
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            if nxt in _JSON_UNESCAPES:
                out.append(_JSON_UNESCAPES[nxt])
                index += 2
                continue
            if nxt == "u" and index + 5 < len(value):
                try:
                    out.append(chr(int(value[index + 2 : index + 6], 16)))
                    index += 6
                    continue
                except ValueError:
                    pass
        out.append(char)
        index += 1
    return "".join(out)


def _locate_record_table_quote(manager: Any, chunk_id: str, quote: str) -> str | None:
    """记录表 chunk 的证据兜底：模型常把 JSON 对象包成数组并转义。

    仅在原样定位失败时调用；返回可以在 chunk 内逐字定位的最长候选片段。
    """
    normalized = _unescape_json_like(quote).strip()
    candidates: list[str] = []
    if normalized:
        candidates.append(normalized)
    stripped = normalized.strip().strip(",")
    if stripped.startswith("["):
        candidates.append(stripped[1:].strip().strip(","))
    if stripped.endswith("]"):
        candidates.append(stripped[:-1].strip().strip(","))
    obj_start = stripped.find("{")
    if obj_start > 0:
        candidates.append(stripped[obj_start:].strip().strip(","))
    for candidate in candidates:
        if not candidate:
            continue
        if manager.find_span(chunk_id, candidate) is not None:
            return candidate
        # 记录之间可能有轻微格式差异，退化为最长可逐字定位前缀。
        for length in range(len(candidate), 23, -8):
            prefix = candidate[:length]
            if manager.find_span(chunk_id, prefix) is not None:
                return prefix
    return None


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
        if not declared:
            continue
        if anchor is None:
            if not manager.chunk_ids:
                continue
            # 没有可绑定 span 的实体用首个 chunk 作为归纳上下文。
            anchor = manager.chunk_ids[0]
        grouped[anchor].append(entity)
        allowed_by_id[str(entity.candidate_id)] = declared

    applied = rejected = calls = verifications = 0
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
            planned: list[dict[str, Any]] = []
            pending: list[dict[str, Any]] = []
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
                evidence_mode = ""
                if quote and manager.find_span(chunk_id, quote) is not None:
                    evidence_mode = "exact"
                else:
                    if quote and _looks_like_record_table(chunk):
                        normalized_quote = _locate_record_table_quote(manager, chunk_id, quote)
                        if normalized_quote is not None:
                            quote = normalized_quote
                            evidence_mode = "normalized"
                    if not evidence_mode:
                        semantic_values = [
                            str(value)
                            for value in dict(update.get("properties") or {}).values()
                        ]
                        semantic_values.append(str(getattr(entity, "name", "") or ""))
                        if quote:
                            semantic_values.append(quote)
                        if semantic_support(chunk.text, semantic_values):
                            evidence_mode = "semantic"
                    if not evidence_mode:
                        pending.append({
                            "item_id": f"{chunk_id}:batch-{batch_index}:update[{update_index}]",
                            "path": path,
                            "entity": entity,
                            "entity_id": entity_id,
                            "update": update,
                            "quote": quote,
                        })
                        continue
                planned.append({
                    "path": path,
                    "entity": entity,
                    "entity_id": entity_id,
                    "update": update,
                    "quote": quote,
                    "mode": evidence_mode,
                })

            if pending:
                verdicts = verify_items_with_llm(
                    llm,
                    chunk=chunk,
                    items=[
                        {
                            "item_id": item["item_id"],
                            "kind": "property",
                            "entity_type": str(getattr(item["entity"], "type", "") or ""),
                            "entity": str(getattr(item["entity"], "name", "") or ""),
                            "properties": dict(item["update"].get("properties") or {}),
                            "evidence_quote": item["quote"],
                        }
                        for item in pending
                    ],
                    pass_name="evidence_verification",
                    chunk_id=f"{chunk_id}:batch-{batch_index}",
                    use_scheduler=use_scheduler,
                    attempts=attempts,
                    max_attempts=min(2, max(1, max_attempts)),
                    retry_base_seconds=retry_base_seconds,
                    call_metrics=call_metrics,
                    record_table=_looks_like_record_table(chunk),
                )
                verifications += 1
                for item in pending:
                    verdict = verdicts.get(item["item_id"], "uncertain")
                    if verdict == "no":
                        rejected += 1
                        _warn(
                            warnings,
                            "PROPERTY_EVIDENCE_REJECTED",
                            "语义比对不一致且大模型核验判定不属于原文，已拒绝",
                            item["path"],
                            item["quote"][:200],
                        )
                        continue
                    code = "PROPERTY_EVIDENCE_LLM_VERIFIED" if verdict == "yes" else "PROPERTY_EVIDENCE_UNVERIFIED"
                    message = (
                        "语义比对不一致，大模型核验确认属于原文"
                        if verdict == "yes"
                        else "语义比对不一致，大模型核验不确定，保留并标记"
                    )
                    _warn(warnings, code, message, item["path"], item["quote"][:200])
                    planned.append({
                        "path": item["path"],
                        "entity": item["entity"],
                        "entity_id": item["entity_id"],
                        "update": item["update"],
                        "quote": item["quote"],
                        "mode": f"llm_{verdict}",
                    })

            for item in planned:
                entity = item["entity"]
                entity_id = item["entity_id"]
                update = item["update"]
                path = item["path"]
                quote = item["quote"]
                if item["mode"] == "normalized":
                    _warn(warnings, "PROPERTY_EVIDENCE_NORMALIZED", "属性证据经 JSON 记录表归一化后定位", path, quote[:200])
                elif item["mode"] == "semantic":
                    _warn(warnings, "PROPERTY_EVIDENCE_SEMANTIC", "属性证据未逐字定位，语义比对通过", path, quote[:200])
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
                            "属性未声明或已有值，已拒绝",
                            f"{path}.properties.{prop_id}",
                            value,
                        )
                        continue
                    if value == "" and _looks_like_record_table(chunk):
                        # 记录表里归纳出的规则没有唯一标识时，不输出空属性占位。
                        continue
                    accepted_values[prop_id] = value
                if accepted_values:
                    entity.properties.update(accepted_values)
                    applied += len(accepted_values)
    return {"calls": calls, "applied": applied, "rejected": rejected, "verifications": verifications}


__all__ = ["enrich_entity_properties"]
