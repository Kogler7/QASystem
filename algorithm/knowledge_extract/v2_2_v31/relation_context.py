"""实体出现位置与局部证据窗口。

关系阶段只读上游切片，按句、表格行切出临时证据视图。视图记录原 chunk
内的区间，不改 chunk 文本、ID 或 ``__source_range``。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from algorithm.table_jsonl import decode_records

from .chunk_context import ChunkContextManager, ExtractionChunk


_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?\n]?")
_CLAUSE_REF = re.compile(r"第\s*([0-9]+(?:\.[0-9]+)*)\s*[条章节]")
_MIN_FORM = 2
_MAX_FORM = 180


@dataclass(frozen=True)
class EntityRef:
    """冻结后的关系端点。``entity_id`` 是候选池里的稳定 ID，不另做别名合并。"""

    entity_id: str
    type_name: str
    name: str
    scope: str
    anchor: bool


@dataclass(frozen=True)
class TextUnit:
    """原 chunk 内的一句或一行。``start``/``end`` 左闭右开。"""

    unit_id: str
    chunk_id: str
    scope: str
    start: int
    end: int
    text: str
    kind: str  # sentence | row


@dataclass
class RelationIndex:
    entities: dict[str, EntityRef]
    units: list[TextUnit]
    unit_entities: dict[str, list[str]]
    chunk_scopes: dict[str, str]
    neighbor_chunks: set[tuple[str, str]]
    chunks_by_scope: dict[str, list[ExtractionChunk]]
    coverage_gaps: list[str] = field(default_factory=list)
    mixed_document_scope: bool = False

    def chunk(self, chunk_id: str) -> ExtractionChunk | None:
        for chunks in self.chunks_by_scope.values():
            for chunk in chunks:
                if chunk.chunk_id == chunk_id:
                    return chunk
        return None

    def unit_by_id(self, unit_id: str) -> TextUnit | None:
        for unit in self.units:
            if unit.unit_id == unit_id:
                return unit
        return None


def document_scopes(chunks: Iterable[ExtractionChunk]) -> tuple[dict[str, str], bool]:
    """按调用方文档分组。全部没有 document_id 时视为同一份文档。

    同一请求里既有明确文档、又有无法归属的切片时，后者单独成组，调用方应报告不支持跨片合并。
    """
    rows = list(chunks)
    named = {str(chunk.document_id) for chunk in rows if chunk.document_id}
    mixed = bool(named) and any(not chunk.document_id for chunk in rows)
    scopes: dict[str, str] = {}
    for chunk in rows:
        if not named:
            scopes[chunk.chunk_id] = "document"
        elif chunk.document_id:
            scopes[chunk.chunk_id] = str(chunk.document_id)
        else:
            scopes[chunk.chunk_id] = f"unscoped:{chunk.chunk_id}"
    return scopes, mixed


def clause_refs(text: str) -> list[str]:
    """抽取「第 5.2 条」这类明确条款号。"""
    found: list[str] = []
    seen: set[str] = set()
    for match in _CLAUSE_REF.finditer(text or ""):
        number = match.group(1)
        if number not in seen:
            seen.add(number)
            found.append(number)
    return found


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in _SENTENCE.finditer(text or ""):
        raw = match.group()
        if not raw.strip():
            continue
        lead = len(raw) - len(raw.lstrip())
        trail = len(raw) - len(raw.rstrip())
        start = match.start() + lead
        end = match.end() - trail
        if start < end:
            spans.append((start, end))
    return spans


def _record_row_spans(text: str) -> list[tuple[int, int]]:
    """JSONL 记录表里每一行对象的原文区间。普通正文返回空。"""
    spans: list[tuple[int, int]] = []
    offset = 0
    for line in (text or "").splitlines(keepends=True):
        stripped = line.strip()
        start = offset + (len(line) - len(line.lstrip()))
        end = offset + len(line.rstrip("\r\n"))
        if stripped.startswith("{") and decode_records(stripped):
            spans.append((start, end))
        offset += len(line)
    return spans if len(spans) >= 2 else []


def _pipe_row_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    offset = 0
    for line in (text or "").splitlines(keepends=True):
        stripped = line.strip()
        is_rule = bool(re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", stripped))
        if stripped.startswith("|") and stripped.count("|") >= 2 and not is_rule:
            start = offset + (len(line) - len(line.lstrip()))
            end = offset + len(line.rstrip("\r\n"))
            if start < end:
                spans.append((start, end))
        offset += len(line)
    return spans if len(spans) >= 2 else []


def evidence_units(chunk: ExtractionChunk, scope: str) -> list[TextUnit]:
    """优先表格行，否则按句。没有句界时保留整片，不把宽区间伪造成行。"""
    text = chunk.text or ""
    row_spans = _record_row_spans(text) or _pipe_row_spans(text)
    if row_spans:
        spans = row_spans
        kind = "row"
    else:
        spans = _sentence_spans(text)
        kind = "sentence"
        if not spans and text.strip():
            stripped = text.strip()
            start = text.find(stripped)
            spans = [(start, start + len(stripped))]
    units: list[TextUnit] = []
    for start, end in spans:
        units.append(
            TextUnit(
                unit_id=f"{chunk.chunk_id}:{start}:{end}",
                chunk_id=chunk.chunk_id,
                scope=scope,
                start=start,
                end=end,
                text=text[start:end],
                kind=kind,
            )
        )
    return units


def _forms(entity: Any) -> list[str]:
    raw: list[str] = [str(getattr(entity, "name", "") or "")]
    raw.extend(str(item) for item in (getattr(entity, "mentions", None) or []) if item)
    for evidence in getattr(entity, "evidence", ()) or ():
        quote = str(getattr(evidence, "quote", "") or "").strip()
        if quote:
            raw.append(quote)
    forms: list[str] = []
    seen: set[str] = set()
    for item in raw:
        form = " ".join(str(item).split())
        if not (_MIN_FORM <= len(form) <= _MAX_FORM) or form in seen:
            continue
        seen.add(form)
        forms.append(form)
    forms.sort(key=len, reverse=True)
    return forms


def _find_all(text: str, form: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    while form and start <= len(text):
        index = text.find(form, start)
        if index < 0:
            break
        spans.append((index, index + len(form)))
        start = index + max(1, len(form))
    return spans


def _containing_unit(units: list[TextUnit], start: int, end: int) -> TextUnit | None:
    containers = [unit for unit in units if unit.start <= start and end <= unit.end]
    if not containers:
        return None
    return min(containers, key=lambda unit: (unit.end - unit.start, unit.start))


def build_relation_index(
    entities: Iterable[Any],
    manager: ChunkContextManager,
    *,
    local_previous: int,
    local_next: int,
    same_section: bool,
) -> RelationIndex:
    """保留能映射到最终实体、且能在原文中定位的出现记录。"""
    chunks = list(manager)
    scopes, mixed = document_scopes(chunks)
    chunks_by_scope: dict[str, list[ExtractionChunk]] = {}
    for chunk in chunks:
        chunks_by_scope.setdefault(scopes[chunk.chunk_id], []).append(chunk)

    units: list[TextUnit] = []
    units_by_chunk: dict[str, list[TextUnit]] = {}
    for chunk in chunks:
        chunk_units = evidence_units(chunk, scopes[chunk.chunk_id])
        units.extend(chunk_units)
        units_by_chunk[chunk.chunk_id] = chunk_units

    entity_rows: dict[str, EntityRef] = {}
    unit_entities: dict[str, list[str]] = {unit.unit_id: [] for unit in units}
    gaps: list[str] = []
    for entity in entities:
        entity_id = str(getattr(entity, "candidate_id", "") or "")
        type_name = str(getattr(entity, "canonical_type", "") or getattr(entity, "proposed_type", "") or "")
        name = str(getattr(entity, "name", "") or "")
        if not entity_id or not type_name:
            continue
        evidence_chunks = {
            block_id
            for evidence in (getattr(entity, "evidence", ()) or ())
            for block_id in (getattr(evidence, "source_block_ids", None) or ())
        }
        scope = ""
        for block_id in evidence_chunks:
            if block_id in scopes:
                scope = scopes[block_id]
                break
        if not scope:
            if len(chunks_by_scope) == 1:
                scope = next(iter(chunks_by_scope))
            else:
                gaps.append(entity_id)
                continue
        anchor = str(getattr(entity, "source", "") or "") == "document_anchor"
        entity_rows[entity_id] = EntityRef(
            entity_id=entity_id,
            type_name=type_name,
            name=name,
            scope=scope,
            anchor=anchor,
        )
        forms = _forms(entity)
        placed = False
        for chunk in chunks_by_scope.get(scope, []):
            text = chunk.text or ""
            chunk_units = units_by_chunk.get(chunk.chunk_id, [])
            for form in forms:
                if form not in text:
                    continue
                for start, end in _find_all(text, form):
                    unit = _containing_unit(chunk_units, start, end)
                    if unit is None:
                        continue
                    bucket = unit_entities[unit.unit_id]
                    if entity_id not in bucket:
                        bucket.append(entity_id)
                    placed = True
        if not placed and not anchor:
            gaps.append(entity_id)
    neighbor_chunks = _neighbor_chunks(
        chunks_by_scope,
        local_previous=local_previous,
        local_next=local_next,
        same_section=same_section,
    )
    return RelationIndex(
        entities=entity_rows,
        units=units,
        unit_entities=unit_entities,
        chunk_scopes=scopes,
        neighbor_chunks=neighbor_chunks,
        chunks_by_scope=chunks_by_scope,
        coverage_gaps=gaps,
        mixed_document_scope=mixed,
    )


def _section_compatible(left: ExtractionChunk, right: ExtractionChunk, same_section: bool) -> bool:
    if not same_section:
        return True
    if left.section_id is None or right.section_id is None:
        return True
    return left.section_id == right.section_id


def _neighbor_chunks(
    chunks_by_scope: dict[str, list[ExtractionChunk]],
    *,
    local_previous: int,
    local_next: int,
    same_section: bool,
) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for chunks in chunks_by_scope.values():
        for index, chunk in enumerate(chunks):
            for step in range(1, max(0, local_next) + 1):
                if index + step >= len(chunks):
                    break
                other = chunks[index + step]
                if _section_compatible(chunk, other, same_section):
                    pairs.add((chunk.chunk_id, other.chunk_id))
            for step in range(1, max(0, local_previous) + 1):
                if index - step < 0:
                    break
                other = chunks[index - step]
                if _section_compatible(chunk, other, same_section):
                    pairs.add((chunk.chunk_id, other.chunk_id))
    return pairs


def cited_units(index: RelationIndex, scope: str, number: str, *, limit: int = 3) -> list[TextUnit]:
    """按条款号取回少量被引用片段，不扫描无关实体对。"""
    needle = f"第{number}条"
    found: list[TextUnit] = []
    for chunk in index.chunks_by_scope.get(scope, []):
        clause = str(chunk.clause_number or "")
        if clause != number and needle not in (chunk.text or "") and f"第 {number} 条" not in (chunk.text or ""):
            continue
        for unit in index.units:
            if unit.chunk_id != chunk.chunk_id:
                continue
            if clause == number or needle in unit.text or f"第 {number} 条" in unit.text:
                found.append(unit)
                if len(found) >= limit:
                    return found
    return found


__all__ = [
    "EntityRef",
    "RelationIndex",
    "TextUnit",
    "build_relation_index",
    "cited_units",
    "clause_refs",
    "document_scopes",
    "evidence_units",
]
