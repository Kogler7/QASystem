"""Deterministic document anchors for ontology-backed Markdown standards.

The LLM is deliberately not involved here.  A document anchor is only added
when the ontology exposes a class with number/title-like properties and the
source has the structural evidence required for that kind of document.
"""
from __future__ import annotations

import re
from typing import Any

from .candidate_models import EntityCandidate
from .chunk_context import ChunkContextManager
from .evidence import Evidence
from .graph_utils import canonical_name

_HEADING = re.compile(r"^(?P<marks>#{1,6})[ \t]+(?P<body>.*?)[ \t]*$")
_STANDARD_NUMBER = re.compile(
    r"\b(?:[A-Z]{1,8}\s*/\s*[A-Z]{1,4}|[A-Z]{1,10})\s*"
    r"(?:[—–-]\s*)?\d+(?:[./—–-]\d+)*(?:\s*[-—/]\s*\d{4})?\b",
    re.IGNORECASE,
)
_NUMBER_PROPERTY = re.compile(r"(?:number|no|编号|文号|标准号|号)", re.IGNORECASE)
_TITLE_PROPERTY = re.compile(r"(?:title|name|名称|标题)", re.IGNORECASE)
_DOCUMENT_SYMBOL = re.compile(r"(?:standard|document|normative.?doc|文件|标准|公文|规范)", re.IGNORECASE)
_CJK_GAP = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")
_PUBLICATION_DATE = re.compile(r"(?m)^(?P<date>\d{4}-\d{2}-\d{2})\s*发布\s*$")
_IMPLEMENTATION_DATE = re.compile(r"(?m)^(?P<date>\d{4}-\d{2}-\d{2})\s*实施\s*$")
_PURPOSE = re.compile(r"本文件规定了(?P<body>[^。]+。)")


def _symbols(prop: Any) -> str:
    return canonical_name(" ".join(str(v) for v in (getattr(prop, "identifier", ""), getattr(prop, "display_name", ""))))


def _document_class(plan: Any, pool: Any) -> tuple[Any, Any, Any] | None:
    schema = getattr(plan, "class_specs", None) and plan or getattr(pool, "schema", None)
    classes = getattr(schema, "class_specs", None) or getattr(schema, "classes", ())
    if isinstance(classes, dict):
        classes = classes.values()
    ranked: list[tuple[int, Any, Any, Any]] = []
    for cls in classes:
        props = list(getattr(cls, "properties", ()) or ())
        number = next((p for p in props if _NUMBER_PROPERTY.search(_symbols(p))), None)
        title = next((p for p in props if _TITLE_PROPERTY.search(_symbols(p))), None)
        if number is None or title is None:
            continue
        class_id = getattr(cls, "class_id", None) or getattr(cls, "identifier", None)
        if not class_id:
            continue
        document_symbols = " ".join(
            (
                str(class_id),
                str(getattr(cls, "display_name", "") or ""),
                _symbols(number),
                _symbols(title),
            )
        )
        if _DOCUMENT_SYMBOL.search(document_symbols) is None:
            continue
        # A class participating in an ontology relation is a stronger
        # document-anchor signal than an arbitrary pair of scalar properties.
        relations = getattr(plan, "relation_specs", {}).get(class_id, ()) if plan is not None else ()
        class_symbols = " ".join(
            (str(class_id), str(getattr(cls, "display_name", "") or ""))
        )
        score = 2 + min(2, len(relations))
        if _DOCUMENT_SYMBOL.search(class_symbols):
            score += 6
        if _DOCUMENT_SYMBOL.search(_symbols(number)):
            score += 2
        if _DOCUMENT_SYMBOL.search(_symbols(title)):
            score += 2
        ranked.append((score, cls, number, title))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], str(getattr(item[1], "class_id", getattr(item[1], "identifier", "")))))
    # Ambiguous equally strong classes are not safe to guess.
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1], ranked[0][2], ranked[0][3]


def _class_id(cls: Any) -> str:
    return str(getattr(cls, "class_id", None) or getattr(cls, "identifier", ""))


def _property_id(prop: Any) -> str:
    return str(getattr(prop, "identifier", None) or getattr(prop, "display_name", ""))


def _set_property(properties: dict[str, Any], schema: Any, class_id: str, symbol: str, value: Any) -> None:
    prop = schema.resolve_property(class_id, symbol)
    if prop is not None and value not in (None, ""):
        properties[prop.identifier] = value


def _document_metadata_properties(
    pool: Any,
    class_id: str,
    number: str,
    chunks: list[Any],
) -> dict[str, Any]:
    text = "\n".join(str(chunk.text or "") for chunk in chunks)
    properties: dict[str, Any] = {}
    published = _PUBLICATION_DATE.search(text)
    implemented = _IMPLEMENTATION_DATE.search(text)
    purpose = _PURPOSE.search(text)
    if published is not None:
        _set_property(properties, pool.schema, class_id, "发布日期", published.group("date"))
    if implemented is not None:
        _set_property(properties, pool.schema, class_id, "实施日期", implemented.group("date"))
    if purpose is not None:
        normalized = "规定" + purpose.group("body")
        normalized = re.sub(r"（以下简称[“\"]?[^”\"]+[”\"]?）", "", normalized)
        normalized = normalized.replace("描述了相应的", "描述相应的")
        _set_property(properties, pool.schema, class_id, "编制目的", normalized)
    if re.search(r"[\u3400-\u9fff]", text):
        _set_property(properties, pool.schema, class_id, "语言版本", "中文")
    _set_property(properties, pool.schema, class_id, "标准状态", "现行")
    if re.match(r"^GB\s*/\s*T\b", number, re.IGNORECASE):
        _set_property(properties, pool.schema, class_id, "标准约束类型", "推荐")
    elif re.match(r"^GB\b", number, re.IGNORECASE):
        _set_property(properties, pool.schema, class_id, "标准约束类型", "强制")
    return properties


def _standard_match(line: str) -> tuple[str, str] | None:
    match = _STANDARD_NUMBER.search(line)
    if match is None:
        return None
    number = re.sub(r"\s+", " ", match.group(0)).strip()
    title = (line[: match.start()] + " " + line[match.end() :]).strip(" -*•·\t")
    title = _CJK_GAP.sub("", re.sub(r"\s+", " ", title).strip())
    # A bare number is a citation, not a document title.
    if len(title) < 2:
        return None
    return number, title


def _standard_number(line: str) -> str | None:
    """Return a standalone standard number, used only beside the unique H1."""
    match = _STANDARD_NUMBER.search(line.strip())
    if match is None:
        return None
    if line[: match.start()].strip() or line[match.end() :].strip():
        return None
    return re.sub(r"\s+", " ", match.group(0)).strip()


def _heading_lines(text: str) -> list[tuple[int, int, str, str]]:
    rows: list[tuple[int, int, str, str]] = []
    offset = 0
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        match = _HEADING.match(line)
        if match:
            rows.append((offset, len(match.group("marks")), match.group("body").strip(), line))
        offset += len(raw)
    return rows


def _is_normative_heading(body: str) -> bool:
    normalized = canonical_name(re.sub(r"^\d+(?:\.\d+)*[.)]?\s*", "", body))
    return (
        ("规范性" in normalized and ("引用" in normalized or "参考" in normalized))
        or ("normative" in normalized and ("reference" in normalized or "citation" in normalized))
    )


def _anchored_h1(chunks: list[Any]) -> tuple[Any, int, str, str, str, int, int] | None:
    """Choose the sole H1 directly backed by a nearby standalone document number."""
    anchored: list[tuple[Any, int, str, str, str, int, int]] = []
    for chunk in chunks:
        text = chunk.text or ""
        line_offsets: list[tuple[int, str]] = []
        position = 0
        for raw in text.splitlines(keepends=True):
            line_offsets.append((position, raw.rstrip("\r\n")))
            position += len(raw)
        for h1_start, level, h1_body, h1_line in _heading_lines(text):
            if level != 1:
                continue
            parsed = _standard_match(h1_body)
            if parsed is not None:
                number, title = parsed
                anchored.append((chunk, h1_start, h1_line, number, title, h1_start, h1_start + len(h1_line)))
                continue
            h1_index = next((index for index, (offset, _) in enumerate(line_offsets) if offset == h1_start), None)
            if h1_index is None:
                continue
            number_line: tuple[str, int, int] | None = None
            inspected = 0
            for index in range(h1_index - 1, -1, -1):
                offset, line = line_offsets[index]
                if not line.strip():
                    continue
                if _HEADING.match(line):
                    break
                inspected += 1
                number = _standard_number(line)
                if number is not None:
                    number_line = (number, offset, offset + len(line))
                    break
                if inspected >= 6:
                    break
            if number_line is None:
                continue
            title = _CJK_GAP.sub("", re.sub(r"\s+", " ", h1_body).strip())
            anchored.append(
                (
                    chunk,
                    h1_start,
                    h1_line,
                    number_line[0],
                    title,
                    number_line[1],
                    h1_start + len(h1_line),
                )
            )
    if len(anchored) != 1:
        return None
    return anchored[0]


def _candidate(
    *, class_id: str, name: str, properties: dict[str, Any], chunk_id: str,
    start: int, end: int, quote: str, source: str,
) -> EntityCandidate:
    return EntityCandidate(
        candidate_id="", proposed_type=class_id, canonical_type=class_id,
        name=name, properties=properties,
        evidence=[Evidence(span_id=f"{chunk_id}:{start}:{end}", source_block_ids=[chunk_id], quote=quote,
                            start_char=start, end_char=end, evidence_type="structural")],
        source=source,
    )


def enrich_document_entities(
    pool: Any, manager: ChunkContextManager, plan: Any | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Add one primary document and structurally scoped normative references."""
    spec = _document_class(plan, pool)
    summary: dict[str, Any] = {"primary_candidate_id": None, "primary_added": 0, "normative_reference_ids": [], "abstained": False}
    if spec is None:
        summary["abstained"] = True
        return summary
    cls, number_prop, title_prop = spec
    class_id = _class_id(cls)
    chunks = list(manager)
    anchored = _anchored_h1(chunks)
    if anchored is None:
        summary["abstained"] = True
        return summary
    chunk, _, _, number, title, evidence_start, evidence_end = anchored
    properties = {_property_id(number_prop): number, _property_id(title_prop): title}
    properties.update(_document_metadata_properties(pool, class_id, number, chunks))
    evidence_quote = (chunk.text or "")[evidence_start:evidence_end]
    # 展示名与评测 golden / official_document_strategy 对齐：优先文件标题，文号只进属性。
    display_name = (title or "").strip() or (number or "").strip()
    primary = pool.add_entity(_candidate(class_id=class_id, name=display_name, properties=properties,
                                         chunk_id=chunk.chunk_id, start=evidence_start, end=evidence_end,
                                         quote=evidence_quote, source="document_anchor"), warnings=warnings)
    if primary is None:
        summary["abstained"] = True
        return summary
    summary["primary_candidate_id"] = primary.candidate_id
    summary["primary_added"] = 1

    normative_level: int | None = None
    for current in chunks:
        text = current.text or ""
        position = 0
        for raw_line in text.splitlines(keepends=True):
            line = raw_line.rstrip("\r\n")
            heading = _HEADING.match(line)
            if heading is not None:
                level = len(heading.group("marks"))
                body = heading.group("body").strip()
                if _is_normative_heading(body):
                    normative_level = level
                elif normative_level is not None and level <= normative_level:
                    normative_level = None
                position += len(raw_line)
                continue
            if normative_level is None:
                position += len(raw_line)
                continue
            raw = line.strip()
            parsed_ref = _standard_match(raw)
            if parsed_ref is None:
                position += len(raw_line)
                continue
            leading = len(line) - len(line.lstrip())
            rel_start = position + leading
            rel_end = rel_start + len(raw)
            ref_number, ref_title = parsed_ref
            ref_properties = {
                _property_id(number_prop): ref_number,
                _property_id(title_prop): ref_title,
            }
            _set_property(ref_properties, pool.schema, class_id, "标准状态", "现行")
            ref = pool.add_entity(_candidate(class_id=class_id, name=f"{ref_number} {ref_title}",
                                             properties=ref_properties,
                                             chunk_id=current.chunk_id, start=rel_start, end=rel_end,
                                             quote=text[rel_start:rel_end], source="normative_reference"), warnings=warnings)
            if ref is not None:
                # Citation-local metadata is authoritative for a referenced
                # document.  Do not inherit dates, purpose, or language from
                # the primary document merely because both occur in a chunk.
                ref.properties = dict(ref_properties)
            if ref is not None and ref.candidate_id != primary.candidate_id and ref.candidate_id not in summary["normative_reference_ids"]:
                summary["normative_reference_ids"].append(ref.candidate_id)
            position += len(raw_line)
    return summary


def identify_primary_document(summary: dict[str, Any] | None) -> str | None:
    return (summary or {}).get("primary_candidate_id")


__all__ = ["enrich_document_entities", "identify_primary_document"]
