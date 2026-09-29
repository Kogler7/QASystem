"""Source-role boundaries for official documents, independent of table format."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from typing import Any, Iterable

from .structured_text import has_json_table_payload


def management_type_ids(plan: Any) -> frozenset[str]:
    return frozenset(
        type_id for type_id, spec in plan.class_specs.items()
        if any(str(value).casefold() in {"officialdocument", "公文管理单元"}
               for value in (type_id, spec.display_name, *spec.stereotypes))
    )

def operational_type_ids(plan: Any) -> frozenset[str]:
    """公文业务实体类型（Operational*）：表单文字中明确的职责/事项/交互同样可以抽取。"""
    return frozenset(
        type_id for type_id, spec in plan.class_specs.items()
        if any(
            str(value).strip().casefold().startswith("operational")
            for value in (type_id, spec.display_name, *spec.stereotypes)
        )
    )




def looks_like_official_form(chunk: Any) -> bool:
    text = unicodedata.normalize("NFKC", chunk.text or "")
    source = str((chunk.metadata or {}).get("source_file") or "").replace("\\", "/").rsplit("/", 1)[-1]
    if re.search(r"(?:表单|收文处理单|审批单)", source):
        return True
    head = text[:600]
    markers = ("收文处理单", "拟办意见", "领导批示", "已阅", "来文单位", "来文字号")
    hits = sum(marker in head for marker in markers)
    if "收文处理单" in head or ("表单" in head and hits):
        return True
    return hits >= 2 and (has_json_table_payload(text) or "拟办意见" in head or "领导批示" in head)


def official_role(plan: Any, chunk: Any, route: Any = None) -> str:
    if not management_type_ids(plan):
        return ""
    metadata = chunk.metadata or {}
    role = str(metadata.get("official_document_role") or "")
    if role in {"form", "body", "attachment"}:
        return role
    source = str(metadata.get("source_file") or "").replace("\\", "/")
    filename = source.rsplit("/", 1)[-1]
    if re.match(r"^(?:附件|附录|attachment|appendix)(?:[\s_\-\d一二三四五六七八九十：:.]|$)", filename, re.I):
        return "attachment"
    if looks_like_official_form(chunk):
        return "form"
    labels = getattr(route, "labels", ()) or ()
    if getattr(route, "kind", "") == "official_document" or "official_document" in labels:
        return "body"
    return ""


def annotate_official_roles(plan: Any, chunks: Iterable[Any], routes: dict[str, Any]) -> dict[str, str]:
    roles = {}
    for chunk in chunks:
        role = official_role(plan, chunk, routes.get(chunk.chunk_id))
        if role:
            chunk.metadata["official_document_role"] = role
            roles[chunk.chunk_id] = role
    return roles


def split_embedded_attachments(plan: Any, chunks: Iterable[Any], routes: dict[str, Any]) -> list[Any]:
    """Isolate explicit attachment sections before any model sees the text.

    Inline references such as '见附件1' or an '附件：文件名' list are not
    section boundaries. An explicit standalone heading starts the attachment
    tail of that source file, including subsequent chunks without a heading.
    """
    chunks = list(chunks)
    if not management_type_ids(plan):
        return chunks
    heading = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]+附件(?:[ \t]*[0-9一二三四五六七八九十]+)?[^\n]*|附件[ \t]*[0-9一二三四五六七八九十]*[ \t]*)$")
    active_sources: set[tuple[Any, Any]] = set()
    result = []
    for chunk in chunks:
        source = (chunk.document_id, (chunk.metadata or {}).get("source_file"))
        role = official_role(plan, chunk, routes.get(chunk.chunk_id))
        if role != "body":
            result.append(chunk)
            continue
        match = heading.search(chunk.text or "")
        split_at = 0 if source in active_sources else (match.start() if match else None)
        if split_at is None:
            result.append(chunk)
            continue
        active_sources.add(source)
        pieces = [(0, split_at, "body"), (split_at, len(chunk.text), "attachment")]
        for start, end, part_role in pieces:
            if start == end:
                continue
            metadata = dict(chunk.metadata or {})
            metadata.update(official_document_role=part_role, source_chunk_id=chunk.chunk_id)
            result.append(replace(
                chunk, chunk_id=f"{chunk.chunk_id}::{part_role}", text=chunk.text[start:end], metadata=metadata,
                parent_chunk_id=None, previous_chunk_id=None, next_chunk_id=None,
                start_offset=chunk.start_offset + start if chunk.start_offset is not None else None,
                end_offset=chunk.start_offset + end if chunk.start_offset is not None else None,
            ))
    return result


def official_entity_types(plan: Any, chunk: Any, types: Iterable[str], route: Any = None) -> frozenset[str]:
    role = official_role(plan, chunk, route)
    management = management_type_ids(plan)
    if role == "form":
        # 表单仍作为官方文档属性来源、不放行结构单元，但表单文字里明确的职责/事项/交互
        # 应当可以产生 operational 业务实体（与 golden 标注一致）。
        allowed = set(management) | operational_type_ids(plan)
        return frozenset(set(plan.allowed_types) & allowed)
    if role == "body":
        return frozenset(set(types) - management)
    if role == "attachment":
        return frozenset()
    return frozenset(types)


def entity_in_official_scope(plan: Any, manager: Any, candidate: Any) -> bool:
    for evidence in candidate.evidence:
        for chunk_id in evidence.source_block_ids:
            if not manager.has_chunk(chunk_id):
                continue
            chunk = manager.get_chunk(chunk_id)
            role = official_role(plan, chunk)
            if candidate.source == "official_attachment":
                if role != "attachment":
                    return False
            elif candidate.canonical_type not in official_entity_types(plan, chunk, plan.allowed_types):
                return False
    return True


def relation_in_official_scope(plan: Any, manager: Any, candidate: Any) -> bool:
    return all(
        not manager.has_chunk(chunk_id)
        or official_role(plan, manager.get_chunk(chunk_id)) != "attachment"
        for evidence in candidate.evidence or ()
        for chunk_id in evidence.source_block_ids
    )


__all__ = [
    "management_type_ids", "looks_like_official_form", "official_role", "annotate_official_roles",
    "split_embedded_attachments", "official_entity_types", "entity_in_official_scope",
    "relation_in_official_scope",
    "operational_type_ids",
]
