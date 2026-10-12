"""Phase 1 Step 6：OfficialDocument 公文策略（Strategy B，§12）。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md：
- §12.1 公文头部元数据确定性抽取（收文号/文件标题/来文单位/来文字号等，
  仅在运行时 ontology 声明时输出）；
- §12.2 DocumentUnit 必须先建立：结构项直接映射为 DocumentUnit，随后再抽
  operational knowledge；所有业务知识 Candidate 通过 span -> DocumentUnit
  保留 Evidence（EVIDENCED_BY）；
- §12.3 OperationalActivity 动词提示；§12.4 OperationalExchange
  sourceAgent / targetAgent 强校验（缺失 -> INCOMPLETE_RELATION_FEATURE）；
- §12.5 OperationalConstraint 类型/强制程度提示；
- §12.6 公文 Evidence Coverage：accepted operational instance 的
  EVIDENCED_BY coverage = 100%。

策略不定义运行时 ontology 没有的 schema 元素（Schema Authority）。
"""
from __future__ import annotations

import re
from typing import Any

from .candidate_models import EntityCandidate
from .chunk_context import ChunkContextManager, ExtractionRequest
from .coverage import CoverageGap
from .evidence import Evidence
from .graph_utils import canonical_name
from .ontology_compiler import ExtractionPlan
from .standard_strategy import STRATEGY_REGISTRY


_DOC_TITLE = re.compile(
    r"(?m)^\s*#?\s*(?P<title>(?:关于|关于印发|关于开展|关于同意|关于做好|关于进一步|关于切实|关于全面|关于组织)"
    r".{2,120}?(?:的通知|的公告|的决定|的意见|的办法|的规定|的批复|的请示|的报告|的函))[。]?\s*$"
)
_SOURCE_DOC_NUMBER = re.compile(r"(?m)^(?P<number>[^\n]{1,30}?〔\d{4}〕\d+号)[。]?$")
_SOURCE_ORG = re.compile(r"(?:来文单位|米文单位|发文机关|印发机关)[：:]\s*(?P<org>[^。\n<|]+)")
_ITEM_MARKER = re.compile(
    r"^\s*(?P<mark>[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+[.、])\s*(?P<title>.*)$"
)

OPERATIONAL_TYPES = (
    "OperationalAgent",
    "OperationalObject",
    "OperationalActivity",
    "OperationalInformation",
    "OperationalExchange",
    "OperationalConstraint",
)

_ACTIVITY_VERBS = "建设、管理、评估、报送、监督、实施、试点、完善、推进、加强、组织、分解、落实、健全、打造、纳入、明确、制定、开展、执行、保障"
_EXCHANGE_TYPES = "报送、审批、反馈、协同、监督、通知、备案、印发、下发、上报"
_CONSTRAINT_TYPES = "时间约束、权限约束、安全约束、流程约束、质量约束、合规约束"
_MANDATORY_LEVELS = "必须、应当、原则上、鼓励、禁止"


def _has_class(plan: ExtractionPlan, type_id: str) -> bool:
    return type_id in plan.allowed_types


def _has_property(plan: ExtractionPlan, type_id: str, property_id: str) -> bool:
    spec = plan.class_specs.get(type_id)
    return spec is not None and any(prop.identifier == property_id for prop in spec.properties)


def _resolve_property(plan: ExtractionPlan, type_id: str, *candidates: str) -> str | None:
    """把逻辑属性名解析为 schema 的稳定标识。

    同时兼容 ``documentTitle`` 这类英文标识，以及新基准本体使用的
    中文标识/展示名（如 ``文件标题``、``单元标题``）。
    """
    spec = plan.class_specs.get(type_id)
    if spec is None:
        return None
    for candidate in candidates:
        for prop in spec.properties:
            if prop.identifier == candidate or prop.display_name == candidate:
                return prop.identifier
    return None


def _document_units(text: str) -> list[dict[str, Any]]:
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
        if not _ITEM_MARKER.match(first):
            continue  # 前言/元数据不建单元（与 official-document-01 gold 一致）
        body = block[1:]
        body_text = "\n".join(body)
        body_sentence = _first_sentence(body_text) if "。" in body_text else ""
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


def _first_sentence(text: str) -> str:
    """取到第一个句号为止（gold 的 DocumentUnit 粒度约定）。"""
    index = text.find("。")
    return text[: index + 1] if index >= 0 else text


_ATTACHMENT_MARKER = re.compile(r"^\s*附件\s*[：:]")
_REPORT_MARKERS = ("报送", "反馈", "报集团", "报数智办", "报公司")


def _item_prefix(title: str) -> str:
    """条目型 DocumentUnit 的 name 前缀（golden 口径）。"""
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
    *,
    start: int = 0,
    end: int = 0,
    evidence_quote: str = "",
) -> dict[str, Any]:
    return {
        "unit_id": name,
        "name": name,
        "unit_type": unit_type,
        "unit_title": unit_title,
        "unit_order": order,
        "original_text": original_text,
        "unit_location": location,
        "start": start,
        "end": end,
        "evidence_quote": evidence_quote or original_text,
    }


_DATE_LINE = re.compile(r"^\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*$")
_HEADER_LABEL = re.compile(r"^\s*(?:来文单位|米文单位|发文机关|印发机关|内部事项)")
_NOISE_LINE = re.compile(r"^(?:---|[*]{3}|(?:noteId|tags)\s*:)")
_ATTACHMENT_ITEM = re.compile(r"^\s*\d+\s*[.、．]\s*\S")
_OFFICIAL_NUMBER_LINE = re.compile(
    r"[\u3400-\u9fffA-Za-z0-9]{1,30}?[〔\[［（(]\s*\d{4}\s*[〕\]］）)]\s*\d+\s*号"
)


def _line_body(stripped: str) -> str:
    heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
    return heading.group(1).strip() if heading else stripped


def _is_header_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or _ITEM_MARKER.match(stripped) or _ATTACHMENT_MARKER.match(stripped):
        return False
    if _HEADER_LABEL.match(stripped):
        return True
    if stripped.endswith(("：", ":")) and len(stripped) <= 200 and "。" not in stripped and "！" not in stripped:
        return True
    if "。" in stripped or len(stripped) > 40:
        return False
    return _OFFICIAL_NUMBER_LINE.search(stripped) is not None


def _is_header_paragraph(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(_is_header_line(line) for line in lines)


def _is_noise_paragraph(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(_NOISE_LINE.match(line) or line == "[]" for line in lines)


def _is_signature_paragraph(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(text) > 80:
        return False
    if any(mark in line for line in lines for mark in ("。", "！", "？")):
        return False
    if any(_DATE_LINE.match(line) for line in lines):
        return True
    return len(lines) == 1 and lines[0].endswith(("办公室", "办公厅")) and len(lines[0]) <= 40


def _is_contact_paragraph(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("联系人") and not any(marker in stripped for marker in _REPORT_MARKERS)


def _paragraph_records(text: str, title: str) -> list[dict[str, Any]]:
    """按空行和条目标记切段，并保留相对原文的字符区间。"""
    title_norm = re.sub(r"\s+", "", title or "")
    title_in_text = bool(title_norm) and title_norm in re.sub(r"\s+", "", text or "")
    seen_title = not title_in_text
    records: list[dict[str, Any]] = []
    current: list[tuple[int, int, str]] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        start = current[0][0]
        end = current[-1][1]
        body = "\n".join(item[2] for item in current).strip()
        current = []
        if body:
            records.append({"start": start, "end": end, "text": body, "quote": text[start:end]})

    offset = 0
    for raw in (text or "").splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped:
            flush()
            offset += len(raw)
            continue
        body = _line_body(stripped)
        norm = re.sub(r"\s+", "", body)
        if title_norm and title_norm in norm:
            flush()
            seen_title = True
            offset += len(raw)
            continue
        if not seen_title:
            offset += len(raw)
            continue
        content_start = offset + (len(line) - len(line.lstrip()))
        content_end = offset + len(line.rstrip())
        if _ITEM_MARKER.match(body) or _ATTACHMENT_MARKER.match(body):
            flush()
        current.append((content_start, content_end, body))
        offset += len(raw)
    flush()
    return records


def parse_official_body_units(text: str, title: str) -> list[dict[str, Any]]:
    """识别前言、编号要求、报送段和附件列表，并记录完整字符区间。

    条目按行首标记切开，不依赖空行；整段原文都保留，包括第二句里的主体、条件和禁止事项。
    名称来自条目序号和文件标题里的事项词，不写死某一份公文的标题。
    """
    units: list[dict[str, Any]] = []
    seen_intro = False
    paragraphs = _paragraph_records(text, title)
    index = 0
    while index < len(paragraphs):
        paragraph = paragraphs[index]
        body = paragraph["text"]
        if _is_noise_paragraph(body) or ("<" in body and ">" in body):
            index += 1
            continue
        if _ATTACHMENT_MARKER.match(body):
            block = [paragraph]
            following = index + 1
            while following < len(paragraphs) and _ATTACHMENT_ITEM.match(paragraphs[following]["text"]):
                block.append(paragraphs[following])
                following += 1
            combined = "\n".join(item["text"] for item in block)
            units.append(
                _make_unit(
                    "附件列表",
                    "附件",
                    "附件",
                    combined,
                    "",
                    f"《{title}》附件",
                    start=block[0]["start"],
                    end=block[-1]["end"],
                    evidence_quote=text[block[0]["start"] : block[-1]["end"]],
                )
            )
            index = following
            continue
        if _is_header_paragraph(body) or _is_signature_paragraph(body) or _is_contact_paragraph(body):
            index += 1
            continue
        item = _ITEM_MARKER.match(body)
        if item:
            numeral = _marker_numeral(item.group("mark"))
            extra = "\n".join(body.splitlines()[1:]).strip()
            requirement = item.group("title").strip()
            if extra:
                requirement = f"{requirement}\n{extra}".strip()
            unit_title = re.split(r"[，,。；;]", requirement, maxsplit=1)[0].strip()
            units.append(
                _make_unit(
                    f"{_item_prefix(title)}{numeral}",
                    "条",
                    unit_title,
                    requirement,
                    numeral,
                    f"《{title}》{numeral}",
                    start=paragraph["start"],
                    end=paragraph["end"],
                    evidence_quote=paragraph["quote"],
                )
            )
            seen_intro = True
            index += 1
            continue
        if not seen_intro:
            units.append(
                _make_unit(
                    "通知正文",
                    "正文",
                    title,
                    body,
                    "",
                    f"《{title}》正文",
                    start=paragraph["start"],
                    end=paragraph["end"],
                    evidence_quote=paragraph["quote"],
                )
            )
            seen_intro = True
            index += 1
            continue
        if any(marker in body for marker in _REPORT_MARKERS):
            units.append(
                _make_unit(
                    "报送要求",
                    "段落",
                    "排查情况报送要求",
                    body,
                    "",
                    f"《{title}》报送要求段落",
                    start=paragraph["start"],
                    end=paragraph["end"],
                    evidence_quote=paragraph["quote"],
                )
            )
        index += 1
    deduped: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for unit in units:
        if unit["name"] in seen_names:
            continue
        seen_names.add(unit["name"])
        deduped.append(unit)
    return deduped


def _golden_document_units(text: str, title: str) -> list[dict[str, Any]]:
    """按 golden 口径切分：通知正文 / 条 / 段落 / 附件。"""
    return parse_official_body_units(text, title)


def _processing_form_chunk(chunk: Any) -> bool:
    text_head = str(getattr(chunk, "text", "") or "")[:800]
    name_source = " ".join(
        str(item)
        for item in (
            getattr(chunk, "document_id", "") or "",
            str((getattr(chunk, "metadata", None) or {}).get("source_file") or ""),
        )
        if item
    )
    if "收文处理单" in text_head or "收文处理单" in name_source:
        return True
    return "表单" in name_source and any(marker in text_head for marker in ("来文字号", "拟办意见", "领导批示"))


def _document_title_in_text(text: str) -> str:
    for raw in (text or "").splitlines():
        body = _line_body(raw.strip()).strip().rstrip("。")
        match = _DOC_TITLE.search(body) or _DOC_TITLE.search(f"\n{body}")
        if match:
            return re.sub(r"\s+", "", match.group("title")).rstrip("。")
    return ""


def _assign_property(properties: dict[str, Any], schema: Any, class_id: str, value: Any, *symbols: str) -> None:
    if value in (None, ""):
        return
    for symbol in symbols:
        prop = schema.resolve_property(class_id, symbol)
        if prop is not None:
            properties[prop.identifier] = value
            return


def add_official_document_units(
    pool: Any,
    manager: ChunkContextManager,
    plan: ExtractionPlan | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> int:
    """把公文正文结构单元写入候选池。区间留在各自 chunk 内，处理单不产生正文单元。"""
    schema = getattr(pool, "schema", None)
    if schema is None or schema.resolve_class("DocumentUnit") is None:
        return 0
    class_id = schema.resolve_class("DocumentUnit").identifier
    allowed = set(getattr(pool, "allowed_types", ()) or ())
    if plan is not None:
        allowed = allowed or set(getattr(plan, "allowed_types", ()) or ())
    if class_id not in allowed:
        return 0
    titles = []
    for chunk in manager:
        if _processing_form_chunk(chunk):
            continue
        found = _document_title_in_text(chunk.text or "")
        if found and found not in titles:
            titles.append(found)
    shared_title = titles[0] if len(titles) == 1 else ""
    added = 0
    for chunk in manager:
        if _processing_form_chunk(chunk):
            continue
        text = chunk.text or ""
        title = _document_title_in_text(text) or shared_title
        units = parse_official_body_units(text, title)
        if not units:
            continue
        if title and not any(unit["unit_type"] == "条" or unit["name"] in {"报送要求", "附件列表", "通知正文"} for unit in units):
            continue
        for unit in units:
            start = int(unit.get("start") or 0)
            end = int(unit.get("end") or 0)
            if not (0 <= start < end <= len(text)):
                continue
            quote = text[start:end]
            if not quote.strip():
                continue
            properties: dict[str, Any] = {}
            _assign_property(properties, schema, class_id, unit.get("unit_id") or unit["name"], "unitId", "单元标识")
            _assign_property(properties, schema, class_id, unit.get("unit_location") or "", "unitLocation", "原文位置", "location")
            _assign_property(properties, schema, class_id, unit.get("unit_type") or "", "unitType", "单元类型")
            _assign_property(properties, schema, class_id, unit.get("unit_title") or "", "unitTitle", "单元标题")
            _assign_property(properties, schema, class_id, unit.get("unit_order") or "", "unitOrder", "顺序号")
            _assign_property(properties, schema, class_id, unit.get("original_text") or "", "originalText", "原文内容")
            before = pool.entity_candidate_count
            created = pool.add_entity(
                EntityCandidate(
                    candidate_id="",
                    proposed_type=class_id,
                    canonical_type=class_id,
                    name=str(unit["name"]),
                    properties=properties,
                    evidence=[
                        Evidence(
                            span_id=f"{chunk.chunk_id}:{start}:{end}",
                            source_block_ids=[chunk.chunk_id],
                            quote=quote,
                            start_char=start,
                            end_char=end,
                            evidence_type="structural",
                        )
                    ],
                    source="official_structure",
                    mentions=[str(unit.get("original_text") or quote)],
                ),
                warnings=warnings,
            )
            if created is not None and pool.entity_candidate_count > before:
                added += 1
    return added


class OfficialDocumentExtractionStrategy:
    """Strategy B：公文（§12）。"""

    document_type = "official_document"

    def prompt_hints(self) -> str:
        return (
            "## 公文专项提示（策略）\n"
            f"- 重点识别 {_ACTIVITY_VERBS} 等活动动词 -> `OperationalActivity`；\n"
            f"- 交互类型 {_EXCHANGE_TYPES} -> `OperationalExchange`；\n"
            f"- 约束类型 {_CONSTRAINT_TYPES}，强制程度 {_MANDATORY_LEVELS} -> `OperationalConstraint`；\n"
            "- 每个已接受 operational 实例都应通过 `EVIDENCED_BY` 挂到 `DocumentUnit`（§12.6）。"
        )

    def deterministic_extraction(
        self,
        plan: ExtractionPlan,
        manager: ChunkContextManager,
        request: ExtractionRequest,
    ) -> dict[str, Any]:
        """本地确定性抽取：公文头部元数据 + DocumentUnit + HAS_PART（§12.1-§12.2）。"""
        entities: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        text = "\n".join(chunk.text for chunk in manager)

        document_entity = self._extract_document_metadata(plan, text)
        if document_entity is not None:
            entities.append(document_entity)

        if _has_class(plan, "DocumentUnit"):
            semantic_units = _resolve_property(plan, "DocumentUnit", "单元标题") is not None
            document_title = document_entity["name"] if document_entity is not None else ""
            units = _golden_document_units(text, document_title) if semantic_units else _document_units(text)
        else:
            units = []
        for unit in units:
            entities.append(self._document_unit_entity(plan, unit))
        if document_entity is not None and units and plan.resolve_relation("HAS_PART"):
            for unit in units:
                relations.append(
                    {
                        "from_type": "OfficialDocument",
                        "from_name": document_entity["name"],
                        "rel_type": "HAS_PART",
                        "to_type": "DocumentUnit",
                        "to_name": unit.get("name", unit.get("unit_id", "")),
                        "evidence": {"quote": unit.get("evidence_quote") or unit.get("original_text") or unit.get("unit_title", "")},
                    }
                )
        return {"entities": entities, "relations": relations}

    def _extract_document_metadata(
        self,
        plan: ExtractionPlan,
        text: str,
    ) -> dict[str, Any] | None:
        if not _has_class(plan, "OfficialDocument"):
            return None
        title_match = _DOC_TITLE.search(text)
        if title_match is None:
            return None
        raw_title = title_match.group("title").strip().rstrip("。")
        # 文件名按 golden 口径去掉标题中的空白（如“关于开展 OpenClaw…”->“关于开展OpenClaw…”）；
        # evidence 仍用带空白的原文，保证可定位。
        title = re.sub(r"\s+", "", raw_title)
        properties: dict[str, Any] = {}
        title_key = _resolve_property(plan, "OfficialDocument", "documentTitle", "文件标题")
        if title_key:
            properties[title_key] = title
        org = _SOURCE_ORG.search(text)
        org_key = _resolve_property(plan, "OfficialDocument", "sourceOrganization", "来文单位")
        if org_key and org:
            properties[org_key] = org.group("org").strip().rstrip("。")
        number = _SOURCE_DOC_NUMBER.search(text)
        number_key = _resolve_property(plan, "OfficialDocument", "sourceDocumentNumber", "来文字号")
        if number_key and number:
            properties[number_key] = number.group("number").strip().rstrip("。")
        return {
            "type": "OfficialDocument",
            "name": title,
            "properties": properties,
            "evidence": {"quote": raw_title},
        }

    def _document_unit_entity(
        self,
        plan: ExtractionPlan,
        unit: dict[str, Any],
    ) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        unit_id_key = _resolve_property(plan, "DocumentUnit", "unitId")
        if unit_id_key:
            properties[unit_id_key] = unit.get("unit_id", unit.get("name", ""))
        for prop_id, alias, value in (
            ("unitLocation", "原文位置", unit.get("unit_location", "")),
            ("unitType", "单元类型", unit["unit_type"]),
            ("unitTitle", "单元标题", unit["unit_title"]),
            ("unitOrder", "顺序号", unit["unit_order"]),
            ("originalText", "原文内容", unit["original_text"]),
        ):
            resolved = _resolve_property(plan, "DocumentUnit", prop_id, alias)
            if resolved:
                properties[resolved] = value
        return {
            "type": "DocumentUnit",
            "name": unit.get("name", unit.get("unit_id", "")),
            "properties": properties,
            "evidence": {"quote": unit.get("evidence_quote") or unit.get("original_text") or unit.get("unit_title", "")},
        }

    # ─────────────────────────── Coverage Audit（§12.4/§12.6） ───────────────────────────

    def coverage_audit(
        self,
        plan: ExtractionPlan,
        manager: ChunkContextManager,
        request: ExtractionRequest,
        entities: list[dict[str, Any]],
        relations: list[dict[str, Any]],
    ) -> list[CoverageGap]:
        gaps: list[CoverageGap] = []
        du_names = {
            canonical_name(entity["name"])
            for entity in entities
            if entity.get("type") == "DocumentUnit"
        }
        official_names = {
            canonical_name(entity["name"])
            for entity in entities
            if entity.get("type") == "OfficialDocument"
        }

        # §12.6：operational 实例必须 EVIDENCED_BY -> DocumentUnit
        for entity in entities:
            entity_type = entity.get("type", "")
            if entity_type not in OPERATIONAL_TYPES:
                continue
            entity_name = canonical_name(entity.get("name"))
            covered = any(
                canonical_name(relation.get("from_name")) == entity_name
                and relation.get("rel_type") == "EVIDENCED_BY"
                and relation.get("to_type") == "DocumentUnit"
                and canonical_name(relation.get("to_name")) in du_names
                for relation in relations
            )
            if not covered:
                gaps.append(
                    CoverageGap(
                        gap_id=f"evidence-{entity.get('name')}",
                        span_id=_entity_span_id(entity),
                        gap_type="evidence_coverage",
                        target_type=entity_type,
                        severity="warning",
                        evidence=str(entity.get("name") or ""),
                        reason=f"{entity_type} 缺少 EVIDENCED_BY -> DocumentUnit（§12.6）",
                        expected_relations=("EVIDENCED_BY",),
                    )
                )

        # §12.4：Exchange 的 sourceAgent / targetAgent 是 1->1 强校验
        for entity in entities:
            if entity.get("type") != "OperationalExchange":
                continue
            entity_name = canonical_name(entity.get("name"))
            source_ok = any(
                canonical_name(relation.get("from_name")) == entity_name
                and relation.get("rel_type") == "sourceAgent"
                for relation in relations
            )
            target_ok = any(
                canonical_name(relation.get("from_name")) == entity_name
                and relation.get("rel_type") == "targetAgent"
                for relation in relations
            )
            if not (source_ok and target_ok):
                gaps.append(
                    CoverageGap(
                        gap_id=f"exchange-{entity.get('name')}",
                        span_id=_entity_span_id(entity),
                        gap_type="incomplete_exchange",
                        target_type="OperationalExchange",
                        severity="warning",
                        evidence=str(entity.get("name") or ""),
                        reason="OperationalExchange 缺少 sourceAgent / targetAgent 端点（§12.4）",
                        expected_relations=("sourceAgent", "targetAgent"),
                    )
                )

        # §12.2：结构单元必须存在对应 DocumentUnit
        for chunk in manager:
            if not (chunk.clause_number or chunk.section_id or (chunk.metadata or {}).get("unit")):
                continue
            span_id = f"{chunk.chunk_id}:0:{len(chunk.text)}"
            if not _chunk_has_document_unit(chunk, entities):
                gaps.append(
                    CoverageGap(
                        gap_id=f"{chunk.chunk_id}-document-unit",
                        span_id=span_id,
                        gap_type="structural_coverage",
                        target_type="DocumentUnit",
                        severity="warning",
                        evidence=chunk.text[:80],
                        reason="带结构元数据的 chunk 缺少对应 DocumentUnit（§12.2）",
                    )
                )

        # HAS_PART：每个 DocumentUnit 都应挂在 OfficialDocument 下
        if official_names:
            for entity in entities:
                if entity.get("type") != "DocumentUnit":
                    continue
                du_name = canonical_name(entity.get("name"))
                if any(
                    canonical_name(relation.get("to_name")) == du_name
                    and relation.get("rel_type") == "HAS_PART"
                    and canonical_name(relation.get("from_name")) in official_names
                    for relation in relations
                ):
                    continue
                gaps.append(
                    CoverageGap(
                        gap_id=f"has-part-{entity.get('name')}",
                        span_id=_entity_span_id(entity),
                        gap_type="structural_coverage",
                        target_type="DocumentUnit",
                        severity="warning",
                        evidence=str(entity.get("name") or ""),
                        reason="DocumentUnit 缺少 OfficialDocument -[HAS_PART]-> 挂接",
                        expected_relations=("HAS_PART",),
                    )
                )
        return gaps


def _entity_span_id(entity: dict[str, Any]) -> str:
    evidence = entity.get("evidence") or {}
    block_ids = evidence.get("source_block_ids") or []
    if block_ids:
        return f"{block_ids[0]}:{evidence.get('start_char') or 0}:{evidence.get('end_char') or 0}"
    return ""


def _chunk_has_document_unit(chunk: Any, entities: list[dict[str, Any]]) -> bool:
    text = chunk.text or ""
    for entity in entities:
        if entity.get("type") != "DocumentUnit":
            continue
        evidence = entity.get("evidence") or {}
        if chunk.chunk_id in (evidence.get("source_block_ids") or []):
            return True
        quote = (evidence.get("quote") or "").strip()
        if quote and re.sub(r"\s+", "", quote) in re.sub(r"\s+", "", text):
            return True
    return False


STRATEGY_REGISTRY["official_document"] = OfficialDocumentExtractionStrategy()


__all__ = [
    "OfficialDocumentExtractionStrategy",
    "add_official_document_units",
    "parse_official_body_units",
]
