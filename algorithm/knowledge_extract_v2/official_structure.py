"""Deterministic official-form structure extraction (form-derived agents)."""
from __future__ import annotations

import re
from typing import Any, Iterable

from .candidate_models import EntityCandidate
from .evidence import Evidence
from .official_document_scope import official_role, operational_type_ids

_FORM_ORG_FIELD = re.compile(
    r"(?:\u6765\u6587\u5355\u4f4d|\u53d1\u6587\u673a\u5173|\u5370\u53d1\u673a\u5173)\s*"
    r"[\uff1a:]\s*([^\u3002\n<|\uff1a:]{2,40})"
)
_FORM_DUTY_REQUEST = re.compile(
    r"\u8bf7\s*([\u4e00-\u9fffA-Za-z0-9\uff08\uff09()\u00b7]{2,20}?)\s*"
    r"(?:\u7275\u5934|\u9605\u529e|\u534f\u540c|\u843d\u5b9e|\u529e\u7406|\u8d1f\u8d23|\u7ec4\u7ec7|\u914d\u5408|\u63a8\u8fdb)"
)
_FORM_DUTY_ENUM = re.compile(
    r"(?:\u660e\u786e\u843d\u5b9e|\u843d\u5b9e\u5230|\u7531)\s*"
    r"([\u4e00-\u9fffA-Za-z0-9\uff08\uff09()\u00b7\u3001]{2,40}?)"
    r"(?:\u76f8\u5173\u5de5\u4f5c\u627f\u62c5\u90e8\u95e8|\u627f\u62c5\u76f8\u5173\u5de5\u4f5c|\u8d1f\u8d23)"
)

_ORG_SUFFIX = re.compile(r"(?:公司|集团|中心|部门|部|局|院|所|处|科|室|组|委|会|办|厅|署|站|队|厂|校|机构|单位|平台)$")


def _property_symbol(plan: Any, class_id: str, *candidates: str) -> str | None:
    spec = plan.class_specs.get(class_id)
    if spec is None:
        return None
    for candidate in candidates:
        for prop in spec.properties:
            if str(prop.identifier) == candidate or str(prop.display_name) == candidate:
                return str(prop.identifier)
    return None


def build_official_form_agents(plan: Any, chunks: Iterable[Any]) -> list[dict[str, Any]]:
    """Return deterministic OperationalAgent entities evidenced by official forms."""
    if "OperationalAgent" not in operational_type_ids(plan):
        return []
    name_prop = _property_symbol(plan, "OperationalAgent", "主体名称", "subjectName", "name")
    entities: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _emit(chunk: Any, start: int, end: int, raw_name: str) -> None:
        name = raw_name.strip().strip("。；;，,、")
        if not name or name in seen:
            return
        seen.add(name)
        properties = {name_prop: name} if name_prop else {}
        entities.append({
            "type": "OperationalAgent",
            "name": name,
            "properties": properties,
            "chunk_id": chunk.chunk_id,
            "start": start,
            "end": end,
            "quote": (chunk.text or "")[start:end],
        })

    for chunk in chunks:
        if official_role(plan, chunk) != "form":
            continue
        text = chunk.text or ""
        for match in _FORM_ORG_FIELD.finditer(text):
            _emit(chunk, match.start(1), match.end(1), match.group(1))
        for match in _FORM_DUTY_REQUEST.finditer(text):
            name = match.group(1).strip()
            if _ORG_SUFFIX.search(name):
                _emit(chunk, match.start(1), match.end(1), name)
        for match in _FORM_DUTY_ENUM.finditer(text):
            for part in re.split(r"[、,，]", match.group(1)):
                part = part.strip()
                if not _ORG_SUFFIX.search(part):
                    continue
                local = match.group(1).find(part)
                if local < 0:
                    continue
                start = match.start(1) + local
                _emit(chunk, start, start + len(part), part)
    return entities


def add_official_form_agents(
    pool: Any,
    plan: Any,
    entities: list[dict[str, Any]],
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Add deterministic form-derived agents to the candidate pool."""
    added = 0
    for item in entities:
        chunk_id = str(item.get("chunk_id") or "")
        start = int(item.get("start") or 0)
        end = int(item.get("end") or 0)
        candidate = EntityCandidate(
            candidate_id="",
            proposed_type=str(item.get("type") or ""),
            canonical_type=str(item.get("type") or ""),
            name=str(item.get("name") or ""),
            properties=dict(item.get("properties") or {}),
            evidence=[Evidence(
                span_id=f"{chunk_id}:{start}:{end}",
                source_block_ids=[chunk_id],
                quote=str(item.get("quote") or item.get("name") or ""),
                start_char=start,
                end_char=end,
                evidence_type="structural",
            )],
            source="official_form_structure",
        )
        if pool.add_entity(candidate, warnings=warnings) is not None:
            added += 1
    return {"entities": added}


# ---------------------------------------------------------------------------
# 确定性官方结构与元数据（对齐 golden：通知正文 / 排查要求一..N / 报送要求 / 附件说明）
# ---------------------------------------------------------------------------

_DOC_TITLE = re.compile(
    "^\\s*#?\\s*(?P<title>(?:关于|关于印发|关于开展|关于同意|关于做好|关于进一步|关于切实|关于全面|关于组织)"
    ".{2,120}?(?:的通知|的公告|的决定|的意见|的办法|的规定|的批复|的请示|的报告|的函))[。]?\\s*$",
    re.M,
)
_SOURCE_DOC_NUMBER = re.compile("^[^\\n]{1,30}?〔\\d{4}〕\\d+号$", re.M)
_SOURCE_ORG = re.compile("(?:来文单位|米文单位|发文机关|印发机关)[：:]\\s*([^。\\n<|]+)")
_ITEM_MARKER = re.compile("^\\s*([一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\\d+[.、])\\s*(.*)$", re.M)
_ATTACHMENT_MARKER = re.compile("^\\s*附件\\s*[：:]")
_REPORT_MARKERS = ("报送", "反馈", "报集团", "报数智办", "报公司")


def _first_sentence(text: str) -> str:
    index = text.find("。")
    return text[: index + 1] if index >= 0 else text


def _item_prefix(title: str) -> str:
    if "排查" in title:
        return "排查要求"
    if "工作要点" in title:
        return "工作要点"
    return "要求"


def _marker_numeral(mark: str) -> str:
    return mark.strip().strip("（()）").rstrip("、.．").strip()


def _make_unit(
    name: str,
    unit_type: str,
    unit_title: str,
    original_text: str,
    order: str,
    location: str,
) -> dict[str, Any]:
    return {
        "unit_id": name,
        "name": name,
        "unit_type": unit_type,
        "unit_title": unit_title,
        "unit_order": order,
        "original_text": original_text,
        "unit_location": location,
    }


def _plain_document_units(text: str) -> list[dict[str, Any]]:
    """按条目标记（一、/（一）/1.）切分结构单元；跳过前言/头部元数据。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if _ITEM_MARKER.match(line) and current:
            blocks.append(current)
            current = []
        current.append(line)
    if current:
        blocks.append(current)

    units: list[dict[str, Any]] = []
    for block in blocks:
        first = block[0]
        item = _ITEM_MARKER.match(first)
        if item is None:
            continue
        body = "\n".join(block[1:]).strip()
        body_sentence = _first_sentence(body) if "。" in body else ""
        if body_sentence:
            unit_title = first
            original_text = body_sentence
        else:
            unit_title = _first_sentence(first)
            original_text = unit_title
        units.append(
            {
                "unit_id": f"DU-{len(units) + 1:03d}",
                "unit_type": "条",
                "unit_title": unit_title,
                "unit_order": len(units) + 1,
                "original_text": original_text,
            }
        )
    return units


def _golden_document_units(text: str, title: str) -> list[dict[str, Any]]:
    """按 golden 口径切分：通知正文 / 条 / 段落 / 附件说明。"""
    raw_lines = [line.strip() for line in text.splitlines()]
    if title:
        normalized = re.sub(r"\s+", "", title)
        for index, line in enumerate(raw_lines):
            if normalized and normalized in re.sub(r"\s+", "", line):
                raw_lines = raw_lines[index + 1:]
                break
    while raw_lines and raw_lines[0].endswith(("：", ":")):
        raw_lines.pop(0)

    blocks: list[list[str]] = []
    current: list[str] = []
    in_attachment = False
    for raw in raw_lines:
        line = raw.strip()
        if not line:
            if current and not in_attachment:
                blocks.append(current)
                current = []
            continue
        if _ATTACHMENT_MARKER.match(line):
            if current:
                blocks.append(current)
                current = []
            in_attachment = True
            current.append(line)
            continue
        if in_attachment:
            if re.match(r"^\d+\s*[.、]", line):
                current.append(line)
                continue
            blocks.append(current)
            current = []
            in_attachment = False
        if _ITEM_MARKER.match(line):
            if current:
                blocks.append(current)
                current = []
            current.append(line)
            continue
        current.append(line)
    if current:
        blocks.append(current)

    units: list[dict[str, Any]] = []
    seen_intro = False
    for block in blocks:
        paragraph = "\n".join(block).strip()
        if not paragraph or ("<" in paragraph and ">" in paragraph):
            continue
        if _ATTACHMENT_MARKER.match(paragraph):
            name = "附件列表" if re.search(r"\d+\s*[.、]", paragraph) else "附件说明"
            units.append(_make_unit(name, "附件说明", "附件", paragraph, "", f"《{title}》附件"))
            continue
        item = _ITEM_MARKER.match(paragraph)
        if item:
            numeral = _marker_numeral(item.group(1))
            body = item.group(2).strip()
            unit_title = re.split(r"[，,。；;]", body, maxsplit=1)[0].strip()
            units.append(
                _make_unit(
                    f"{_item_prefix(title)}{numeral}",
                    "条",
                    unit_title,
                    _first_sentence(body),
                    numeral,
                    f"《{title}》{numeral}",
                )
            )
            seen_intro = True
            continue
        if not seen_intro:
            units.append(_make_unit("通知正文", "正文", title, paragraph, "", f"《{title}》正文"))
            seen_intro = True
            continue
        if any(marker in paragraph for marker in _REPORT_MARKERS):
            units.append(_make_unit("报送要求", "段落", "排查情况报送要求", paragraph, "", f"《{title}》报送要求段落"))
    # 正文以 《X》(见附件) 方式引用附件但没有独立附件块时，补一个附件说明单元。
    if not any(unit.get("unit_type") == "附件说明" for unit in units):
        reference = re.search(r"《([^《》]{2,80})》[^。]{0,12}见附件", text)
        if reference:
            units.append(
                _make_unit(
                    "附件说明",
                    "附件说明",
                    "附件",
                    f"附件：{reference.group(1)}",
                    "",
                    f"《{title}》附件",
                )
            )

    deduped: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for unit in units:
        if unit["name"] in seen_names:
            continue
        seen_names.add(unit["name"])
        deduped.append(unit)
    return deduped


def _official_document_entity(plan: Any, chunk: Any, text: str) -> dict[str, Any] | None:
    """确定性抽取公文管理单元（文件标题/来文字号/来文单位）。"""
    title_match = _DOC_TITLE.search(text)
    if title_match is None:
        return None
    raw_title = str(title_match.group("title")).strip().rstrip("。")
    title = re.sub(r"\s+", "", raw_title)
    properties: dict[str, Any] = {}
    title_key = _property_symbol(plan, "OfficialDocument", "文件标题", "documentTitle", "title")
    if title_key:
        properties[title_key] = title
    org = _SOURCE_ORG.search(text)
    org_key = _property_symbol(plan, "OfficialDocument", "来文单位", "sourceOrganization")
    if org_key and org:
        properties[org_key] = org.group(1).strip().rstrip("。")
    number = _SOURCE_DOC_NUMBER.search(text)
    number_key = _property_symbol(plan, "OfficialDocument", "来文字号", "sourceDocumentNumber")
    if number_key and number:
        properties[number_key] = number.group(0).strip().rstrip("。")
    return {
        "type": "OfficialDocument",
        "name": title,
        "properties": properties,
        "chunk_id": chunk.chunk_id,
        "start": title_match.start(0),
        "end": title_match.end(0),
        "quote": raw_title,
    }


def _document_unit_entities(plan: Any, chunk: Any, text: str, title: str) -> list[dict[str, Any]]:
    if "DocumentUnit" not in plan.allowed_types:
        return []
    name_key = _property_symbol(plan, "DocumentUnit", "单元标题", "unitTitle")
    type_key = _property_symbol(plan, "DocumentUnit", "单元类型", "unitType")
    body_key = _property_symbol(plan, "DocumentUnit", "原文内容", "originalText")
    order_key = _property_symbol(plan, "DocumentUnit", "顺序号", "unitOrder")
    location_key = _property_symbol(plan, "DocumentUnit", "原文位置", "unitLocation")
    id_key = _property_symbol(plan, "DocumentUnit", "单元标识", "unitId")
    units = _golden_document_units(text, title) if name_key is not None else _plain_document_units(text)
    entities: list[dict[str, Any]] = []
    for unit in units:
        properties: dict[str, Any] = {}
        mapping = (
            (id_key, unit.get("unit_id", unit.get("name", ""))),
            (type_key, unit.get("unit_type", "")),
            (name_key, unit.get("unit_title", "")),
            (body_key, unit.get("original_text", "")),
            (order_key, unit.get("unit_order", "")),
            (location_key, unit.get("unit_location", "")),
        )
        for key, value in mapping:
            if key:
                properties[key] = value
        quote = str(unit.get("original_text") or unit.get("unit_title") or unit.get("name") or "")
        start = text.find(quote) if quote else -1
        if start < 0:
            quote = str(unit.get("unit_title") or unit.get("name") or "")
            start = text.find(quote) if quote else -1
        start = max(start, 0)
        entities.append({
            "type": "DocumentUnit",
            "name": str(unit.get("name") or unit.get("unit_id") or ""),
            "properties": properties,
            "chunk_id": chunk.chunk_id,
            "start": start,
            "end": start + len(quote),
            "quote": quote,
        })
    return entities


def build_official_structure_entities(plan: Any, chunks: Iterable[Any]) -> list[dict[str, Any]]:
    """构建确定性的公文管理单元与结构单元实体（只取正文 chunk）。"""
    body_chunks = [chunk for chunk in chunks if official_role(plan, chunk) == "body"]
    if not body_chunks:
        return []
    chunk = max(body_chunks, key=lambda item: len(item.text or ""))
    text = chunk.text or ""
    entities: list[dict[str, Any]] = []
    document = _official_document_entity(plan, chunk, text)
    title = str(document["name"]) if document else ""
    if document is not None:
        entities.append(document)
    entities.extend(_document_unit_entities(plan, chunk, text, title))
    return entities


def add_official_structure_entities(
    pool: Any,
    plan: Any,
    entities: list[dict[str, Any]],
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """把确定性公文结构实体加入候选池。"""
    added = 0
    for item in entities:
        chunk_id = str(item.get("chunk_id") or "")
        start = int(item.get("start") or 0)
        end = int(item.get("end") or 0)
        candidate = EntityCandidate(
            candidate_id="",
            proposed_type=str(item.get("type") or ""),
            canonical_type=str(item.get("type") or ""),
            name=str(item.get("name") or ""),
            properties=dict(item.get("properties") or {}),
            evidence=[Evidence(
                span_id=f"{chunk_id}:{start}:{end}",
                source_block_ids=[chunk_id],
                quote=str(item.get("quote") or item.get("name") or ""),
                start_char=start,
                end_char=end,
                evidence_type="structural",
            )],
            source="official_structure",
        )
        if pool.add_entity(candidate, warnings=warnings) is not None:
            added += 1
    return {"entities": added}


__all__ = [
    "build_official_form_agents",
    "add_official_form_agents",
    "build_official_structure_entities",
    "add_official_structure_entities",
]
