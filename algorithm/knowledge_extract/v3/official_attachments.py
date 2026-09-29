"""Deterministic preservation of official-document attachment chunks."""
from __future__ import annotations

import os
import json
from collections import defaultdict
from typing import Any, Iterable

from .candidate_models import EntityCandidate
from .evidence import Evidence
from .graph_utils import canonical_name, coerce_non_empty_name


def _warn(warnings: list[dict[str, Any]] | None, code: str, message: str, value: Any = None) -> None:
    if warnings is not None:
        item = {"code": code, "message": message, "path": "official_attachments"}
        if value is not None:
            item["value"] = value
        warnings.append(item)


def _attachment_class(plan: Any, pool: Any) -> Any | None:
    specs = getattr(plan, "class_specs", None) or getattr(getattr(pool, "schema", None), "class_specs", {})
    for class_id, spec in (specs.items() if isinstance(specs, dict) else ()):
        labels = (class_id, getattr(spec, "display_name", ""), *(getattr(spec, "stereotypes", ()) or ()))
        if any(canonical_name(label).replace("_", "") in {"operationalinformation", "操作信息", "运行信息"} for label in labels):
            return spec
    return None


def _class_id(spec: Any) -> str:
    return str(getattr(spec, "class_id", "") or getattr(spec, "identifier", ""))


def _property(spec: Any, *labels: str) -> Any | None:
    wanted = {canonical_name(label).replace("_", "") for label in labels}
    for prop in getattr(spec, "properties", ()) or ():
        values = (getattr(prop, "identifier", ""), getattr(prop, "display_name", ""))
        if any(canonical_name(value).replace("_", "") in wanted for value in values):
            return prop
    return None


def _chunk_meta(chunk: Any) -> dict[str, Any]:
    value = getattr(chunk, "metadata", {}) or {}
    return value if isinstance(value, dict) else {}


def _source_key(chunk: Any) -> tuple[str, str]:
    meta = _chunk_meta(chunk)
    source = coerce_non_empty_name(meta.get("source_file"))
    document = coerce_non_empty_name(getattr(chunk, "document_id", None) or meta.get("document_id"))
    return source, document


def _joined_text(chunks: list[Any], warnings: list[dict[str, Any]] | None = None) -> str:
    has_offsets = all(isinstance(getattr(chunk, "start_offset", None), int) and isinstance(getattr(chunk, "end_offset", None), int) for chunk in chunks)
    ordered = sorted(chunks, key=lambda c: (getattr(c, "start_offset", 0) or 0)) if has_offsets else list(chunks)
    result: list[str] = []
    known_ranges = has_offsets
    covered_end: int | None = None
    previous_text = ""
    for chunk in ordered:
        text = str(getattr(chunk, "text", "") or "")
        start = getattr(chunk, "start_offset", None)
        end = getattr(chunk, "end_offset", None)
        if not text:
            continue
        if isinstance(start, int) and isinstance(end, int) and covered_end is not None and start < covered_end:
            trim = min(len(text), covered_end - start)
            overlap = min(len(previous_text), covered_end - start)
            if overlap and previous_text[-overlap:] != text[:overlap]:
                _warn(warnings, "ATTACHMENT_OVERLAP_TEXT_MISMATCH", "附件重叠 chunk 原文不一致，保留两段原文", getattr(chunk, "chunk_id", ""))
            else:
                text = text[trim:]
        if text:
            if result and not known_ranges:
                result.append("\n")
            result.append(text)
        if isinstance(start, int) and isinstance(end, int):
            covered_end = max(covered_end or end, end)
        elif covered_end is None:
            covered_end = 0
        previous_text = str(getattr(chunk, "text", "") or "")
    return "".join(result)


def _candidate(spec: Any, chunks: list[Any], source_name: str, properties: dict[str, Any], source_file: str, document_id: str) -> EntityCandidate:
    class_id = _class_id(spec)
    evidences = []
    for chunk in chunks:
        text = str(getattr(chunk, "text", "") or "")
        chunk_id = str(getattr(chunk, "chunk_id", "") or "attachment")
        evidences.append(Evidence(
            span_id=f"{chunk_id}:0:{len(text)}",
            source_block_ids=[chunk_id],
            quote=text,
            start_char=0,
            end_char=len(text),
            evidence_type="structural",
        ))
    candidate = EntityCandidate(
        candidate_id="",
        proposed_type=class_id,
        canonical_type=class_id,
        name=source_name,
        properties=properties,
        evidence=evidences,
        source="official_attachment",
    )
    source_identity = json.dumps({"document_id": document_id, "source_file": source_file}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    setattr(candidate, "source_identity", source_identity)
    source_meta = _chunk_meta(chunks[0]) if chunks else {}
    source_ref = {"source_file": source_file, "document_id": document_id}
    for key in ("source_url", "file_id"):
        if source_meta.get(key) not in (None, ""):
            source_ref[key] = source_meta[key]
    setattr(candidate, "source_files", [source_ref])
    for prop_id in properties:
        candidate.property_evidence[prop_id] = list(evidences)
    return candidate


def build_official_attachment_entities(
    plan: Any,
    pool: Any,
    manager: Iterable[Any],
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> list[EntityCandidate]:
    """Build one ontology-backed attachment entity per source file/document."""
    spec = _attachment_class(plan, pool)
    if spec is None:
        _warn(warnings, "ATTACHMENT_TYPE_UNAVAILABLE", "本体没有 OperationalInformation，附件仅保留为原文证据")
        return []
    class_id = _class_id(spec)
    plan_allowed = getattr(plan, "allowed_types", None)
    allowed = set(plan_allowed if plan_allowed is not None else getattr(pool, "allowed_types", ()))
    if class_id not in allowed:
        _warn(warnings, "ATTACHMENT_TYPE_OUT_OF_SCOPE", "OperationalInformation 被 target scope 排除，未构造附件实体", class_id)
        return []
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for chunk in manager:
        if _chunk_meta(chunk).get("official_document_role") == "attachment":
            groups[_source_key(chunk)].append(chunk)
    name_prop = _property(spec, "信息名称", "名称", "标题", "name", "title", "filename")
    type_prop = _property(spec, "信息类型", "类型", "informationType", "type")
    text_prop = _property(spec, "sourceText", "来源文本", "原文", "source", "text", "content")
    file_prop = _property(spec, "sourceFile", "来源文件", "文件引用", "fileReference", "reference")
    result: list[EntityCandidate] = []
    for (source_file, document_id), chunks in sorted(groups.items()):
        display = source_file or document_id or str(getattr(chunks[0], "chunk_id", "attachment"))
        display = os.path.basename(display.replace("\\", "/")) or display
        properties: dict[str, Any] = {}
        if name_prop is not None:
            properties[name_prop.identifier] = display
        if type_prop is not None:
            properties[type_prop.identifier] = "附件"
        if text_prop is not None:
            properties[text_prop.identifier] = _joined_text(chunks, warnings)
        if file_prop is not None and (source_file or document_id):
            properties[file_prop.identifier] = source_file or document_id
        if name_prop is None:
            _warn(warnings, "ATTACHMENT_NAME_PROPERTY_UNAVAILABLE", "本体没有信息名称属性，附件名称仅保留在实体 name", class_id)
        if text_prop is None:
            _warn(warnings, "ATTACHMENT_SOURCE_TEXT_UNMODELED", "本体没有来源文本属性，完整附件原文仅保留在 evidence/property evidence", class_id)
        result.append(_candidate(spec, chunks, display, properties, source_file, document_id))
    return result


def add_official_attachment_entities(
    plan: Any,
    pool: Any,
    manager: Iterable[Any],
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> int:
    before = getattr(pool, "entity_candidate_count", 0)
    for candidate in build_official_attachment_entities(plan, pool, manager, warnings=warnings):
        pool.add_entity(candidate, warnings=warnings)
    return getattr(pool, "entity_candidate_count", 0) - before


__all__ = ["build_official_attachment_entities", "add_official_attachment_entities"]
