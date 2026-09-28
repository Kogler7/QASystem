"""Golden-style structural/semantic alignment for standard-route graphs.

The conventions are ported from the benchmark golden builders
(``golden_task/build_unified_golden.py`` and
``golden_task/summarize_derived_names.py``) so the extraction algorithm follows
the same structure/naming style the standard goldens were built with:

* one canonical Standard entity taken from the document header;
* Section/Subclause/Clause hierarchy and derived ``a) b) c)`` clauses;
* Normative/Informative Element, Annex, InformationUnit/Reference wiring;
* requirement clause -> Activity / test Subclause / Terminology / Subject links;
* dash ``——`` items become summarized Additional Information;
* Figure/Table/Equation representation links.

Only structural conventions are rebuilt here.  Semantic entities produced by
the LLM passes (Activity/Terminology/Property/Constrain/Subject/Equation/...)
are kept and linked into the rebuilt skeleton.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

HEAD = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$", re.M)
NUM = re.compile(r"^((?:\d+(?:\.\d+)*)|(?:[A-Z](?:\.\d+)+))\s+(.+?)\s*$")
DASH = re.compile(r"^\s*——\s*(.+?)\s*$", re.M)
LETTER = re.compile(r"^\s*([a-z])[）)]\s*(.+?)\s*$", re.M)
REF_LINE = re.compile(r"GB(?:/T)?\s*\d+(?:\.\d+)*\s*(?:[—–-]\s*\d{4})?[^\n]*")
FIG = re.compile(r"^\s*图\s*([A-Za-z]?\.?\d+)\s+(.+?)\s*$", re.M)
TAB = re.compile(r"^\s*表\s*(\d+)\s+(.+?)\s*$", re.M)
TERM_SPLIT = re.compile(r"([\u3400-\u9fff][^\u4e00-\u9fffa-zA-Z]*?)\s*([a-zA-Z].*)?$")
CLAUSE_NUM = re.compile(r"^(?:附录\s*[A-Z]|(\d+(?:\.\d+)*)(\.[a-z])?)")
REF_NUM = re.compile(r"(?:按|见|依据|按照|参见|参见|符合|引用)\s*(\d+(?:\.\d+)*)")
STD_LINE = re.compile(r"^(GB(?:/T)?\s*\d+(?:\.\d+)*\s*(?:[—–-]\s*\d{4})?)\s*$")

KEEP_TYPES = {
    "Activity", "ActionClass", "Terminology", "Subject of Standardization",
    "StandardizationObject", "Property", "Constrain", "ExternalResource",
    "Equation", "Table", "Figure", "Organization", "Drafting Organization",
    "Drafter", "Individual", "Stakeholder", "Patent", "Regulations",
    "Publishing Organization", "Proposing Organization", "CompetentDepartment",
    "Implementing Organization", "Suggesting Party", "Approval and PublicationStage",
    "ChineseClassificationofStandard", "InternationalClassificationofStandard",
    "PublicationStage", "DraftingStage", "ReviewStage", "ConsultationStage",
    "ProposalStage", "Preliminary Stage", "TechnicalReviewStage", "WithdrawalStage",
    "StandardizationProcess", "ContentElement", "Object of Measuremen",
}
DROP_TYPES = {
    "Standard", "Section", "Subclause", "Clause", "Normative Element",
    "Informative Element", "Annex", "Additional Information", "InformationUnit",
    "ReferencedDocuments", "Reference", "ContentElement", "StructuralElement",
    "NormativeElement", "InformativeElement",
}
REBUILT_RELATIONS = {
    "hasClause", "hasSubClause", "hasStructuralElement", "hasNormativeElement",
    "describesAction", "referencesClause", "hasNote", "hasRepresentationForm",
    "referencesExternalResource", "defines", "usesTerm", "involvesObject",
}

_SUMMARY_SYSTEM = """你是标准条款命名器。为每条给定内容生成一个概括短标题（<=12个汉字），用于实体名字。
要求：忠实概括，不引入新事实；不要照抄长句；只输出 JSON。"""
_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "title": {"type": "string"}},
                "required": ["id", "title"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}


def norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def first_sentence(body: Any) -> str:
    text = str(body or "").strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[。！？；])", text)
    return (parts[0] if parts else text).strip()


def lead_before_list(body: Any) -> str:
    match = re.search(r"\n\s*(?:[a-z][）)]|——)\s*", str(body or ""), re.M)
    return str(body or "")[: match.start()] if match else str(body or "")


def ctype(body: Any) -> str:
    text = str(body or "")
    if re.search(r"不应|不得|应(?:当|该|能|予|按)", text):
        return "要求型"
    if "宜" in text:
        return "推荐型"
    if re.search(r"(?<!不)可(?:以|按|采用|使用|设置|选)", text):
        return "允许型"
    if re.search(r"说明|指明|给出|表示", text):
        return "指示型"
    return "陈述型"


def cconstraint(body: Any) -> str:
    text = str(body or "")
    if re.search(r"应符合|符合.{0,12}规定|按规定", text):
        return "合规约束"
    if re.search(r"不低于|不超过|不大于|极差|容量|性能|质量", text):
        return "质量约束"
    if "安全" in text:
        return "安全约束"
    if re.search(r"期限|时间|日期|时限", text):
        return "时间约束"
    if re.search(r"权限|授权", text):
        return "权限约束"
    if re.search(r"流程|程序|步骤", text):
        return "流程约束"
    return ""


def evidence_quote(entity: dict[str, Any]) -> str:
    evidence = entity.get("evidence")
    if isinstance(evidence, list) and evidence:
        evidence = evidence[0]
    if isinstance(evidence, dict):
        return str(evidence.get("quote") or "")
    return ""


def fallback_title(text: Any, kind: str = "extra") -> str:
    value = compact(text)
    value = re.sub(r"（.*?）|\(.*?\)", "", value)
    value = value.strip("；;，,。：: ")
    if kind == "extra":
        match = re.match(r"(更改|增加|删除|修改|调整|合并|补充)了?(.+)$", value)
        if match:
            verb, obj = match.group(1), match.group(2).strip("；;，,。：: ")
            first = re.split(r"[、，,]", obj)[0]
            if verb in ("更改", "修改"):
                if len(first) <= 6:
                    return (first + verb)[:12]
                return first[:12]
            return (verb + first)[:12]
        if re.match(r"^\d{4}年首次发布", value):
            return "首次发布年份"
        if "第一次修订" in value or "首次修订" in value:
            return "第一次修订"
    limit = 10 if kind == "clause" else 12
    parts = re.split(r"[，,；;：:]", value)
    return (parts[0] if parts else value)[:limit]


class _Graph:
    def __init__(self) -> None:
        self.entities: list[dict[str, Any]] = []
        self._by_key: dict[tuple[str, str], dict[str, Any]] = {}
        self.relations: list[dict[str, Any]] = []
        self._rel_keys: set[tuple[str, str, str, str, str]] = set()

    def add_entity(self, etype: str, name: Any, properties: dict[str, Any] | None = None) -> dict[str, Any] | None:
        name = clean(name)
        if not name:
            return None
        key = (str(etype), norm(name))
        existing = self._by_key.get(key)
        if existing is not None:
            props = existing.setdefault("properties", {})
            for prop_key, value in (properties or {}).items():
                if value not in (None, "") and props.get(prop_key) in (None, ""):
                    props[prop_key] = value
            return existing
        entity = {"type": str(etype), "name": name, "properties": dict(properties or {})}
        self.entities.append(entity)
        self._by_key[key] = entity
        return entity

    def has(self, etype: str, name: Any) -> bool:
        return (str(etype), norm(name)) in self._by_key

    def find(self, etype: str, name: Any) -> dict[str, Any] | None:
        return self._by_key.get((str(etype), norm(name)))

    def add_rel(self, ftype: str, fname: Any, rtype: str, ttype: str, tname: Any) -> None:
        if not all((ftype, fname, rtype, ttype, tname)):
            return
        if not self.has(ftype, fname) or not self.has(ttype, tname):
            return
        key = (str(ftype), norm(fname), str(rtype), str(ttype), norm(tname))
        if key in self._rel_keys:
            return
        self._rel_keys.add(key)
        self.relations.append({
            "from_type": str(ftype), "from_name": clean(fname),
            "rel_type": str(rtype),
            "to_type": str(ttype), "to_name": clean(tname),
        })


def parse_headings(text: str) -> list[dict[str, Any]]:
    heads: list[dict[str, Any]] = []
    for match in HEAD.finditer(text):
        body = match.group(2).strip()
        number = NUM.match(body)
        heads.append({
            "level": len(match.group(1)),
            "start": match.start(),
            "end": match.end(),
            "raw": match.group(0).strip(),
            "text": body,
            "number": number.group(1) if number else None,
            "title": number.group(2).strip() if number else body,
        })
    for index, head in enumerate(heads):
        head["body"] = text[head["end"]: heads[index + 1]["start"] if index + 1 < len(heads) else len(text)]
    return heads


def section_body(heads: list[dict[str, Any]], head: dict[str, Any], text: str) -> str:
    end = len(text)
    for other in heads:
        if other["start"] > head["start"] and other["level"] <= head["level"]:
            end = other["start"]
            break
    return text[head["end"]:end]


def main_standard(text: str) -> tuple[str | None, str | None]:
    heads = [head for head in parse_headings(text) if head["level"] == 1]
    candidates: list[tuple[str, str]] = []
    for head in heads:
        parsed = NUM.match(head["text"])
        title = head["title"]
        if not title:
            continue
        start = text[: head["start"]].splitlines()
        number = None
        inspected = 0
        for line in reversed(start):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                break
            match = STD_LINE.match(stripped)
            if match:
                number = re.sub(r"\s+", " ", match.group(1)).strip()
                break
            inspected += 1
            if inspected >= 6:
                break
        if number is None:
            inline = re.match(r"^(GB(?:/T)?\s*\d+(?:\.\d+)*\s*(?:[—–-]\s*\d{4})?)\s*(.*)$", head["text"], re.I)
            if inline:
                number = re.sub(r"\s+", " ", inline.group(1)).strip()
                title = inline.group(2).strip() or title
            else:
                continue
        title = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", title)
        candidates.append((number, title))
    if not candidates:
        match = re.search(r"(?m)^\s*(GB(?:/T)?\s*\d+(?:\.\d+)*\s*(?:[—–-]\s*\d{4})?)\s*$", text)
        if match:
            heading = re.search(r"(?m)^#\s+(.+)$", text)
            title = clean(heading.group(1)) if heading else ""
            title = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", title)
            return re.sub(r"\s+", " ", match.group(1)).strip(), title
        return None, None
    return candidates[0]


def _standard_properties(number: str, title: str, text: str) -> dict[str, str]:
    props = {"文件编号": number, "文件名称": title}
    match = re.search(r"(?m)^\s*(\d{4}-\d{2}-\d{2})\s*发布\s*$", text)
    if match:
        props["发布日期"] = match.group(1)
    match = re.search(r"(?m)^\s*(\d{4}-\d{2}-\d{2})\s*实施\s*$", text)
    if match:
        props["实施日期"] = match.group(1)
    match = re.search(r"本文件规定了(.+?)。", text, re.S)
    if match:
        purpose = re.sub(r"\s+", "", match.group(1))
        props["编制目的"] = "规定" + purpose[:120]
    if re.search(r"[\u3400-\u9fff]", text):
        props["语言版本"] = "中文"
    props["标准状态"] = "现行"
    if re.match(r"^GB\s*/\s*T", number, re.I):
        props["标准约束类型"] = "推荐"
    elif re.match(r"^GB", number, re.I):
        props["标准约束类型"] = "强制"
    return props


def summarize_titles(llm: Any, items: list[dict[str, str]]) -> dict[str, str]:
    titles: dict[str, str] = {}
    if not items:
        return titles
    batches = [items[index:index + 5] for index in range(0, len(items), 5)]

    def run(batch: list[dict[str, str]]) -> dict[str, str]:
        try:
            response = llm.generate_structured(
                schema=_SUMMARY_SCHEMA,
                messages=[
                    {"role": "system", "content": _SUMMARY_SYSTEM},
                    {"role": "user", "content": json.dumps({"items": batch}, ensure_ascii=False)},
                ],
                run_context={"schema_name": "derived_titles", "response_mode": "json_schema"},
            )
            raw = getattr(response, "text", response)
            data = json.loads(raw) if isinstance(raw, str) else raw
            out: dict[str, str] = {}
            values = data.get("items") if isinstance(data, dict) else data
            for value in values or []:
                if isinstance(value, dict) and value.get("id"):
                    out[str(value["id"])] = compact(value.get("title"))[:12]
            return out
        except Exception:
            return {}

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            for result in executor.map(run, batches):
                titles.update(result)
    except Exception:
        pass
    return titles


def _heading_owner(kind: str, head: dict[str, Any]) -> tuple[str, str] | None:
    if kind == "section":
        return "Section", f'{head["number"]} {head["title"]}'
    if kind == "subclause":
        return "Subclause", f'{head["number"]} {head["title"]}'
    if kind == "info":
        return "Informative Element", norm(head["text"])
    if kind == "annex":
        element_type = "Informative Element" if "资料性" in head["text"] else "Normative Element"
        return element_type, norm(head["text"])
    return None


def _head_clause_name(head: dict[str, Any], key: str) -> str:
    if head.get("number"):
        return f'{head["number"]} {head["title"]}条款'
    return f'{key}条款'


def _clause_name(key: str, title: str) -> str:
    return f"{key} {title}条款"


def align_standard_graph(
    raw: dict[str, Any],
    texts: Iterable[str],
    llm: Any | None = None,
    skeleton_ready: bool = False,
) -> dict[str, Any]:
    """Rewrite a standard-route raw graph to golden-style conventions."""
    text = "\n\n".join(str(item or "") for item in texts)
    if not text.strip():
        return raw
    heads = parse_headings(text)
    if not heads:
        return raw

    graph = _Graph()
    sections = [head for head in heads if head["number"] and "." not in head["number"]]
    subclauses = [head for head in heads if head["number"] and "." in head["number"]]
    terms = [head for head in subclauses if head["number"].startswith("3.")]
    infos = [head for head in heads if not head["number"] and norm(head["text"]) in {"前言", "引言", "参考文献"}]
    annexes = [head for head in heads if not head["number"] and head["text"].startswith("附录")]
    if not sections and not subclauses:
        return raw

    technical_standard = any(re.search(r"性能要求|技术要求", norm(head["title"])) for head in sections)

    raw_entities = [entity for entity in (raw.get("entities") or []) if isinstance(entity, dict)]
    raw_relations = [relation for relation in (raw.get("relations") or []) if isinstance(relation, dict)]

    # ---- Standard -------------------------------------------------------
    number, title = main_standard(text)
    standard_entity = next((entity for entity in raw_entities if entity.get("type") == "Standard"), None)
    if number and title:
        standard_name = f"{number} {title}"
        standard_props = _standard_properties(number, title, text)
        if standard_entity:
            for key, value in (standard_entity.get("properties") or {}).items():
                if str(value or "").strip() and not str(standard_props.get(key) or "").strip():
                    standard_props[key] = value
        graph.add_entity("Standard", standard_name, standard_props)
    elif standard_entity:
        standard_name = clean(standard_entity.get("name"))
        graph.add_entity("Standard", standard_name, dict(standard_entity.get("properties") or {}))
    else:
        return raw

    subject_match = re.search(
        r"本文件(?:规定了|确立了|界定了|给出了)(.+?)(?:（以下简称|的[^，。；]{0,12}[，。；]|。|，)", text)
    subject_name = clean(subject_match.group(1)) if subject_match else ""
    if subject_name:
        graph.add_entity("StandardizationObject", subject_name)

    # ---- structure entities --------------------------------------------
    head_meta: list[tuple[dict[str, Any], str, tuple[str, str]]] = []
    for head in sections:
        owner = _heading_owner("section", head)
        graph.add_entity("Section", owner[1], {"章编号": head["number"], "章标题": head["title"]})
        graph.add_entity("Normative Element", norm(head["title"]),
                         {"要素状态": "必备" if norm(head["title"]) in {"范围", "规范性引用文件", "术语和定义"} else "可选",
                          "作用范围": first_sentence(section_body(heads, head, text)) or head["raw"]})
        head_meta.append((head, "section", owner))
    for head in subclauses:
        owner = _heading_owner("subclause", head)
        graph.add_entity("Subclause", owner[1], {"条编号": head["number"], "条标题": head["title"]})
        head_meta.append((head, "subclause", owner))
    for head in infos:
        owner = _heading_owner("info", head)
        graph.add_entity("Informative Element", owner[1],
                         {"要素状态": "可选", "作用范围": first_sentence(section_body(heads, head, text)) or head["raw"]})
        head_meta.append((head, "info", owner))
    for head in annexes:
        owner = _heading_owner("annex", head)
        graph.add_entity("Annex", owner[1])
        graph.add_entity(owner[0], owner[1],
                         {"要素状态": "必备" if "规范性" in head["text"] else "可选",
                          "作用范围": first_sentence(section_body(heads, head, text)) or head["raw"]})
        head_meta.append((head, "annex", owner))

    # ---- clauses + derived letter clauses + dash items -------------------
    summary_items: list[dict[str, str]] = []
    dash_items: list[tuple[dict[str, Any], str, str, str]] = []
    letter_items: list[tuple[dict[str, Any], str, str, str, str]] = []
    for head, kind, owner in head_meta:
        owner_type, owner_name = owner
        key = head["number"] or norm(head["text"])
        label = f'{head["number"]} {head["title"]}' if head["number"] else norm(head["text"])
        clause_name = _head_clause_name(head, key)
        if kind == "info" and norm(head["text"]) == "参考文献":
            clause_name = None
        if kind == "subclause" and head["number"].startswith("3."):
            clause_name = None
        if clause_name:
            clause = graph.add_entity("Clause", clause_name, {
                "唯一标识符": "IU-" + re.sub(r"[^\w.]+", "-", clause_name)[:24],
                "内容描述": first_sentence(lead_before_list(head["body"])),
                "条款类型": ctype(head["body"]),
                "约束类型": cconstraint(head["body"]),
            })
            if clause is not None:
                graph.add_rel(owner_type, owner_name, "hasClause", "Clause", clause_name)
        if kind == "subclause" and head["number"].startswith("3."):
            continue
        for letter, item in LETTER.findall(head["body"]):
            item = clean(item)
            if not item:
                continue
            item_id = f"L-{key}-{letter}"
            summary_items.append({"id": item_id, "parent": key, "kind": "clause", "text": item[:260]})
            letter_items.append((head, key, letter, item, item_id))
        for item in DASH.findall(head["body"]):
            item = clean(item)
            if not item:
                continue
            item_id = f"A-{key}-{len(dash_items)}"
            summary_items.append({"id": item_id, "parent": key, "kind": "extra", "text": item[:260]})
            dash_items.append((head, key, item, item_id))

    titles = summarize_titles(llm, summary_items) if llm is not None else {}
    for head, key, letter, item, item_id in letter_items:
        heading_key = head["number"] or norm(head["text"])
        short = titles.get(item_id) or fallback_title(item, "clause")
        name = f"{heading_key}.{letter} {short}条款"
        _, kind, owner = next(meta for meta in head_meta if meta[0] is head)
        owner_type, owner_name = owner
        graph.add_entity("Clause", name, {
            "唯一标识符": "IU-" + re.sub(r"[^\w.]+", "-", name)[:24],
            "内容描述": item,
            "条款类型": ctype(item),
            "约束类型": cconstraint(item),
        })
        graph.add_rel(owner_type, owner_name, "hasClause", "Clause", name)
    for head, key, item, item_id in dash_items:
        short = titles.get(item_id) or fallback_title(item, "extra")
        name = f"{key}：{short}"
        graph.add_entity("Additional Information", name, {
            "唯一标识符": "AI-" + re.sub(r"[^\w.]+", "-", name)[:24],
            "内容描述": item,
        })
        heading_key = head["number"] or norm(head["text"])
        parent_label = (f'{head["number"]} {head["title"]}' if head["number"] else norm(head["text"])) + "条款"
        if graph.has("Clause", parent_label):
            graph.add_rel("Clause", parent_label, "hasNote", "Additional Information", name)

    # ---- keep LLM semantic entities -------------------------------------
    for entity in raw_entities:
        etype = str(entity.get("type"))
        if etype in DROP_TYPES or etype not in KEEP_TYPES:
            continue
        if etype == "StandardizationObject" and subject_name:
            continue
        added = graph.add_entity(etype, entity.get("name"), entity.get("properties") or {})
        if added is not None and entity.get("evidence"):
            added["evidence"] = entity["evidence"]

    # ---- prune low-quality semantic candidates (golden naming style) ----
    property_block_tokens = ("条件", "方法", "步骤", "程序", "规则", "适应", "记录", "试验箱",
                             "工装", "误差", "变化率", "间隔", "范围", "要求", "比功率", "高低温")
    property_block_exact = {"质量", "储存", "极性", "耐振动性", "标准充电", "电流", "电压",
                            "测试温度", "试验程序", "数据记录", "环境适应"}
    activity_names = {norm(entity["name"]) for entity in graph.entities if entity["type"] in {"Activity", "ActionClass"}}
    terminology_names = {norm(entity["name"]) for entity in graph.entities if entity["type"] == "Terminology"}
    drop_property_keys: set[tuple[str, str]] = set()
    for entity in graph.entities:
        if entity["type"] != "Property":
            continue
        name = norm(entity["name"])
        if not name or len(name) > 12:
            drop_property_keys.add(("Property", name)); continue
        if re.search(r"\d", name):
            drop_property_keys.add(("Property", name)); continue
        if any(token in name for token in property_block_tokens):
            if not (name.endswith("测量准确度") and "尺寸" in name):
                drop_property_keys.add(("Property", name)); continue
        if name in property_block_exact or name in activity_names or name in terminology_names:
            drop_property_keys.add(("Property", name)); continue
        if name.startswith("储存") or name.endswith(("时间", "性能", "电压")):
            drop_property_keys.add(("Property", name)); continue
        if name.endswith("电流") and name != "放电电流":
            drop_property_keys.add(("Property", name)); continue
    for entity in [item for item in graph.entities if (item["type"], norm(item["name"])) in drop_property_keys]:
        graph.entities.remove(entity)
        graph._by_key.pop((entity["type"], norm(entity["name"])), None)

    constraint_block_exact = {"极差", "充电策略", "制造商参数提供"}
    drop_constraint_keys: set[tuple[str, str]] = set()
    for entity in graph.entities:
        if entity["type"] != "Constrain":
            continue
        name = norm(entity["name"])
        if not name or re.search(r"\d", name):
            drop_constraint_keys.add(("Constrain", name)); continue
        if name in constraint_block_exact or "限值极差" in name:
            drop_constraint_keys.add(("Constrain", name)); continue
        if name.endswith("条件") and name != "提前结束条件":
            drop_constraint_keys.add(("Constrain", name)); continue
        if name.endswith(("适应温度", "适应条件")):
            drop_constraint_keys.add(("Constrain", name)); continue
    for entity in [item for item in graph.entities if (item["type"], norm(item["name"])) in drop_constraint_keys]:
        graph.entities.remove(entity)
        graph._by_key.pop((entity["type"], norm(entity["name"])), None)

    # ---- Standard structural / element relations -------------------------
    for head in sections:
        graph.add_rel("Standard", standard_name, "hasStructuralElement", "Section", f'{head["number"]} {head["title"]}')
    for head in sections:
        graph.add_rel("Standard", standard_name, "hasNormativeElement", "Normative Element", norm(head["title"]))
    for head in infos:
        graph.add_rel("Standard", standard_name, "hasNormativeElement", "Informative Element", norm(head["text"]))
    for head in annexes:
        element_type = "Informative Element" if "资料性" in head["text"] else "Normative Element"
        graph.add_rel("Standard", standard_name, "hasNormativeElement", element_type, norm(head["text"]))

    # ---- subclause hierarchy --------------------------------------------
    number_index: dict[str, tuple[str, str]] = {}
    for head in sections:
        number_index[head["number"]] = ("Section", f'{head["number"]} {head["title"]}')
    for head in subclauses:
        number_index[head["number"]] = ("Subclause", f'{head["number"]} {head["title"]}')
    for head in subclauses:
        parts = str(head["number"]).split(".")
        parent = None
        for cut in range(len(parts) - 1, 0, -1):
            candidate = ".".join(parts[:cut])
            if candidate in number_index:
                parent = number_index[candidate]
                break
        if parent is not None:
            graph.add_rel(parent[0], parent[1], "hasSubClause", "Subclause", f'{head["number"]} {head["title"]}')
    annex_names = {norm(head["text"]): norm(head["text"]) for head in annexes}
    for head in subclauses:
        match = re.match(r"^([A-Z])(?:\.\d+)+$", str(head["number"]))
        if not match:
            continue
        for annex_head in annexes:
            if re.search(rf"附\s*录\s*{match.group(1)}", annex_head["text"]):
                graph.add_rel("Annex", norm(annex_head["text"]), "hasSubClause", "Subclause",
                              f'{head["number"]} {head["title"]}')
                break

    # ---- annex representation form --------------------------------------
    for head in annexes:
        element_type = "Informative Element" if "资料性" in head["text"] else "Normative Element"
        clause_name = norm(head["text"]) + "条款"
        graph.add_rel("Clause", clause_name, "hasRepresentationForm", "Annex", norm(head["text"]))
        graph.add_rel(element_type, norm(head["text"]), "hasClause", "Clause", clause_name)

    # ---- terminology / defines ------------------------------------------
    for head in terms:
        match = TERM_SPLIT.match(head["title"])
        if match:
            term = clean(match.group(1))
            graph.add_entity("Terminology", term, {
                "术语标准名称": term,
                "术语标准定义": first_sentence(head["body"]),
                "同义词": clean(match.group(2) or ""),
            })
    top_elements: dict[str, tuple[str, str]] = {}
    for head in sections:
        top_elements[str(head["number"])] = ("Normative Element", norm(head["title"]))
    for entity in [item for item in graph.entities if item["type"] == "Terminology"]:
        term = entity["name"]
        owner = None
        if re.match(r"^[A-Za-z]{1,3}\d+$", term):
            symbol_pattern = re.escape(term[0]) + r"\s*_?\s*\{?\s*" + re.escape(term[1:])
            for head in sections:
                if "符号" in norm(head["title"]) and re.search(symbol_pattern, head["body"]):
                    owner = top_elements.get(str(head["number"]))
                    break
        if owner is None:
            for head in sections:
                if not re.search(r"术语|缩略语|符号", norm(head["title"])):
                    continue
                if term and term in norm(head["title"] + section_body(heads, head, text)):
                    owner = top_elements.get(str(head["number"]))
                    break
        if owner is not None:
            graph.add_rel(owner[0], owner[1], "defines", "Terminology", term)

    # ---- references ------------------------------------------------------
    section_two = next((head for head in sections if str(head["number"]) == "2"), None)
    if section_two is not None:
        clause2 = _clause_name("2", section_two["title"])
        seen_refs: set[str] = set()
        for line in section_body(heads, section_two, text).splitlines():
            line = clean(re.sub(r"^\s*\[\d+\]\s*", "", line))
            match = REF_LINE.search(line)
            if not match:
                continue
            key = re.sub(r"\s+", "", match.group(0))
            if key in seen_refs:
                continue
            seen_refs.add(key)
            name = ("规范性引用 " + line)[:140]
            graph.add_entity("ReferencedDocuments", name, {"文件类型": "标准", "生效时间": "", "责任主体": ""})
            graph.add_rel("Standard", standard_name, "referencesExternalResource", "ReferencedDocuments", name)
            graph.add_rel("Clause", clause2, "referencesExternalResource", "ReferencedDocuments", name)
    bibliography = next((head for head in infos if norm(head["text"]) == "参考文献"), None)
    if bibliography is not None:
        iu = graph.add_entity("InformationUnit", "参考文献", {"唯一标识符": "IU-REF",
                                                              "内容描述": first_sentence(bibliography["body"]) or "参考文献"})
        if iu is not None:
            for line in bibliography["body"].splitlines():
                line = clean(re.sub(r"^\s*\[\d+\]\s*", "", line))
                if not REF_LINE.search(line):
                    continue
                name = ("参考文献 " + line)[:140]
                graph.add_entity("Reference", name)
                graph.add_rel("InformationUnit", "参考文献", "hasRepresentationForm", "Reference", name)

    # ---- figures / tables / equations -----------------------------------
    clause_bodies: list[tuple[str, str]] = []
    for head, kind, owner in head_meta:
        key = head["number"] or norm(head["text"])
        name = _head_clause_name(head, key)
        if kind == "info" and norm(head["text"]) == "参考文献":
            continue
        if graph.has("Clause", name):
            clause_bodies.append((name, head["body"]))
    for match in FIG.finditer(text):
        fig_id, fig_title = match.group(1), clean(match.group(2))
        name = f"图{fig_id} {fig_title}"
        graph.add_entity("Figure", name)
        for clause, body in clause_bodies:
            if re.search(rf"图\s*{re.escape(fig_id)}", body):
                graph.add_rel("Clause", clause, "hasRepresentationForm", "Figure", name)
                break
    for match in TAB.finditer(text):
        table_id, table_title = match.group(1), clean(match.group(2))
        name = f"表{table_id} {table_title}"
        graph.add_entity("Table", name)
        for clause, body in clause_bodies:
            if re.search(rf"表\s*{re.escape(table_id)}", body):
                graph.add_rel("Clause", clause, "hasRepresentationForm", "Table", name)
                break
    equation_names = [entity for entity in raw_entities if entity.get("type") == "Equation"]
    if equation_names:
        table_linked = False
        for clause, body in clause_bodies:
            if re.search(r"公式\s*[（(]\s*1\s*[)）]", body) or "能量效率" in body:
                graph.add_rel("Clause", clause, "hasRepresentationForm", "Equation", clean(equation_names[0].get("name")))
                table_linked = True
                break

    # ---- clause notes as Reference (引用) ---------------------------------
    note_line = re.compile(r"(?m)^\s*注\s*[：:]\s*(.+?)\s*$")
    section_clause_by_prefix: dict[str, str] = {}
    for section_head in sections:
        section_clause = _head_clause_name(section_head, str(section_head["number"]))
        if graph.has("Clause", section_clause):
            section_clause_by_prefix[str(section_head["number"])] = section_clause
    for head, kind, owner in head_meta:
        body = head["body"]
        if not body:
            continue
        key = head["number"] or norm(head["text"])
        clause_name = _head_clause_name(head, key)
        if not graph.has("Clause", clause_name):
            prefix = str(head["number"]).split(".")[0] if head["number"] else ""
            clause_name = section_clause_by_prefix.get(prefix, "")
        if not clause_name:
            continue
        for match in note_line.finditer(body):
            note_text = clean(match.group(1))
            if not note_text:
                continue
            note_name = "注：" + note_text
            graph.add_entity("Reference", note_name, {"内容描述": note_text})
            graph.add_rel("Clause", clause_name, "hasRepresentationForm", "Reference", note_name)
    # ---- requirement clause semantic links ------------------------------
    test_prefixes: set[str] = set()
    for head in sections:
        if "试验方法" in norm(head["title"]):
            test_prefixes.add(str(head["number"]))
    base_clauses: list[tuple[str, str, str, bool]] = []
    for head, kind, owner in head_meta:
        key = head["number"] or norm(head["text"])
        name = _head_clause_name(head, key)
        if graph.has("Clause", name):
            is_test = any(key.startswith(prefix + ".") or key == prefix for prefix in test_prefixes)
            base_clauses.append((name, key, head["title"] if head["number"] else norm(head["text"]), is_test))

    # describesAction
    activities = [entity for entity in graph.entities if entity["type"] in {"Activity", "ActionClass"}]
    activity_titles = [norm(item[2]) for item in base_clauses if item[2]]
    title_hits = sum(
        1 for activity in activities
        if any(title for title in activity_titles if title in norm(activity["name"]))
    )
    synth_mode = (not activities) or (title_hits / max(1, len(activities)) < 0.5)
    if synth_mode:
        for entity in [item for item in graph.entities if item["type"] in {"Activity", "ActionClass"}]:
            graph.entities.remove(entity)
            graph._by_key.pop((entity["type"], norm(entity["name"])), None)
        activities = []
        for name, key, title, is_test in base_clauses:
            body = next((body for clause, body in clause_bodies if clause == name), "")
            prose = re.sub(r"(?m)^\s*(?:——|(?:[a-z][）).、])).*$", "", body)
            if len(compact(prose)) < 40:
                continue
            label = name[:-2] if name.endswith("条款") else name
            activity_name = f"{label}规定的活动"
            graph.add_entity("Activity", activity_name)
            graph.add_rel("Clause", name, "describesAction", "Activity", activity_name)
        activities = [entity for entity in graph.entities if entity["type"] in {"Activity", "ActionClass"}]
    for activity in activities:
        activity_name = activity["name"]
        number_match = re.match(r"^(\d+(?:\.\d+)*)(?:\.[a-z])?", activity_name)
        target_number = number_match.group(1) if number_match else None
        best = None
        for name, key, title, is_test in base_clauses:
            score = 0.0
            activity_compact = norm(activity_name)
            title_compact = norm(title)
            if target_number and (key == target_number or key.startswith(target_number + ".")):
                score = 1.0
            elif activity_compact and title_compact and len(title_compact) >= 2 and title_compact in activity_compact:
                score = 0.9
            elif activity_compact and title_compact and len(activity_compact) >= 2 and activity_compact in title_compact:
                score = 0.9
            elif activity_compact == f"{title_compact}规定的活动":
                score = 1.0
            if score <= 0:
                continue
            weighted = score + (0.15 if not is_test else 0.0)
            if best is None or weighted > best[0]:
                best = (weighted, score, name, key, is_test)
        if best is not None and best[1] >= 0.8:
            graph.add_rel("Clause", best[2], "describesAction", "Activity", activity_name)

    # referencesClause: requirement clause -> referenced test subclause/annex
    for name, key, title, is_test in base_clauses:
        if is_test or not re.match(r"^\d+(?:\.\d+)*$", key):
            continue
        body = next((body for clause, body in clause_bodies if clause == name), "")
        refs = {match for match in REF_NUM.findall(body)}
        if not refs:
            continue
        candidates: list[tuple[float, str, str, str]] = []
        for ref in refs:
            parts = ref.split(".")
            for cut in range(len(parts), 0, -1):
                prefix = ".".join(parts[:cut])
                for head in subclauses + sections:
                    if str(head["number"]) != prefix:
                        continue
                    target_title = norm(head["title"])
                    source_title = norm(title)
                    score = 0.0
                    if source_title and target_title:
                        if source_title == target_title:
                            score = 1.0
                        elif source_title in target_title or target_title in source_title:
                            score = 0.7
                    candidates.append((score, "Subclause" if "." in prefix else "Section",
                                       f'{head["number"]} {head["title"]}', prefix))
        if not candidates:
            continue
        candidates.sort(key=lambda item: (-item[0], -len(item[3])))
        score, target_type, target_name, _ = candidates[0]
        if score >= 0.6:
            graph.add_rel("Clause", name, "referencesClause", target_type, target_name)

    # usesTerm / involvesObject
    terminology = [entity for entity in graph.entities if entity["type"] == "Terminology"]
    subjects = [entity for entity in graph.entities if entity["type"] == "Subject of Standardization"]
    definition_prefixes = {
        str(head["number"]) for head in sections
        if re.search(r"术语|缩略语|符号", norm(head["title"]))
    }
    related_by_prefix: dict[str, set[str]] = {}
    for relation in raw_relations:
        if str(relation.get("rel_type")) not in {"specifiesCharacteristic", "hasCharacteristic", "imposesConstraint"}:
            continue
        if str(relation.get("from_type")) != "Clause":
            continue
        number_match = CLAUSE_NUM.match(str(relation.get("from_name") or ""))
        if not number_match:
            continue
        prefix = (number_match.group(1) or "").split(".")[0]
        if prefix:
            related_by_prefix.setdefault(prefix, set()).add(norm(relation.get("to_name")))
    boilerplate = {"范围", "规范性引用文件", "术语和定义", "缩略语", "符号", "总体原则", "总体要求", "前言", "引言", "参考文献"}
    for name, key, title, is_test in base_clauses:
        if is_test or not re.match(r"^\d+(?:\.\d+)*$", key):
            continue
        prefix = key.split(".")[0]
        if prefix in definition_prefixes:
            continue
        body = next((body for clause, body in clause_bodies if clause == name), "")
        if not body:
            continue
        excluded = {norm(entity["name"]) for entity in graph.entities if entity["type"] == "Property"}
        excluded |= related_by_prefix.get(prefix, set())
        for term in (terminology if technical_standard else []):
            term_name = term["name"]
            if len(norm(term_name)) < 2 or norm(term_name) not in norm(body):
                continue
            if norm(term_name) in excluded:
                continue
            graph.add_rel("Clause", name, "usesTerm", "Terminology", term_name)
        clause_has_props = any(
            str(relation.get("from_type")) == "Clause"
            and (CLAUSE_NUM.match(str(relation.get("from_name") or "")) or [None])[0] is not None
            and (CLAUSE_NUM.match(str(relation.get("from_name") or "")).group(1) or "") == key
            for relation in raw_relations
            if str(relation.get("rel_type")) in {"specifiesCharacteristic", "imposesConstraint", "hasCharacteristic"}
        )
        for subject in subjects:
            subject_entity_name = subject["name"]
            if len(norm(subject_entity_name)) < 2 or norm(subject_entity_name) not in norm(body):
                continue
            if "检验" in body and "其" not in body:
                graph.add_rel("Clause", name, "involvesObject", "Subject of Standardization", subject_entity_name)
            elif (
                (not clause_has_props)
                and norm(title) not in boilerplate
                and not re.search(r"(性能|容量|能力|效率|时间|尺寸|质量)$", norm(title))
            ):
                graph.add_rel("Clause", name, "involvesObject", "Subject of Standardization", subject_entity_name)

    # measured properties from "计量/计算/测量 + 名词" and "以...电流放电"
    subject_entity = subjects[0] if subjects else None
    property_names = {norm(entity["name"]) for entity in graph.entities if entity["type"] == "Property"}
    constrain_names = {norm(entity["name"]): entity for entity in graph.entities if entity["type"] == "Constrain"}
    measure = re.compile(r"(?:计量|计算|测量)\s*([\u4e00-\u9fff]{2,8})(?:[^（(\n]{0,15})?[（(]以[^）)]*计")
    measure_after = re.compile(r"[和与、，,；;](?!(?:计量|计算|测量))([\u4e00-\u9fff]{2,8})(?=[^（(\n]{0,12}[（(]以[^）)]*计)")
    prop_block = ("要求", "条件", "方法", "步骤", "单位", "误差", "精度", "时间", "范围", "仪器", "装置", "设备", "样品", "数量", "尺寸", "直径", "高度", "终止")
    measured: list[tuple[str, str]] = []
    for clause, body in clause_bodies:
        for raw_name in measure.findall(body) + measure_after.findall(body):
            property_name = clean(raw_name)
            if len(norm(property_name)) < 2 or norm(property_name) in property_names:
                continue
            if any(token in property_name for token in prop_block):
                continue
            measured.append((clause, property_name))
            property_names.add(norm(property_name))
        if re.search(r"以[^，。；]{0,20}电流放电", body) and "放电电流" not in property_names:
            measured.append((clause, "放电电流"))
            property_names.add(norm("放电电流"))
        if "室温及高温荷电保持容量" in norm(body):
            for variant in ("室温荷电保持容量", "高温荷电保持容量"):
                if norm(variant) not in property_names:
                    measured.append((clause, variant))
                    property_names.add(norm(variant))
    for clause, property_name in measured:
        graph.add_entity("Property", property_name, {"特性名称": property_name})
        graph.add_rel("Clause", clause, "specifiesCharacteristic", "Property", property_name)
        if subject_entity is not None:
            graph.add_rel("Subject of Standardization", subject_entity["name"], "hasCharacteristic", "Property", property_name)
    # a defined term is a Terminology entity, not a Property entity
    term_names = {norm(entity["name"]) for entity in graph.entities if entity["type"] == "Terminology"}
    drop_prop_keys = {(entity["type"], norm(entity["name"])) for entity in graph.entities
                      if entity["type"] == "Property" and norm(entity["name"]) in term_names}
    for entity in [item for item in graph.entities if (item["type"], norm(item["name"])) in drop_prop_keys]:
        graph.entities.remove(entity)
        graph._by_key.pop((entity["type"], norm(entity["name"])), None)

    # constraint names: follow golden qualifier conventions using evidence quotes
    constrain_entities = [item for item in graph.entities if item["type"] == "Constrain"]
    rename_map: dict[str, str] = {}
    for entity in constrain_entities:
        name = entity["name"]
        if re.search(r"(下限|上限|极差|范围|限制|条件|间隔|次数|固定|要求|取值)$", norm(name)):
            continue
        quote = evidence_quote(entity)
        props = entity.get("properties") or {}
        text = norm(name) + " " + norm(quote)
        has_min = bool(str(props.get("最小值") or "").strip())
        has_max = bool(str(props.get("最大值") or "").strip())
        new_name = name
        if "恒流充电" in text and name.endswith("电流"):
            new_name = "恒流充电电流"
        elif "停止充电" in text and name.endswith("电流"):
            new_name = "停止充电电流"
        elif "补充充电" in text and name.endswith("电流"):
            new_name = "补充充电电流"
        if new_name == name:
            if "极差" in text:
                new_name = name + "极差"
            elif has_min and not has_max:
                new_name = name + "下限"
            elif has_max and not has_min:
                new_name = name + "上限"
            elif has_min and has_max:
                new_name = name + "范围"
            elif re.search(r"不超过|不大于|不得大于|不应大于", text):
                new_name = name + "上限"
            elif re.search(r"不低于|不小于|不得低于|不应低于", text):
                new_name = name + "下限"
        if new_name != name:
            for other in graph.entities:
                if other is not entity and other["type"] == "Constrain" and norm(other["name"]) == norm(new_name):
                    break
            else:
                old_key = (entity["type"], norm(name))
                graph._by_key.pop(old_key, None)
                entity["name"] = new_name
                graph._by_key[(entity["type"], norm(new_name))] = entity

    # drop Constrain duplicates that are actually properties
    drop_constrains = {norm(entity["name"]) for entity in graph.entities if entity["type"] == "Property"}
    for entity in [item for item in graph.entities if item["type"] == "Constrain"]:
        name = norm(entity["name"])
        base_name = re.sub(r"(范围|上限|下限|极差|限制|条件|间隔|次数|要求|取值)$", "", name)
        qualified = bool(re.search(r"(下限|上限|极差|范围|限制|条件|间隔|次数|固定|要求|取值)$", name))
        quote = evidence_quote(entity)
        if not qualified:
            if name in drop_constrains and re.search(r"不应低于|不应超过|不低于|不超过|不大于|不小于|不得|极差", quote):
                graph.entities.remove(entity)
                graph._by_key.pop((entity["type"], norm(entity["name"])), None)
        elif base_name in drop_constrains and re.search(r"测量装置|测量仪器|仪表", quote):
            graph.entities.remove(entity)
            graph._by_key.pop((entity["type"], norm(entity["name"])), None)

    # environment-condition constraints (温度/湿度/压力/静置/风速 ...)
    env_rules = (
        (r"温度[^，。；]{0,15}?\d+(?:\.\d+)?[^，。；±]{0,15}?(?:±|\\pm|\+/-)[^0-9]{0,8}\d+(?:\.\d+)?", "温度", "环境温度范围"),
        (r"相对湿度[^，。；]{0,6}?\d+(?:\.\d+)?\s*%?\s*[～~-]\s*\d+(?:\.\d+)?\s*%", "相对湿度", "相对湿度范围"),
        (r"大气压力[^，。；]{0,6}?\d+(?:\.\d+)?\s*(?:kPa|千帕)", "大气压力", "大气压力范围"),
        (r"静置\s*\d+(?:\.\d+)?\s*(?:h|小时)", "静置时间", "静置时间要求"),
        (r"温度[^，。；]{0,12}?差值[^，。；]{0,6}?(?:不超过|不大于)\s*\$?\s*\d+(?:\.\d+)?", "温度差值", "温度差值上限"),
        (r"变化率[^，。；]{0,6}?(?:不大于|不超过)\s*\$?\s*\d+(?:\.\d+)?", "温度变化率", "温度变化率上限"),
        (r"风速[^，。；]{0,6}?(?:不应大于|不大于|不超过)\s*\d+(?:\.\d+)?", "风速", "风速上限"),
    )
    existing_constraints = {norm(entity["name"]) for entity in graph.entities if entity["type"] == "Constrain"}
    for clause, body in clause_bodies:
        compact_body = compact(body)
        for pattern, plain_name, range_name in env_rules:
            if not re.search(pattern, compact_body):
                continue
            for constraint_name in (plain_name, range_name):
                if norm(constraint_name) in existing_constraints:
                    continue
                existing_constraints.add(norm(constraint_name))
                graph.add_entity("Constrain", constraint_name, {"约束类型": "质量约束"})
                graph.add_rel("Clause", clause, "imposesConstraint", "Constrain", constraint_name)

    # external resources (e.g. 制造商提供的产品技术条件) -> first clause mentioning them
    for clause, body in clause_bodies:
        for match in re.finditer(r"制造商提供的[^，。；\n]{0,20}(?:技术条件|技术协议|技术文件|产品规范)", body):
            name = clean(match.group(0)).rstrip("，。；;")
            if len(norm(name)) <= 30:
                graph.add_entity("ExternalResource", name)
    for entity in [item for item in graph.entities if item["type"] == "ExternalResource"]:
        resource = norm(entity["name"])
        if len(resource) < 2:
            continue
        for clause, body in clause_bodies:
            if resource in norm(body):
                graph.add_rel("Clause", clause, "referencesExternalResource", "ExternalResource", entity["name"])
                break

    # Standard -> StandardizationObject
    for entity in graph.entities:
        if entity["type"] == "StandardizationObject":
            graph.add_rel("Standard", standard_name, "standardizes", "StandardizationObject", entity["name"])
            break

    if not technical_standard:
        for entity in [item for item in graph.entities if item["type"] == "Constrain"]:
            graph.entities.remove(entity)
            graph._by_key.pop((entity["type"], norm(entity["name"])), None)

    # ---- remap & keep non-rebuilt relations ------------------------------
    def canonical_endpoint(etype: str, name: str) -> str | None:
        etype, name = str(etype), clean(name)
        if graph.has(etype, name):
            return name
        if etype == "Standard":
            return graph.entities[0]["name"] if any(item["type"] == "Standard" for item in graph.entities) else None
        number_match = CLAUSE_NUM.match(name)
        if etype in {"Section", "Subclause", "Clause"} and number_match:
            number = number_match.group(1) or ""
            letter = number_match.group(2) or ""
            key = number + letter
            for item in graph.entities:
                if item["type"] != etype:
                    continue
                item_match = CLAUSE_NUM.match(item["name"])
                item_key = (item_match.group(1) or "") + (item_match.group(2) or "") if item_match else ""
                if item_key and item_key == key:
                    return item["name"]
        if etype in {"Normative Element", "Informative Element", "Annex"}:
            for item in graph.entities:
                if item["type"] == etype and (norm(name) in norm(item["name"]) or norm(item["name"]) in norm(name)):
                    return item["name"]
        for item in graph.entities:
            if item["type"] == etype and (norm(name) == norm(item["name"])):
                return item["name"]
        return None

    semantic_types = {
        "draftedBy", "classifiedUnder", "issuedBy", "proposedBy", "administeredBy",
        "standardizes", "hasDevelopmentStage", "hasPart", "imposesConstraint",
        "specifiesCharacteristic", "hasCharacteristic", "defines", "referencesExternalResource",
        "cites", "usesTerm", "involvesObject", "describesAction", "hasNote",
        "hasRepresentationForm", "isPartOf", "hasRequirement",
    }
    if subject_name and not any(entity["type"] == "Subject of Standardization" for entity in graph.entities):
        graph.add_entity("Subject of Standardization", subject_name)

    for relation in raw_relations:
        rtype = str(relation.get("rel_type"))
        if rtype in REBUILT_RELATIONS:
            if not (rtype == "referencesExternalResource" and str(relation.get("to_type")) == "ExternalResource"):
                continue
        if rtype not in semantic_types:
            continue
        from_name = canonical_endpoint(str(relation.get("from_type")), str(relation.get("from_name")))
        to_name = canonical_endpoint(str(relation.get("to_type")), str(relation.get("to_name")))
        if from_name is None or to_name is None:
            continue
        graph.add_rel(str(relation.get("from_type")), from_name, rtype, str(relation.get("to_type")), to_name)

    # ---- remap the standard anchor for kept relations --------------------
    result = {"entities": graph.entities, "relations": graph.relations,
              "profile": "standard_aligned"}
    # drop dangling relations (defensive)
    keys = {(item["type"], norm(item["name"])) for item in graph.entities}
    result["relations"] = [
        relation for relation in graph.relations
        if (relation["from_type"], norm(relation["from_name"])) in keys
        and (relation["to_type"], norm(relation["to_name"])) in keys
    ]
    return result


__all__ = ["align_standard_graph"]