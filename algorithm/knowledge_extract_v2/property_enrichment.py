"""Evidence-gated closed-world completion for accepted entities.

Identity mode runs before entity resolution and may fill only declared identity
fields.  Property mode runs after resolution and may fill only missing,
non-identity ontology fields on a frozen candidate-id enum.  Neither mode has a
schema surface for creating entities or relations.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from ._logging import get_step_logger
from .candidate_generation import generate_structured, verify_items_with_llm
from .chunk_context import ChunkContextManager, ExtractionChunk
from .evidence import Evidence, locate_quote
from .graph_utils import canonical_name
from .ontology_compiler import ExtractionPlan, PropertySpec
from .official_document_scope import management_type_ids, official_entity_types, official_role
from .prompt_router import DocumentRoute, _looks_like_record_table, build_chat_messages


logger = get_step_logger(__name__)
_EMPTY = (None, [])
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


def _declared_properties(
    plan: ExtractionPlan,
    entity: Any,
    *,
    mode: str = "property",
) -> dict[str, PropertySpec]:
    spec = plan.class_specs.get(str(getattr(entity, "canonical_type", "") or ""))
    if spec is None:
        return {}
    existing = {canonical_name(key) for key in (getattr(entity, "properties", None) or {})}
    identity_properties = {canonical_name(key) for key in spec.identity_properties}
    if mode == "identity":
        return {
            prop.identifier: prop
            for prop in spec.properties
            if canonical_name(prop.identifier) not in existing
            and canonical_name(prop.identifier) in identity_properties
        }
    if mode != "property":
        raise ValueError(f"未知属性补全模式: {mode!r}")
    return {
        prop.identifier: prop
        for prop in spec.properties
        if canonical_name(prop.identifier) not in existing
        and canonical_name(prop.identifier) not in identity_properties
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


_CATALOG_VALUE_MAX = 40
_CATALOG_MAX_PROPERTIES = 3
_CATALOG_TEXTLIKE_HINTS = (
    "body", "template", "antecedent", "consequent", "quote", "original",
    "text", "content", "正文", "模板", "前件", "后件", "内容", "原文", "摘要", "描述",
)


def _identity_properties(entity: Any) -> dict[str, Any]:
    """目录里只保留"身份型"短标量。

    entity.properties 里是上游 pass 让模型归纳出的长文本（规则正文、模板、
    前件后件、单元标题…），它们对"补哪些属性"毫无帮助，却会把单次 prompt
    从几千字符撑到两万以上（实测 property prompt 26k、单次调用 66 秒）。
    这里只留短标量，模型补值时靠 missing_properties 与 current chunk。
    """
    picked: dict[str, Any] = {}
    for key, value in dict(getattr(entity, "properties", None) or {}).items():
        if not isinstance(value, (str, int, float, bool)):
            continue
        text = str(value)
        if len(text) > _CATALOG_VALUE_MAX:
            continue
        if any(hint in f"{key}{text}" for hint in _CATALOG_TEXTLIKE_HINTS):
            continue
        picked[key] = value
        if len(picked) >= _CATALOG_MAX_PROPERTIES:
            break
    return picked


_NEIGHBOR_TEXT_MAX = 800
_NEIGHBOR_TOTAL_MAX = 2000


def _truncated_neighbor_context(neighbors: Iterable[Any]) -> list[dict[str, Any]]:
    """邻接片只作补值参考：截断文本，避免把整篇文档拼进单次 prompt。

    属性值通常落在实体所属片（current chunk）内并已原文提供；邻接片保留片首
    若干字符足以提供上下文，完整拼接会让 prompt 膨胀数倍、单次调用显著变慢。
    """
    rows: list[dict[str, Any]] = []
    budget = _NEIGHBOR_TOTAL_MAX
    for item in neighbors:
        payload = item.to_dict()
        text = str(payload.get("text") or "")
        if len(text) > _NEIGHBOR_TEXT_MAX:
            payload["text"] = f"{text[:_NEIGHBOR_TEXT_MAX]}…（共{len(text)}字符，已截断）"
        if budget <= 0:
            break
        budget -= len(str(payload.get("text") or ""))
        rows.append(payload)
    return rows


def _catalog_row(entity: Any, allowed: dict[str, PropertySpec]) -> dict[str, Any]:
    return {
        "id": entity.candidate_id,
        "type": entity.canonical_type,
        "name": entity.name,
        "properties": _identity_properties(entity),
        "missing_properties": {
            prop_id: {
                "display_name": prop.display_name,
                "data_type": prop.data_type,
            }
            for prop_id, prop in allowed.items()
        },
    }


def _entity_anchor_chunks(entity: Any, manager: ChunkContextManager) -> list[str]:
    anchors: list[str] = []
    for evidence in getattr(entity, "evidence", ()) or ():
        for block_id in getattr(evidence, "source_block_ids", ()) or ():
            block_id = str(block_id)
            if manager.has_chunk(block_id) and block_id not in anchors:
                anchors.append(block_id)
    return anchors


_CONTEXT_BOUNDARIES = "\n\r。！？!?；;"
_CONTEXT_RADIUS = 240


def _entity_context_windows(
    entity: Any,
    chunk: ExtractionChunk,
) -> list[tuple[int, int, str]]:
    """Return sentence/record windows anchored to this entity, not the whole chunk.

    A window may support several properties together.  It is intentionally not
    split into one evidence item per property.
    """
    if (chunk.metadata or {}).get("official_document_role") == "form":
        return [(0, len(chunk.text), chunk.text)]
    anchors: list[tuple[int, int]] = []
    for evidence in getattr(entity, "evidence", ()) or ():
        source_ids = {str(item) for item in getattr(evidence, "source_block_ids", ()) or ()}
        if chunk.chunk_id not in source_ids:
            continue
        start = getattr(evidence, "start_char", None)
        end = getattr(evidence, "end_char", None)
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(chunk.text):
            anchors.append((start, end))
    if not anchors:
        name = str(getattr(entity, "name", "") or "")
        if name:
            span = chunk.text.find(name)
            if span >= 0:
                anchors.append((span, span + len(name)))

    windows: list[tuple[int, int]] = []
    for anchor_start, anchor_end in anchors:
        floor = max(0, anchor_start - _CONTEXT_RADIUS)
        ceiling = min(len(chunk.text), anchor_end + _CONTEXT_RADIUS)
        left = max((chunk.text.rfind(mark, floor, anchor_start) for mark in _CONTEXT_BOUNDARIES), default=-1)
        start = floor if left < floor else left + 1
        right_candidates = [
            position
            for mark in _CONTEXT_BOUNDARIES
            if (position := chunk.text.find(mark, anchor_end, ceiling)) >= 0
        ]
        end = min(right_candidates) + 1 if right_candidates else ceiling
        if start < end:
            windows.append((start, end))

    merged: list[tuple[int, int]] = []
    for start, end in sorted(set(windows)):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [(start, end, chunk.text[start:end]) for start, end in merged]


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
    if isinstance(value, str) and not value.strip():
        return False
    if isinstance(value, list) and not any(
        not isinstance(item, str) or item.strip() for item in value
    ):
        return False
    # 属性值不需要在 evidence.quote 中逐字出现。调用方会对
    # “实体 + 本次属性集合 + current chunk”做整体语义一致性核验。
    return True


def _contextual_evidence(chunk: ExtractionChunk, start: int, end: int) -> Evidence:
    """Represent bundle-level support with a valid, entity-anchored span."""
    return Evidence(
        span_id=f"{chunk.chunk_id}:{start}:{end}",
        source_block_ids=[chunk.chunk_id],
        quote=chunk.text[start:end],
        start_char=start,
        end_char=end,
        evidence_type="contextual",
    )


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
    route_map: dict[str, DocumentRoute] | None = None,
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
    mode: str = "property",
) -> dict[str, int]:
    """Fill identity or ordinary properties without changing the entity universe."""
    if mode not in {"identity", "property"}:
        raise ValueError("属性补全 mode 仅支持 identity 或 property")
    grouped: dict[str, list[Any]] = defaultdict(list)
    allowed_by_id: dict[str, dict[str, PropertySpec]] = {}
    applied = rejected = calls = verifications = 0
    for entity in pool.accepted_entities():
        if entity.source == "official_attachment":
            continue
        # Deterministic extractors own the complete property set for the
        # entities they create.  Letting the LLM fill their remaining schema
        # fields re-introduces cross-entity leakage and turns optional fields
        # into false positives under a complete golden set.
        if (
            mode == "property"
            and str(getattr(entity, "source", "") or "") in _AUTHORITATIVE_SOURCES
        ):
            continue
        declared = _declared_properties(plan, entity, mode=mode)
        anchors = _entity_anchor_chunks(entity, manager)
        if entity.canonical_type in management_type_ids(plan):
            sources = {
                (manager.get_chunk(anchor).document_id, (manager.get_chunk(anchor).metadata or {}).get("source_file"))
                for anchor in anchors
            }
            sources.discard((None, None))
            sources = {source for source in sources if any(source)}
            anchors = list(dict.fromkeys([*anchors, *[
                chunk.chunk_id for chunk in manager if official_role(plan, chunk) == "form"
                and (chunk.document_id, (chunk.metadata or {}).get("source_file")) in sources
            ]]))
        anchors = [anchor for anchor in anchors if entity.canonical_type in
                   official_entity_types(plan, manager.get_chunk(anchor), {entity.canonical_type})]
        if not declared:
            continue
        if not anchors:
            rejected += len(declared)
            _warn(
                warnings,
                "PROPERTY_ENTITY_WITHOUT_ANCHOR",
                "实体没有可绑定的原文 chunk，闭世属性补全已跳过",
                f"entities.{entity.candidate_id}.properties",
                sorted(declared),
            )
            continue
        for anchor in anchors:
            grouped[anchor].append(entity)
        allowed_by_id[str(entity.candidate_id)] = declared

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
            and official_role(plan, item) == official_role(plan, chunk)
            and (official_role(plan, chunk) != "form" or (
                bool(chunk.document_id or (chunk.metadata or {}).get("source_file"))
                and (item.document_id, (item.metadata or {}).get("source_file"))
                == (chunk.document_id, (chunk.metadata or {}).get("source_file"))
            ))
        ]
        local_context = json.dumps(
            _truncated_neighbor_context(neighbors),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        compatible_groups: dict[tuple[str, tuple[str, ...]], list[Any]] = defaultdict(list)
        for entity in candidates:
            entity_id = str(entity.candidate_id)
            compatible_groups[(
                str(getattr(entity, "canonical_type", "") or ""),
                tuple(sorted(allowed_by_id[entity_id])),
            )].append(entity)
        compatible_batches = [
            batch
            for group in compatible_groups.values()
            for batch in _batches(group, batch_size)
        ]
        for batch_index, batch in enumerate(compatible_batches, start=1):
            batch_allowed = {
                str(entity.candidate_id): allowed_by_id[str(entity.candidate_id)]
                for entity in batch
            }
            output_schema = _schema(batch, batch_allowed)
            messages = build_chat_messages(
                task=mode,
                plan=plan,
                route=(route_map or {}).get(chunk_id, route),
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
                pass_name=mode,
                chunk_id=f"{chunk_id}:batch-{batch_index}",
                max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds,
                call_metrics=call_metrics,
            )
            calls += 1
            if parsed is None:
                continue
            entities_by_id = {str(entity.candidate_id): entity for entity in batch}
            context_windows_by_id = {
                entity_id: _entity_context_windows(entity, chunk)
                for entity_id, entity in entities_by_id.items()
            }
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
                context_windows = context_windows_by_id.get(entity_id) or []
                if not context_windows:
                    rejected += 1
                    _warn(
                        warnings,
                        "PROPERTY_ENTITY_CONTEXT_UNRESOLVED",
                        "实体虽引用当前 chunk，但无法解析局部锚定窗口，属性更新已拒绝",
                        path,
                        entity_id,
                    )
                    continue
                context_text = "\n…\n".join(window[2] for window in context_windows)
                quote_mode = ""
                if quote and manager.find_span(chunk_id, quote) is not None:
                    quote_mode = "exact"
                else:
                    if quote and _looks_like_record_table(chunk):
                        normalized_quote = _locate_record_table_quote(manager, chunk_id, quote)
                        if normalized_quote is not None:
                            quote = normalized_quote
                            quote_mode = "normalized"
                # Presence/similarity is not proof that a value belongs to this
                # entity.  Every bundle therefore goes through the scoped
                # semantic verifier, including exact quotes and record rows.
                pending.append({
                    "item_id": f"{chunk_id}:batch-{batch_index}:update[{update_index}]",
                    "path": path,
                    "entity": entity,
                    "entity_id": entity_id,
                    "update": update,
                    "quote": quote,
                    "context_windows": context_windows,
                    "context_text": context_text,
                    "quote_mode": quote_mode,
                })

            if pending:
                verdicts = verify_items_with_llm(
                    llm,
                    chunk=chunk,
                    items=[
                        {
                            "item_id": item["item_id"],
                            "kind": "property",
                            "entity_type": str(
                                getattr(item["entity"], "canonical_type", "")
                                or getattr(item["entity"], "proposed_type", "")
                                or ""
                            ),
                            "entity": str(getattr(item["entity"], "name", "") or ""),
                            "properties": dict(item["update"].get("properties") or {}),
                            "evidence_quote": item["quote"],
                            "entity_context": item["context_text"],
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
                    if verdict != "yes":
                        rejected += 1
                        code = (
                            "PROPERTY_EVIDENCE_REJECTED"
                            if verdict == "no"
                            else "PROPERTY_EVIDENCE_UNVERIFIED"
                        )
                        message = (
                            "语义核验判定属性更新不受原文支持，已拒绝"
                            if verdict == "no"
                            else "语义核验不确定或失败，闭世属性更新已拒绝"
                        )
                        _warn(
                            warnings,
                            code,
                            message,
                            item["path"],
                            item["quote"][:200],
                        )
                        continue
                    _warn(
                        warnings,
                        "PROPERTY_EVIDENCE_LLM_VERIFIED",
                        "快速语义比对未通过，大模型核验确认整条属性更新受原文支持",
                        item["path"],
                        item["quote"][:200],
                    )
                    planned.append({
                        "path": item["path"],
                        "entity": item["entity"],
                        "entity_id": item["entity_id"],
                        "update": item["update"],
                        "quote": item["quote"],
                        "context_windows": item["context_windows"],
                        "mode": f"{item['quote_mode'] or 'contextual'}_llm_yes",
                    })

            for item in planned:
                entity = item["entity"]
                entity_id = item["entity_id"]
                update = item["update"]
                path = item["path"]
                quote = item["quote"]
                if item["mode"].startswith("normalized"):
                    _warn(warnings, "PROPERTY_EVIDENCE_NORMALIZED", "属性证据经 JSON 记录表归一化后定位", path, quote[:200])
                if "semantic" in item["mode"]:
                    _warn(warnings, "PROPERTY_EVIDENCE_SEMANTIC", "实体、属性集合与当前 chunk 整体语义比对通过", path, quote[:200])
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
                    accepted_values[prop_id] = value
                if accepted_values:
                    entity.properties.update(accepted_values)
                    located = locate_quote(manager, quote, chunk_ids=(chunk_id,)) if quote else None
                    supporting_evidence = (
                        [located]
                        if located is not None
                        else [
                            _contextual_evidence(chunk, start, end)
                            for start, end, _text in item["context_windows"]
                        ]
                    )
                    for prop_id in accepted_values:
                        evidence_rows = entity.property_evidence.setdefault(prop_id, [])
                        for evidence in supporting_evidence:
                            if not any(row.span_id == evidence.span_id for row in evidence_rows):
                                evidence_rows.append(evidence)
                    applied += len(accepted_values)
    summary = {"calls": calls, "applied": applied, "rejected": rejected, "verifications": verifications}
    logger.info(
        "[步骤][%s补全] 输出: 实体=%d 批次调用=%d 应用=%d 拒绝=%d 语义核验=%d",
        "身份" if mode == "identity" else "属性",
        len(pool.accepted_entities()),
        calls,
        applied,
        rejected,
        verifications,
    )
    return summary


def enrich_entity_identities(*args: Any, **kwargs: Any) -> dict[str, int]:
    """Complete only ontology-declared identities before the freeze boundary."""
    kwargs["mode"] = "identity"
    return enrich_entity_properties(*args, **kwargs)


__all__ = ["enrich_entity_identities", "enrich_entity_properties"]
