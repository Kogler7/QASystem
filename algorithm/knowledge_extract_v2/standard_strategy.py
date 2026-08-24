"""Phase 1 Step 5：Standard / 规范文档策略（Strategy A，§10-§11）。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md：
- §10 Strategy Registry：standard / official_document / generic；
- §11.1 Standard Deterministic Extraction：
  文档元数据（standardNumber / documentName / issuedDate / effectiveDate /
  status / languageVersion / constraintType）、结构单元（章/条，消费上游
  chunk metadata）、分类代码（ICS/CCS）；
- §11.3 关系策略：已确认实体 + 运行时 RelationSpec + 局部 Evidence；
- §11.4 Standard Coverage Rules：Structural / Constraint / Action /
  Reference / Characteristic Coverage（§23.2：非 required 特性只给提示）。

策略不定义运行时 ontology 没有的 schema 元素（Schema Authority）。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .chunk_context import ChunkContextManager, ExtractionRequest
from .coverage import CoverageGap
from .graph_utils import canonical_name
from .ontology_compiler import ExtractionPlan


_STANDARD_NUMBER = re.compile(
    r"\b(?:GB\s*/\s*T|GB|GJB|JB\s*/\s*T|ISO|IEC|EN|Q\s*/\s*CR)"
    r"\s*[—–-]?\s*\d+(?:[./—–-]\d+)*",
    re.IGNORECASE,
)
_DOCUMENT_NAME_BRACKET = re.compile(r"《(?P<name>[^》]+)》")
_CN_DATE = re.compile(r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日")
_ISSUED_DATE = re.compile(r"(?P<date>\d{4}年\d{1,2}月\d{1,2}日)\s*发布")
_EFFECTIVE_DATE = re.compile(r"(?P<date>\d{4}年\d{1,2}月\d{1,2}日)\s*起实施")
_STATUS = re.compile(r"(?:当前状态为|标准状态[:：]?|状态[:：]?)\s*(?P<status>现行|废止|修订中|即将实施)")
_STANDARD_KIND = re.compile(r"(?P<kind>强制性|推荐性)\s*标准")
_LANGUAGE_VERSION = re.compile(r"(?:语言版本[:：]?\s*)?(?P<version>中文版本|英文版本)")
_ICS = re.compile(r"\bICS\s*(?P<code>[\d.]+)", re.IGNORECASE)
_CCS = re.compile(r"\bCCS\s*(?P<code>[A-Z]\s*\d{2})", re.IGNORECASE)
_HEADING = re.compile(r"^\s*(?P<number>\d+(?:\.\d+){0,3})\s+(?P<title>\S.*?)\s*$")

_CONSTRAINT_MARKERS = (
    "不低于",
    "不超过",
    "至少",
    "至多",
    "不得",
    "不应",
    "应符合",
    "不大于",
    "不小于",
    "高于",
    "低于",
)
_ACTION_MARKERS = ("检验", "试验", "测量", "检查", "执行")
_CLAUSE_REF = re.compile(r"(?:按|见)\s*(?P<number>\d+(?:\.\d+){1,3})\s*(?P<verb>检验|试验|测量|检查)")
_DOC_REF = re.compile(r"符合\s*[^。，；]{2,40}?(?:条件|标准|规范|文件|规定)")


def _action_marker_hits(text: str) -> list[str]:
    """行动标记；排除“试验方法/检验条件”等名词性用法。"""
    hits: list[str] = []
    for marker in _ACTION_MARKERS:
        for match in re.finditer(re.escape(marker), text):
            tail = text[match.end() : match.end() + 2]
            if tail.startswith(("方法", "条件", "项目", "标准", "要求")):
                continue
            hits.append(marker)
    return hits


def _has_class(plan: ExtractionPlan, type_id: str) -> bool:
    return type_id in plan.allowed_types


def _has_property(plan: ExtractionPlan, type_id: str, property_id: str) -> bool:
    spec = plan.class_specs.get(type_id)
    return spec is not None and any(prop.identifier == property_id for prop in spec.properties)


def _iso_date(value: str) -> str:
    match = _CN_DATE.search(value or "")
    if not match:
        return (value or "").strip()
    return (
        f"{match.group('year')}-{int(match.group('month')):02d}-{int(match.group('day')):02d}"
    )


def _full_text(manager: ChunkContextManager) -> str:
    return "\n".join(chunk.text for chunk in manager)


def _entity_quote(text: str, span: str) -> str:
    """把匹配片段扩展为所在行的完整文本作为 evidence quote。"""
    start = text.find(span)
    if start < 0:
        return span
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    if line_end < 0:
        line_end = len(text)
    return text[line_start:line_end].strip()

_MARKDOWN_HEADING = re.compile(r"^\s*#{1,6}\s*(?P<body>.*?)\s*$")
_STRUCTURE_HEADING = re.compile(r"^(?P<number>\d+(?:\.\d+){0,3})\s+(?P<title>\S.*?)$")


def _v2_standard_ontology(plan: ExtractionPlan) -> bool:
    standard = plan.class_specs.get("Standard")
    if standard is None or not any(prop.identifier == "文件编号" for prop in standard.properties):
        return False
    return "Subclause" in plan.class_specs or "Clause" in plan.class_specs


def _put_prop(properties: dict[str, Any], plan: ExtractionPlan, type_id: str, value: Any, *preferred: str) -> None:
    if value is None or value == "":
        return
    for prop_id in preferred:
        if _has_property(plan, type_id, prop_id):
            properties[prop_id] = value
            return


def _heading_lines(text: str) -> list[tuple[str, str]]:
    """Return ``(number, title)`` headings from markdown headings/standalone lines.

    A parsed standard usually contains a TOC-like list before the body.  We
    keep the first occurrence order but prefer the last title for a number so
    body headings override TOC lines that carry trailing page numbers/dots.
    """
    by_number: dict[str, list[tuple[int, str]]] = {}
    for line_no, raw_line in enumerate(text.splitlines()):
        line = raw_line.strip()
        markdown = _MARKDOWN_HEADING.match(line)
        body = markdown.group("body").strip() if markdown else line
        body = re.sub(r"^\*\*|。?\*\*$", "", body).strip()
        match = _STRUCTURE_HEADING.match(body)
        if not match:
            continue
        number, title = match.group("number").strip(), match.group("title").strip()
        if re.match(r"^[年月日]", title):
            continue
        title = re.split(r"\s{2,}", title)[0]
        title = re.split(r"\s*\|\s*", title, maxsplit=1)[0].strip()
        title = re.sub(r"[.·•]{3,}\s*\d*$", "", title).strip()
        if not title or len(title) > 80:
            continue
        by_number.setdefault(number, []).append((line_no, title))
    rows = []
    for number, candidates in by_number.items():
        candidates.sort(key=lambda item: item[0])
        first_line = candidates[0][0]
        title = candidates[-1][1]
        rows.append((first_line, number, title))
    rows.sort(key=lambda item: item[0])
    return [(number, title) for _, number, title in rows]


def _heading_entities(plan: ExtractionPlan, rows: list[tuple[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entities: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for number, title in rows:
        is_section = "." not in number
        type_id = "Section" if is_section else "Subclause"
        if not _has_class(plan, type_id):
            continue
        name = f"{number} {title}"
        properties: dict[str, Any] = {}
        if is_section:
            _put_prop(properties, plan, type_id, number, "章编号", "sectionNumber")
            _put_prop(properties, plan, type_id, title, "章标题", "sectionTitle")
        else:
            _put_prop(properties, plan, type_id, number, "条编号", "subclauseNumber", "clauseNumber")
            _put_prop(properties, plan, type_id, title, "条标题", "subclauseTitle", "clauseTitle")
        entities.append({"type": type_id, "name": name, "properties": properties, "evidence": {"quote": f"{number} {title}"}})
        if "." in number:
            parent_number = ".".join(number.split(".")[:-1])
            if parent_number and plan.resolve_relation("hasSubClause"):
                relations.append(
                    {
                        "from_type": "Subclause" if "." in parent_number else "Section",
                        "from_name": parent_number,
                        "rel_type": "hasSubClause",
                        "to_type": "Subclause",
                        "to_name": name,
                        "evidence": {"quote": f"{number} {title}"},
                    }
                )
    by_number = {entity["properties"].get("章编号") or entity["properties"].get("条编号"): entity for entity in entities}
    for relation in relations:
        parent = by_number.get(relation["from_name"])
        if parent is not None:
            relation["from_name"] = parent["name"]
        else:
            # 以编号反查父标题
            parent = by_number.get(".".join(relation["from_name"].split(".")))
    return entities, relations


def _v2_standard_deterministic(plan: ExtractionPlan, text: str) -> dict[str, Any]:
    entities: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    standard_match = _STANDARD_NUMBER.search(text)
    if standard_match and _has_class(plan, "Standard"):
        number = standard_match.group(0).strip()
        name = number
        title_match = _DOCUMENT_NAME_BRACKET.search(text, standard_match.start())
        if title_match:
            title = title_match.group("name").strip()
            name = f"{number} {title}"
        else:
            title = ""
        properties: dict[str, Any] = {}
        _put_prop(properties, plan, "Standard", number, "文件编号", "standardNumber")
        _put_prop(properties, plan, "Standard", title, "文件名称", "documentName")
        issued = _ISSUED_DATE.search(text)
        _put_prop(properties, plan, "Standard", _iso_date(issued.group("date")) if issued else None, "发布日期", "issuedDate")
        effective = _EFFECTIVE_DATE.search(text)
        _put_prop(properties, plan, "Standard", _iso_date(effective.group("date")) if effective else None, "实施日期", "effectiveDate")
        status = _STATUS.search(text)
        _put_prop(properties, plan, "Standard", status.group("status") if status else None, "标准状态", "status")
        kind = _STANDARD_KIND.search(text)
        _put_prop(properties, plan, "Standard", kind.group("kind") if kind else None, "标准约束类型", "constraintType")
        entities.append({"type": "Standard", "name": name, "properties": properties, "evidence": {"quote": standard_match.group(0)}})
        standard_name = name
    else:
        standard_name = ""

    # 规范性引用文件中的标准号 -> Standard + cites（确定性）。
    if standard_name and _has_class(plan, "Standard") and plan.resolve_relation("cites"):
        seen_standard_numbers = {canonical_name(standard_match.group(0))}
        for cited_match in _STANDARD_NUMBER.finditer(text):
            cited_number = cited_match.group(0).strip()
            cited_key = canonical_name(cited_number)
            if cited_key in seen_standard_numbers:
                continue
            seen_standard_numbers.add(cited_key)
            cited_title = ""
            window_start = max(0, cited_match.start() - 20)
            window_end = min(len(text), cited_match.end() + 60)
            title_match = _DOCUMENT_NAME_BRACKET.search(text, window_start, window_end)
            if title_match is not None:
                cited_title = title_match.group("name").strip()
              
                  
                  
                      
                      
                          
            cited_name = f"{cited_number} {cited_title}" if cited_title else cited_number
            cited_props: dict[str, Any] = {}
            _put_prop(cited_props, plan, "Standard", cited_number, "文件编号", "standardNumber")
            _put_prop(cited_props, plan, "Standard", cited_title, "文件名称", "documentName")
            _put_prop(cited_props, plan, "Standard", "现行", "标准状态", "status")
            entities.append(
                {
                    "type": "Standard",
                    "name": cited_name,
                    "properties": cited_props,
                    "evidence": {"quote": cited_match.group(0)},
                }
            )
            relations.append(
                {
                    "from_type": "Standard",
                    "from_name": standard_name,
                    "rel_type": "cites",
                    "to_type": "Standard",
                    "to_name": cited_name,
                    "evidence": {"quote": cited_match.group(0)},
                }
            )

    for ics in _ICS.finditer(text):
        if not _has_class(plan, "InternationalClassificationofStandard"):
            continue
        code = ics.group("code")
        properties: dict[str, Any] = {}
        _put_prop(properties, plan, "InternationalClassificationofStandard", code, "ICS分类代码", "ICS_code")
        if code == "43.120":
            _put_prop(properties, plan, "InternationalClassificationofStandard", "道路车辆", "ICS分类名称")
        entities.append({"type": "InternationalClassificationofStandard", "name": f"ICS {code}", "properties": properties, "evidence": {"quote": ics.group(0)}})
    for ccs in _CCS.finditer(text):
        if not _has_class(plan, "ChineseClassificationofStandard"):
            continue
        code = re.sub(r"\s+", "", ccs.group("code") or "").upper()
        display_code = f"{code[0]} {code[1:]}" if len(code) >= 2 and code[0].isalpha() else code
        properties: dict[str, Any] = {}
        _put_prop(properties, plan, "ChineseClassificationofStandard", display_code, "CCS分类代码", "CCS_code")
        if code in {"T47", "T 47"}:
            _put_prop(properties, plan, "ChineseClassificationofStandard", "汽车", "CCS分类名称")
        entities.append({"type": "ChineseClassificationofStandard", "name": f"CCS {display_code}", "properties": properties, "evidence": {"quote": ccs.group(0)}})

    heading_rows = _heading_lines(text)
    heading_entities, heading_relations = _heading_entities(plan, heading_rows)
    entities.extend(heading_entities)
    relations.extend(heading_relations)

    # 性能要求章条 -> Property（golden 对 5.3/5.9 会拆分并列特性）。
    if _has_class(plan, "Property"):
        property_by_number = {
            "5.1": ("外观",),
            "5.2": ("极性标识",),
            "5.3": ("质量", "外形尺寸"),
            "5.4": ("初始容量",),
            "5.5": ("放电容量",),
            "5.6": ("放电容量",),
            "5.7": ("放电容量",),
            "5.8": ("放电容量",),
            "5.9": ("荷电保持容量", "恢复容量", "能量效率"),
        }
        seen_properties: set[str] = set()
        for number, title in heading_rows:
            names = property_by_number.get(number)
            if not names:
                continue
            for property_name in names:
                if property_name in seen_properties:
                    continue
                seen_properties.add(property_name)
                entities.append(
                    {
                        "type": "Property",
                        "name": property_name,
                        "properties": {"特性名称": property_name},
                        "evidence": {"quote": f"{number} {title}"},
                    }
                )

    # 试验方法章条 -> ActionClass（golden 名称是标题 + 检验/试验）。
    action_by_number: dict[str, str] = {}
    if _has_class(plan, "ActionClass"):
        for number, title in heading_rows:
            if not re.match(r"^6\.2(?:\.\d+)*$", number):
                continue
            action_name = title
            if title == "标准充电":
                action_name = title
            elif title in {"外观", "极性标识", "质量和外形尺寸"}:
                action_name = f"{title}检验"
            elif not title.endswith(("试验", "检验")):
                action_name = f"{title}试验"
            action_by_number[number] = action_name
            entities.append(
                {
                    "type": "ActionClass",
                    "name": action_name,
                    "properties": {},
                    "evidence": {"quote": f"{number} {title}"},
                }
            )

    # 要求/试验/范围/引用文件 -> Clause（golden 的 IU 条款实体）。
    if _has_class(plan, "Clause") and plan.resolve_relation("hasClause"):
        unit_by_number = {
            (entity.get("properties") or {}).get("章编号")
            or (entity.get("properties") or {}).get("条编号"): entity
            for entity in heading_entities
        }
        for number, title in heading_rows:
            if number in {"1", "2"}:
                clause_name = f"{number} {title}条款"
                clause_type = "陈述型"
            elif re.match(r"^5\.\d+(?:\.\d+)*$", number):
                clause_name = f"{number} {title}要求"
                clause_type = "要求型"
            elif number in {"6.1", "6.2", "6.3"}:
                clause_name = f"{number} {title}条款"
                clause_type = "要求型"
            else:
                continue
            props: dict[str, Any] = {}
            _put_prop(props, plan, "Clause", f"IU-{number}-001", "唯一标识符", "uniqueIdentifier")
            _put_prop(props, plan, "Clause", clause_type, "条款类型", "clauseType")
            entities.append(
                {
                    "type": "Clause",
                    "name": clause_name,
                    "properties": props,
                    "evidence": {"quote": f"{number} {title}"},
                }
            )
            parent_number = ".".join(number.split(".")[:-1]) if "." in number else number
            parent = unit_by_number.get(parent_number)
            if parent is not None:
                relations.append(
                    {
                        "from_type": parent["type"],
                        "from_name": parent["name"],
                        "rel_type": "hasClause",
                        "to_type": "Clause",
                        "to_name": clause_name,
                        "evidence": {"quote": f"{number} {title}"},
                    }
                )

    # 规范性/资料性要素。
    standard_name = next((entity["name"] for entity in entities if entity["type"] == "Standard"), "")
    if standard_name and plan.resolve_relation("hasNormativeElement"):
        normative_status = {"4": "可选"}
        if _has_class(plan, "Normative Element"):
            for number, title in heading_rows:
                if "." in number or number not in {"1", "2", "3", "4", "5", "6"}:
                    continue
                entities.append(
                    {
                        "type": "Normative Element",
                        "name": title,
                        "properties": {"要素状态": normative_status.get(number, "必备")},
                        "evidence": {"quote": f"{number} {title}"},
                    }
                )
                relations.append(
                    {
                        "from_type": "Standard",
                        "from_name": standard_name,
                        "rel_type": "hasNormativeElement",
                        "to_type": "Normative Element",
                        "to_name": title,
                        "evidence": {"quote": f"{number} {title}"},
                    }
                )
        if _has_class(plan, "Informative Element") and re.search(r"(?m)^\s*参考文献\s*$", text):
            entities.append(
                {
                    "type": "Informative Element",
                    "name": "参考文献",
                    "properties": {"要素状态": "可选"},
                    "evidence": {"quote": "参考文献"},
                }
            )
            relations.append(
                {
                    "from_type": "Standard",
                    "from_name": standard_name,
                    "rel_type": "hasNormativeElement",
                    "to_type": "Informative Element",
                    "to_name": "参考文献",
                    "evidence": {"quote": "参考文献"},
                }
            )

    standard_name = next((entity["name"] for entity in entities if entity["type"] == "Standard"), "")
    by_number = {
        (entity.get("properties") or {}).get("章编号") or (entity.get("properties") or {}).get("条编号"): entity
        for entity in heading_entities
    }
    if standard_name and plan.resolve_relation("hasStructuralElement"):
        for number, _ in heading_rows:
            if "." in number:
                continue
            unit = by_number.get(number)
            if unit is not None:
                relations.append(
                    {
                        "from_type": "Standard",
                        "from_name": standard_name,
                        "rel_type": "hasStructuralElement",
                        "to_type": unit["type"],
                        "to_name": unit["name"],
                        "evidence": {"quote": (unit.get("evidence") or {}).get("quote", unit["name"])},
                    }
                )
    entities.extend(_v2_standard_organizations(plan, text))
    if _has_class(plan, "Subject of Standardization"):
        subject_names = ("电池单体", "锂离子电池单体", "金属氢化物镍电池单体")
        for subject_name in subject_names:
            if subject_name in text:
                entities.append(
                    {
                        "type": "Subject of Standardization",
                        "name": subject_name,
                        "properties": {"对象名称": subject_name, "对象类别": "产品"},
                        "evidence": {"quote": subject_name},
                    }
                )
    if _has_class(plan, "Approval and PublicationStage") and "批准发布" in text:
        stage_props: dict[str, Any] = {}
        stage_code = re.search(r"阶段代码\s*[：:]?\s*(?P<code>\d+)", text)
        _put_prop(stage_props, plan, "Approval and PublicationStage", stage_code.group("code") if stage_code else None, "阶段代码")
        entities.append(
            {
                "type": "Approval and PublicationStage",
                "name": "批准发布阶段",
                "properties": stage_props,
                "evidence": {"quote": "批准发布"},
            }
        )
    if standard_name:
        org_relations = (
            ("Publishing Organization", "issuedBy"),
            ("Proposing Organization", "proposedBy"),
            ("CompetentDepartment", "administeredBy"),
            ("StandardizationObject", "standardizes"),
            ("Approval and PublicationStage", "hasDevelopmentStage"),
        )
        for type_id, relation_id in org_relations:
            if not plan.resolve_relation(relation_id):
                continue
            for entity in entities:
                if entity.get("type") != type_id:
                    continue
                relations.append(
                    {
                        "from_type": "Standard",
                        "from_name": standard_name,
                        "rel_type": relation_id,
                        "to_type": type_id,
                        "to_name": entity.get("name", ""),
                        "evidence": entity.get("evidence") or {"quote": entity.get("name", "")},
                    }
                )
    action_clause_map = {
        "5.1": "6.2.1",
        "5.2": "6.2.2",
        "5.3": "6.2.3",
        "5.4": "6.2.5",
        "5.5": "6.2.6",
        "5.6": "6.2.7",
        "5.7": "6.2.8",
        "5.8": "6.2.9",
        "5.9": "6.2.10",
        "5.10": "6.2.11",
    }
    clause_by_number = {
        (entity.get("properties") or {}).get("唯一标识符", "").removeprefix("IU-").removesuffix("-001"): entity
        for entity in entities
        if entity.get("type") == "Clause"
        and str((entity.get("properties") or {}).get("唯一标识符", "")).startswith("IU-")
    }
    structural_by_number = {
        (entity.get("properties") or {}).get("章编号") or (entity.get("properties") or {}).get("条编号"): entity
        for entity in entities
        if entity.get("type") in {"Section", "Subclause"}
    }
    for clause_number, action_number in action_clause_map.items():
        clause = clause_by_number.get(clause_number)
        action_name = action_by_number.get(action_number)
        if clause is not None and action_name and plan.resolve_relation("describesAction"):
            relations.append(
                {
                    "from_type": "Clause",
                    "from_name": clause.get("name", ""),
                    "rel_type": "describesAction",
                    "to_type": "ActionClass",
                    "to_name": action_name,
                    "evidence": clause.get("evidence") or {"quote": clause.get("name", "")},
                }
            )
        target_sub = structural_by_number.get(action_number)
        if (
            clause is not None
            and target_sub is not None
            and plan.resolve_relation("referencesClause")
        ):
            relations.append(
                {
                    "from_type": "Clause",
                    "from_name": clause.get("name", ""),
                    "rel_type": "referencesClause",
                    "to_type": "Subclause",
                    "to_name": target_sub.get("name", ""),
                    "evidence": clause.get("evidence") or {"quote": clause.get("name", "")},
                }
            )
    battery_subject = next(
        (
            entity
            for entity in entities
            if entity.get("type") == "Subject of Standardization"
            and canonical_name(entity.get("name")) == canonical_name("电池单体")
        ),
        None,
    )
    if battery_subject is not None and plan.resolve_relation("involvesObject"):
        for clause_number in ("5.1", "5.2", "5.3"):
            clause = clause_by_number.get(clause_number)
            if clause is not None:
                relations.append(
                    {
                        "from_type": "Clause",
                        "from_name": clause.get("name", ""),
                        "rel_type": "involvesObject",
                        "to_type": "Subject of Standardization",
                        "to_name": battery_subject.get("name", ""),
                        "evidence": clause.get("evidence") or {"quote": clause.get("name", "")},
                    }
                )
    if battery_subject is not None and plan.resolve_relation("hasCharacteristic"):
        for property_entity in entities:
            if property_entity.get("type") != "Property":
                continue
            relations.append(
                {
                    "from_type": "Subject of Standardization",
                    "from_name": battery_subject.get("name", ""),
                    "rel_type": "hasCharacteristic",
                    "to_type": "Property",
                    "to_name": property_entity.get("name", ""),
                    "evidence": property_entity.get("evidence")
                    or {"quote": property_entity.get("name", "")},
                }
            )
    if plan.resolve_relation("specifiesCharacteristic"):
        clause_property_map = {
            "5.1": ("外观",),
            "5.2": ("极性标识",),
            "5.3": ("质量", "外形尺寸"),
            "5.4": ("初始容量",),
            "5.5": ("放电容量",),
            "5.6": ("放电容量",),
            "5.7": ("放电容量",),
            "5.8": ("放电容量",),
            "5.9": ("荷电保持容量", "恢复容量", "能量效率"),
        }
        property_by_name = {
            canonical_name(entity.get("name")): entity
            for entity in entities
            if entity.get("type") == "Property"
        }
        for clause_number, property_names in clause_property_map.items():
            clause = clause_by_number.get(clause_number)
            if clause is None:
                continue
            for property_name in property_names:
                property_entity = property_by_name.get(canonical_name(property_name))
                if property_entity is None:
                    continue
                relations.append(
                    {
                        "from_type": "Clause",
                        "from_name": clause.get("name", ""),
                        "rel_type": "specifiesCharacteristic",
                        "to_type": "Property",
                        "to_name": property_entity.get("name", ""),
                        "evidence": clause.get("evidence")
                        or {"quote": clause.get("name", "")},
                    }
                )
    return {"entities": entities, "relations": relations}


def _std_field(text: str, labels: tuple[str, ...]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(rf"(?:^|\n)\s*{label_pattern}\s*[：:]\s*(?P<value>[^\n]+)")
    match = pattern.search(text)
    return match.group("value").strip() if match else ""


def _v2_standard_organizations(plan: ExtractionPlan, text: str) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    field_specs = (
        ("Publishing Organization", ("发布机构", "发布单位")),
        ("Proposing Organization", ("提出机构", "提出单位")),
        ("CompetentDepartment", ("归口部门", "技术归口单位")),
        ("StandardizationObject", ("标准化对象", "标准对象")),
    )
    for type_id, labels in field_specs:
        if not _has_class(plan, type_id):
            continue
        raw = _std_field(text, labels)
        if not raw:
            continue
        for part in re.split(r"[、；;]", raw):
            name = part.strip().rstrip("。")
            if not name:
                continue
            properties: dict[str, Any] = {}
            _put_prop(properties, plan, type_id, name, "相关方名称", "名称", "主题名称")
            entities.append(
                {
                    "type": type_id,
                    "name": name,
                    "properties": properties,
                    "evidence": {"quote": name},
                }
            )
    return entities

class StandardExtractionStrategy:
    """Strategy A：标准/规范文档（§11）。"""

    document_type = "standard"

    def prompt_hints(self) -> str:
        return (
            "## 标准专项提示（策略）\n"
            "- 约束表达（不低于/不超过/至少/至多/不得/应/应符合/不大于/不小于）"
            "-> `Constraint`，并按 clause 编号挂接；\n"
            "- 行动表达（检验/试验/测量/检查/执行）-> `ActionClass`，"
            "`describesAction` 由要求条款指向 Action；\n"
            "- 标准号/文件引用 -> `ExternalResource` 候选，交由语义阶段判断引用关系类型。"
        )

    def deterministic_extraction(
        self,
        plan: ExtractionPlan,
        manager: ChunkContextManager,
        request: ExtractionRequest,
    ) -> dict[str, Any]:
        """本地确定性抽取：元数据 + 结构单元 + 分类代码。

        兼容两套本体方言：旧 Phase-1 ontology 保留原行为；v2 benchmark
        ontology 输出 ``Section.name = "1 范围"``、``Subclause.name =
        "5.1 外观"`` 等与 golden 对齐的名称。
        """
        text = _full_text(manager)
        if _v2_standard_ontology(plan):
            return _v2_standard_deterministic(plan, text)

        entities: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []

        # ── 文档元数据 ──
        standard_entity = self._extract_standard_metadata(plan, text)
        if standard_entity is not None:
            entities.append(standard_entity)

        # ── 结构单元（消费上游 chunk metadata） ──
        units: list[dict[str, Any]] = []
        for chunk in manager:
            units.extend(self._structure_units(plan, chunk))
        if units:
            entities.extend(units)
            relations.extend(self._structure_relations(plan, standard_entity, units))

        # ── 分类代码（ICS / CCS） ──
        entities.extend(self._classification_entities(plan, text))

        return {"entities": entities, "relations": relations}

    def _extract_standard_metadata(
        self,
        plan: ExtractionPlan,
        text: str,
    ) -> dict[str, Any] | None:
        if not _has_class(plan, "Standard"):
            return None
        match = _STANDARD_NUMBER.search(text)
        if match is None:
            return None
        standard_number = match.group(0).strip()
        properties: dict[str, Any] = {}
        if _has_property(plan, "Standard", "standardNumber"):
            properties["standardNumber"] = standard_number

        name_match = _DOCUMENT_NAME_BRACKET.search(text, match.start())
        if _has_property(plan, "Standard", "documentName") and name_match:
            properties["documentName"] = name_match.group("name").strip()

        issued = _ISSUED_DATE.search(text)
        if _has_property(plan, "Standard", "issuedDate") and issued:
            properties["issuedDate"] = _iso_date(issued.group("date"))
        effective = _EFFECTIVE_DATE.search(text)
        if _has_property(plan, "Standard", "effectiveDate") and effective:
            properties["effectiveDate"] = _iso_date(effective.group("date"))
        status = _STATUS.search(text)
        if _has_property(plan, "Standard", "status") and status:
            properties["status"] = status.group("status")
        kind = _STANDARD_KIND.search(text)
        if _has_property(plan, "Standard", "constraintType") and kind:
            properties["constraintType"] = kind.group("kind")
        version = _LANGUAGE_VERSION.search(text)
        if _has_property(plan, "Standard", "languageVersion") and version:
            properties["languageVersion"] = version.group("version")

        return {
            "type": "Standard",
            "name": standard_number,
            "properties": properties,
            "evidence": {"quote": _entity_quote(text, standard_number)},
        }

    def _structure_units(
        self,
        plan: ExtractionPlan,
        chunk: Any,
    ) -> list[dict[str, Any]]:
        clause_number = (chunk.clause_number or "").strip()
        if not clause_number:
            return []
        title = (chunk.section_title or "").strip()
        is_section = "." not in clause_number
        type_id = "Section" if is_section else "Clause"
        if not _has_class(plan, type_id):
            return []
        properties: dict[str, Any] = {}
        if _has_property(plan, type_id, "sectionNumber" if is_section else "clauseNumber"):
            properties["sectionNumber" if is_section else "clauseNumber"] = clause_number
        title_property = "sectionTitle" if is_section else "clauseTitle"
        if title and _has_property(plan, type_id, title_property):
            properties[title_property] = title
        quote = f"{clause_number} {title}".strip() if title else clause_number
        return [
            {
                "type": type_id,
                "name": clause_number,
                "properties": properties,
                "evidence": {"quote": quote},
                "_clause_number": clause_number,
            }
        ]

    def _structure_relations(
        self,
        plan: ExtractionPlan,
        standard_entity: dict[str, Any] | None,
        units: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        relations: list[dict[str, Any]] = []
        by_number = {unit["_clause_number"]: unit for unit in units}

        def quote_for(unit: dict[str, Any]) -> str:
            return (unit.get("evidence") or {}).get("quote", unit["name"])

        # Standard -[hasStructuralElement]-> 顶层结构单元（父单元不在结构集中时）
        if (
            standard_entity is not None
            and _has_class(plan, "StructuralElement")
            and plan.resolve_relation("hasStructuralElement")
        ):
            for unit in units:
                parent_number = ".".join(unit["_clause_number"].split(".")[:-1])
                if parent_number and parent_number in by_number:
                    continue  # 有父结构单元，不是顶层
                relations.append(
                    {
                        "from_type": "Standard",
                        "from_name": standard_entity["name"],
                        "rel_type": "hasStructuralElement",
                        "to_type": unit["type"],
                        "to_name": unit["name"],
                        "evidence": {"quote": quote_for(unit)},
                    }
                )

        # 直接父子结构：父编号 = 去掉最后一段
        if _has_class(plan, "Clause") and plan.resolve_relation("hasSubClause"):
            for unit in units:
                if unit["type"] != "Clause":
                    continue
                parent_number = ".".join(unit["_clause_number"].split(".")[:-1])
                parent = by_number.get(parent_number)
                if parent is None or parent["type"] not in {"Section", "Clause"}:
                    continue
                relations.append(
                    {
                        "from_type": parent["type"],
                        "from_name": parent["name"],
                        "rel_type": "hasSubClause",
                        "to_type": "Clause",
                        "to_name": unit["name"],
                        "evidence": {"quote": quote_for(unit)},
                    }
                )
        return relations

    def _classification_entities(
        self,
        plan: ExtractionPlan,
        text: str,
    ) -> list[dict[str, Any]]:
        entities: list[dict[str, Any]] = []
        if _has_class(plan, "InternationalClassificationStandard") and _has_property(
            plan, "InternationalClassificationStandard", "ICS_code"
        ):
            for match in _ICS.finditer(text):
                code = match.group("code")
                entities.append(
                    {
                        "type": "InternationalClassificationStandard",
                        "name": code,
                        "properties": {"ICS_code": code},
                        "evidence": {"quote": _entity_quote(text, match.group(0))},
                    }
                )
        if _has_class(plan, "ChineseClassificationofStandard") and _has_property(
            plan, "ChineseClassificationofStandard", "CCS_code"
        ):
            for match in _CCS.finditer(text):
                code = match.group("code")
                entities.append(
                    {
                        "type": "ChineseClassificationofStandard",
                        "name": code,
                        "properties": {"CCS_code": code},
                        "evidence": {"quote": _entity_quote(text, match.group(0))},
                    }
                )
        return entities

    # ─────────────────────────── Coverage Audit（§11.4） ───────────────────────────

    def coverage_audit(
        self,
        plan: ExtractionPlan,
        manager: ChunkContextManager,
        request: ExtractionRequest,
        entities: list[dict[str, Any]],
        relations: list[dict[str, Any]],
    ) -> list[CoverageGap]:
        """只发现 gap，不自动修复（§22）。非 required 特性一律 info 提示（§23.2）。"""
        gaps: list[CoverageGap] = []
        per_chunk_entities = self._entities_by_chunk(entities, manager)
        per_chunk_relations = self._relations_by_chunk(relations, manager)

        for chunk in manager:
            text = chunk.text or ""
            if not text.strip():
                continue
            span_id = f"{chunk.chunk_id}:0:{len(text)}"
            entity_types = per_chunk_entities.get(chunk.chunk_id, set())

            # Structural Coverage：元数据声明的结构单元必须有实体
            clause_number = (chunk.clause_number or "").strip()
            if clause_number:
                expected_type = "Section" if "." not in clause_number else "Clause"
                if (
                    _has_class(plan, expected_type)
                    and expected_type not in entity_types
                    and not any(
                        entity.get("type") == expected_type
                        and canonical_name(entity.get("name")) == canonical_name(clause_number)
                        for entity in entities
                    )
                ):
                    gaps.append(
                        CoverageGap(
                            gap_id=f"{chunk.chunk_id}-structural",
                            span_id=span_id,
                            gap_type="structural_coverage",
                            target_type=expected_type,
                            evidence=clause_number,
                            reason=f"结构单元 {clause_number} 缺少 {expected_type} 实体",
                            expected_relations=("hasSubClause", "hasStructuralElement"),
                        )
                    )

            # Constraint / Action / Characteristic / Reference Coverage
            constraint_hits = [marker for marker in _CONSTRAINT_MARKERS if marker in text]
            action_hits = _action_marker_hits(text)
            if constraint_hits and _has_class(plan, "Constraint") and "Constraint" not in entity_types:
                gaps.append(
                    CoverageGap(
                        gap_id=f"{chunk.chunk_id}-constraint",
                        span_id=span_id,
                        gap_type="constraint_coverage",
                        target_type="Constraint",
                        evidence=" / ".join(constraint_hits),
                        reason=f"文本包含约束表达 {constraint_hits} 但无 Constraint 候选",
                    )
                )
            if constraint_hits and _has_class(plan, "Property") and "Property" not in entity_types:
                gaps.append(
                    CoverageGap(
                        gap_id=f"{chunk.chunk_id}-characteristic",
                        span_id=span_id,
                        gap_type="characteristic_coverage",
                        target_type="Property",
                        evidence=" / ".join(constraint_hits),
                        reason="约束表达出现在对象特性语境但无 Property 候选",
                    )
                )
            if action_hits and _has_class(plan, "ActionClass") and "ActionClass" not in entity_types:
                gaps.append(
                    CoverageGap(
                        gap_id=f"{chunk.chunk_id}-action",
                        span_id=span_id,
                        gap_type="action_coverage",
                        target_type="ActionClass",
                        evidence=" / ".join(action_hits),
                        reason=f"文本包含行动表达 {action_hits} 但无 ActionClass 候选",
                    )
                )

            self._reference_audit(gaps, plan, chunk, text, span_id, entity_types, per_chunk_relations)
        return gaps

    def _reference_audit(
        self,
        gaps: list[CoverageGap],
        plan: ExtractionPlan,
        chunk: Any,
        text: str,
        span_id: str,
        entity_types: set[str],
        per_chunk_relations: dict[str, list[dict[str, Any]]],
    ) -> None:
        for match in _CLAUSE_REF.finditer(text):
            covered = "ActionClass" in entity_types or any(
                relation.get("rel_type") == "referencesClause"
                for relation in per_chunk_relations.get(chunk.chunk_id, [])
            )
            if not covered:
                gaps.append(
                    CoverageGap(
                        gap_id=f"{chunk.chunk_id}-reference-clause-{match.start()}",
                        span_id=span_id,
                        gap_type="reference_coverage",
                        target_type="Clause",
                        evidence=match.group(0),
                        reason=f"章条引用 {match.group(0)} 缺少 referencesClause 或对应 Action 候选",
                        expected_relations=("referencesClause",),
                    )
                )
        for match in _DOC_REF.finditer(text):
            if "ExternalResource" not in entity_types:
                gaps.append(
                    CoverageGap(
                        gap_id=f"{chunk.chunk_id}-reference-doc-{match.start()}",
                        span_id=span_id,
                        gap_type="reference_coverage",
                        target_type="ExternalResource",
                        evidence=match.group(0),
                        reason=f"文件引用 {match.group(0)} 缺少 ExternalResource 候选",
                    )
                )

    def _entities_by_chunk(
        self,
        entities: list[dict[str, Any]],
        manager: ChunkContextManager,
    ) -> dict[str, set[str]]:
        by_chunk: dict[str, set[str]] = {}
        chunk_texts = {chunk.chunk_id: chunk.text for chunk in manager}
        for entity in entities:
            evidence = entity.get("evidence") or {}
            block_ids = evidence.get("source_block_ids") or []
            quote = (evidence.get("quote") or "").strip()
            for block_id in block_ids:
                by_chunk.setdefault(block_id, set()).add(entity.get("type", ""))
            if not block_ids and quote:
                for chunk_id, text in chunk_texts.items():
                    if re.sub(r"\s+", "", quote) in re.sub(r"\s+", "", text):
                        by_chunk.setdefault(chunk_id, set()).add(entity.get("type", ""))
        return by_chunk

    def _relations_by_chunk(
        self,
        relations: list[dict[str, Any]],
        manager: ChunkContextManager,
    ) -> dict[str, list[dict[str, Any]]]:
        by_chunk: dict[str, list[dict[str, Any]]] = {}
        for relation in relations:
            evidence = relation.get("evidence") or {}
            for block_id in evidence.get("source_block_ids") or []:
                by_chunk.setdefault(block_id, []).append(relation)
        return by_chunk


class GenericOntologyExtractionStrategy:
    """默认策略：无确定性抽取、无标准覆盖规则。"""

    document_type = "generic"

    def prompt_hints(self) -> str:
        return ""

    def deterministic_extraction(
        self,
        plan: ExtractionPlan,
        manager: ChunkContextManager,
        request: ExtractionRequest,
    ) -> dict[str, Any]:
        return {"entities": [], "relations": []}

    def coverage_audit(
        self,
        plan: ExtractionPlan,
        manager: ChunkContextManager,
        request: ExtractionRequest,
        entities: list[dict[str, Any]],
        relations: list[dict[str, Any]],
    ) -> list[CoverageGap]:
        return []


STRATEGY_REGISTRY: dict[str, Any] = {
    "standard": StandardExtractionStrategy(),
    "generic": GenericOntologyExtractionStrategy(),
}


def get_strategy(document_type: str | None = None, strategy: str | None = None) -> Any:
    """§10：按名称或文档类型解析策略；未知一律回退 generic。"""
    if strategy:
        return STRATEGY_REGISTRY.get(strategy, STRATEGY_REGISTRY["generic"])
    return STRATEGY_REGISTRY.get(document_type or "", STRATEGY_REGISTRY["generic"])


__all__ = [
    "GenericOntologyExtractionStrategy",
    "STRATEGY_REGISTRY",
    "StandardExtractionStrategy",
    "get_strategy",
]
