"""Deterministic official-form structure extraction (form-derived agents)."""
from __future__ import annotations

import re
import unicodedata
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
# 确定性官方结构与元数据（对齐 golden：章 / 条 / 第N段 / 附件说明）
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


_CN_DIGITS = "一二三四五六七八九十"


def _cn_number(value: int) -> str:
    """阿拉伯数字 → 汉字数字（1..99），用于「工作目标第一条」这类模板名。"""
    if value < 1 or value > 99:
        return str(value)
    if value <= 10:
        return _CN_DIGITS[value - 1]
    if value < 20:
        return "十" + _CN_DIGITS[value - 11]
    tens, ones = divmod(value, 10)
    return _CN_DIGITS[tens - 1] + "十" + (_CN_DIGITS[ones - 1] if ones else "")


# 解析产物的正文行带 markdown 标题前缀（`# 一、工作目标`），匹配前需剥掉。
_MD_HEADING = re.compile(r"^\s*#{1,6}\s*")

# 版记/版头类噪声：抄送、联系人、印发说明等，金标不作为结构单元。
_NOISE_PREFIXES = ("抄送", "抄 送", "联系人", "主题词", "签发", "共印", "份数")
_NOISE_SUFFIXES = ("印发",)


def _strip_markdown_heading(line: str) -> str:
    """`# 一、工作目标` → `一、工作目标`。"""
    return _MD_HEADING.sub("", line).strip()


def _is_body_paragraph(text: str) -> bool:
    """判断一个块是否是正文自然段。

    正文段落须有句读；页码（`丨11`）、印发机关、成文日期、密级文号一类的版头版记
    既无句读也不是正文，金标里不建结构单元。
    """
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith(_NOISE_PREFIXES) or stripped.endswith(_NOISE_SUFFIXES):
        return False
    return bool(re.search(r"[，,。；;]", stripped))


# 表单区段起始标记：正文到此结束。解析产物常把正文与「收文处理单/领导批示」
# 拼在同一个 chunk 里，若不截断，批示文字（「已阅。」「请…阅」）会被误当成正文段落。
_FORM_SECTION_MARKERS = (
    "收文处理单",
    "领导批示",
    "拟办意见",
    "承办处理",
    "附件/相关文件",
    "关联文件",
    "收文号",
    "来文单位",
)


def _starts_form_section(line: str) -> bool:
    """该行是否开启表单区段（收文处理单 / 领导批示 / 拟办意见 …）。"""
    probe = unicodedata.normalize("NFKC", line).replace(" ", "")
    probe = probe.lstrip("|").replace("**", "").replace("__", "")
    return any(probe == marker or probe.startswith((marker + ":", marker + "|"))
               for marker in _FORM_SECTION_MARKERS)


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
    """按 golden 口径切分：章 / 条 / 第N段 / 附件说明。"""
    raw_lines = [_strip_markdown_heading(line) for line in text.splitlines()]
    if title:
        normalized = re.sub(r"\s+", "", title)
        for index, line in enumerate(raw_lines):
            if normalized and normalized in re.sub(r"\s+", "", line):
                raw_lines = raw_lines[index + 1:]
                break
    # 丢掉标题之后的主送行（以「：」结尾）及其间的空行
    while raw_lines and (not raw_lines[0] or raw_lines[0].endswith(("：", ":"))):
        raw_lines.pop(0)
    # 正文在表单区段（收文处理单 / 领导批示 / 拟办意见 …）处结束
    for index, line in enumerate(raw_lines):
        if _starts_form_section(line):
            raw_lines = raw_lines[:index]
            break

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
    paragraph = 0
    chapter = ""            # 当前章题名（`一、工作目标`），供章下无题名的条生成模板名
    chapter_numeral = ""
    for block in blocks:
        paragraph_text = "\n".join(block).strip()
        if not paragraph_text or ("<" in paragraph_text and ">" in paragraph_text):
            continue

        # 附件段 —— 无论附件项是否带编号，单元名统一取「附件说明」
        if _ATTACHMENT_MARKER.match(paragraph_text):
            units.append(
                _make_unit("附件说明", "附件", "附件说明", paragraph_text, "",
                           f"{title}#附件")
            )
            continue

        item = _ITEM_MARKER.match(paragraph_text)
        if item:
            mark = item.group(1)
            numeral = _marker_numeral(mark)
            inline = item.group(2).strip()
            is_arabic = bool(re.match(r"^\d", mark))

            # 章：只有标题、标题内无句读（`一、工作目标`），单独建为「章」单元
            if not is_arabic and not re.search(r"[，,。；;：:]", inline) and len(inline) <= 20:
                chapter = inline
                chapter_numeral = numeral
                units.append(
                    _make_unit(inline, "章", inline, paragraph_text, numeral,
                               f"{title}#{numeral}")
                )
                continue

            if is_arabic and chapter:
                # 章下无题名的条 → 「<章题名>第N条」
                name = f"{chapter}第{_cn_number(int(numeral))}条"
                units.append(
                    _make_unit(name, "条", name, paragraph_text, numeral,
                               f"{title}#{chapter_numeral}-{numeral}")
                )
                continue

            # 有编号且有题名的条 → 名称取题名（去掉编号前缀），逐字照抄
            unit_title = re.split(r"[，,。；;]", inline, maxsplit=1)[0].strip()
            if not unit_title:
                unit_title = inline or numeral
            units.append(
                _make_unit(unit_title, "条", unit_title, paragraph_text, numeral,
                           f"{title}#{numeral}")
            )
            continue

        # 无编号自然段 → 「第N段」。正文首段与后续段落同等处理，
        # 不再区分「通知正文」/「报送要求」（那套角色命名与本体及金标不一致）。
        if not _is_body_paragraph(paragraph_text):
            continue
        paragraph += 1
        name = f"第{paragraph}段"
        units.append(
            _make_unit(name, "段落", name, paragraph_text, str(paragraph),
                       f"{title}#段落{paragraph}")
        )
    # 正文以 《X》(见附件) 方式引用附件但没有独立附件块时，补一个附件说明单元。
    if not any(unit.get("unit_type") == "附件" for unit in units):
        reference = re.search(r"《([^《》]{2,80})》[^。]{0,12}见附件", text)
        if reference:
            units.append(
                _make_unit(
                    "附件说明",
                    "附件",
                    "附件说明",
                    f"附件：{reference.group(1)}",
                    "",
                    f"{title}#附件",
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
    # 注意：`单元标识` 按本体是**非抽取字段**（系统分配，不参与抽取），
    # 且金标不含该属性，因此这里不产出，避免用单元名去填充它。
    units = _golden_document_units(text, title) if name_key is not None else _plain_document_units(text)
    entities: list[dict[str, Any]] = []
    for unit in units:
        properties: dict[str, Any] = {}
        mapping = (
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
