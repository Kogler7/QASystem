"""Ontology- and source-structure-aware candidate abstention.

The selector is deliberately benchmark-agnostic.  It rejects candidates only
when their literal source form conflicts with the compiled ontology or lacks
the structural/relational support required by that ontology role.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .graph_utils import canonical_name
from .structured_text import has_json_table_payload, is_structured_payload

_NUMBERED_HEADING = re.compile(
    r"^\s{0,3}(?:#{1,6}\s+)?(?P<number>\d+(?:\.\d+)*)[.)]?\s+\S"
)
_DATE_PREFIX = re.compile(
    r"^\s*(?:\d{4}[年/-]\d{1,2}(?:月|[/-])\d{1,2}日?|\d{4}年\d{1,2}月)"
)
_SCALAR_PROPERTY = re.compile(r"date|time|year|month|day|日期|时间|年份|年月", re.IGNORECASE)
_MATH_ATOM = re.compile(
    r"^(?:\\[A-Za-z]+|[A-Za-zΑ-Ωα-ωη])"
    r"(?:[_^](?:\{[^{}]+\}|[A-Za-z0-9]+))?$"
)
_SEPARATOR = re.compile(r"[,，、;；|]\s*|\n(?!\s*\n)")
_TABLE_ROW = re.compile(r"^\s*\|.+\|\s*$")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<(?:img|svg)\b", re.IGNORECASE)
#: 表结构支撑。§10 之后解析层把表格统一输出为 json 代码块，切片还会把围栏
#: 留在表题所在片段里，因此围栏开头也算表结构；.md/.txt 原文直读时是 <table> 或管道表格。
_TABLE = re.compile(
    r"<table\b"
    r"|```\s*json\b"
    r"|^\s*\{\s*\"(?:columns|rows)\""
    r"|^\s*\|.+\|\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_EQUATION = re.compile(r"\$\$[\s\S]*?\$\$|\\begin\{(?:equation|align)\}|[^\n]{1,160}=[^\n]{1,160}")
_ANNEX = re.compile(r"^\s{0,3}(?:#{1,6}\s+)?附录\s*[A-Za-z0-9一二三四五六七八九十]", re.MULTILINE)
_REFERENCE = re.compile(r"^\s*\[\d+\]\s+\S", re.MULTILINE)


def _type(candidate: Any) -> str:
    return str(
        getattr(candidate, "canonical_type", None)
        or getattr(candidate, "proposed_type", None)
        or ""
    )


def _evidence(candidate: Any) -> list[Any]:
    return list(getattr(candidate, "evidence", None) or [])


def _blocks(candidate: Any) -> set[str]:
    return {
        str(block_id)
        for evidence in _evidence(candidate)
        for block_id in (getattr(evidence, "source_block_ids", None) or [])
        if block_id
    }


def _quote(candidate: Any) -> str:
    return "\n".join(
        str(getattr(evidence, "quote", "") or "") for evidence in _evidence(candidate)
    )


def _spec(plan: Any, type_id: str) -> Any:
    specs = getattr(plan, "class_specs", {}) or {}
    if type_id in specs:
        return specs[type_id]
    key = canonical_name(type_id)
    return next(
        (value for name, value in specs.items() if canonical_name(name) == key),
        None,
    )


def _ancestors(plan: Any, type_id: str) -> set[str]:
    result: set[str] = set()
    pending = [type_id]
    while pending:
        current = pending.pop()
        spec = _spec(plan, current)
        for parent in getattr(spec, "parents", ()) if spec else ():
            parent_id = str(parent)
            parent_key = canonical_name(parent_id)
            if parent_key in {canonical_name(item) for item in result}:
                continue
            result.add(parent_id)
            pending.append(parent_id)
    return result


def _is_assignable(plan: Any, actual: str, expected: str) -> bool:
    expected_key = canonical_name(expected)
    return canonical_name(actual) == expected_key or any(
        canonical_name(parent) == expected_key for parent in _ancestors(plan, actual)
    )


def _lineage_text(plan: Any, type_id: str) -> str:
    values: list[str] = []
    for item in (type_id, *_ancestors(plan, type_id)):
        spec = _spec(plan, item)
        values.extend(
            [
                str(item),
                str(getattr(spec, "class_id", "") or ""),
                str(getattr(spec, "display_name", "") or ""),
                " ".join(str(value) for value in (getattr(spec, "stereotypes", ()) or ())),
            ]
        )
    return " ".join(values).casefold()


def _own_type_text(plan: Any, type_id: str) -> tuple[str, str]:
    spec = _spec(plan, type_id)
    identifiers = " ".join(
        (
            str(type_id),
            str(getattr(spec, "class_id", "") or ""),
            " ".join(str(value) for value in (getattr(spec, "stereotypes", ()) or ())),
        )
    ).casefold()
    return identifiers, str(getattr(spec, "display_name", "") or "").strip()


def _chunk_text(manager: Any, block_id: str) -> str:
    if not block_id or manager is None:
        return ""
    try:
        return str(manager.get_chunk(block_id).text or "")
    except (KeyError, AttributeError):
        return ""


def _paragraph_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    before = text.rfind("\n\n", 0, max(0, start))
    after = text.find("\n\n", min(len(text), max(start, end)))
    return (0 if before < 0 else before + 2, len(text) if after < 0 else after)


def _is_structured_chunk(text: str) -> bool:
    """Whether *text* is machine-structured data rather than prose paragraphs.

    A JSON array/object or a tab-separated dump already gives every record its
    own structural boundary, so the "dense prose enumeration" heuristic must not
    run on it — otherwise a table of N records is treated as one long paragraph
    and rejected wholesale.  Detection is content-based on purpose: a JSON table
    is often stored with a ``.md`` extension.
    """
    stripped = (text or "").strip()
    # 知识沉淀批量路径会在文档前拼接 "\n## 文件: <文件名>\n\n" 头，剥掉后再判断。
    if stripped.startswith("## 文件:"):
        stripped = stripped.split("\n", 1)[1].strip() if "\n" in stripped else ""
    if not stripped:
        return False
    # 裸 JSON 与 Markdown 外壳包裹的表格 JSON（解析行为规范 §10 产物）都算结构化。
    if is_structured_payload(stripped):
        return True
    lines = [line for line in stripped.splitlines() if line.strip()]
    return bool(len(lines) >= 3 and all("\t" in line for line in lines[:8]))


def _evidence_units(candidate: Any, manager: Any) -> list[tuple[str, int, int, str]]:
    units: list[tuple[str, int, int, str]] = []
    for evidence in _evidence(candidate):
        quote = str(getattr(evidence, "quote", "") or "")
        for block_id in getattr(evidence, "source_block_ids", None) or ():
            block_id = str(block_id)
            text = _chunk_text(manager, block_id)
            if not text:
                continue
            start = getattr(evidence, "start_char", None)
            end = getattr(evidence, "end_char", None)
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
                start = text.find(quote) if quote else -1
                end = start + len(quote) if start >= 0 else -1
            if start < 0 or end < start:
                continue
            unit_start, unit_end = _paragraph_bounds(text, start, end)
            units.append((block_id, unit_start, unit_end, text[unit_start:unit_end]))
    return units


def _support_window(candidate: Any, manager: Any) -> str:
    units = _evidence_units(candidate, manager)
    return "\n".join(unit[3] for unit in units) or _quote(candidate)


def _following_paragraph(candidate: Any, manager: Any, limit: int = 4000) -> str:
    values: list[str] = []
    for block_id, _, end, _ in _evidence_units(candidate, manager):
        text = _chunk_text(manager, block_id)
        cursor = end
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        next_end = text.find("\n\n", cursor)
        if next_end < 0:
            next_end = len(text)
        values.append(text[cursor : min(next_end, cursor + limit)])
    return "\n".join(values)


def _adjacent_chunk_text(candidate: Any, manager: Any, limit: int = 1200) -> str:
    """证据 chunk 之后的相邻 chunk 文本（同一文档，不跨段，各取前 ``limit`` 字符）。

    表题与其表格数据常被 500-700 字符的切片分到相邻两片：表题片自身没有表格
    结构，结构支撑必须能看到下一片（与 standard_semantics 的表定位同口径）。
    """
    if manager is None or not hasattr(manager, "local_context"):
        return ""
    values: list[str] = []
    seen: set[str] = set()
    for evidence in _evidence(candidate):
        for block_id in getattr(evidence, "source_block_ids", None) or ():
            block_id = str(block_id)
            if not block_id or block_id in seen:
                continue
            seen.add(block_id)
            try:
                neighbours = manager.local_context(
                    block_id, previous=0, next=1, same_section=False
                )
            except (KeyError, AttributeError):
                continue
            for chunk in neighbours:
                if str(getattr(chunk, "chunk_id", "")) == block_id:
                    continue
                text = str(getattr(chunk, "text", "") or "")
                if text:
                    values.append(text[:limit])
    return "\n".join(values)


def _warn(
    candidate: Any,
    code: str,
    reason: str,
    sink: list[dict[str, Any]] | None,
) -> None:
    if getattr(candidate, "mapping_status", "accepted") == "rejected":
        return
    candidate.mapping_status = "rejected"
    candidate_warnings = getattr(candidate, "warnings", None)
    if isinstance(candidate_warnings, list) and code not in candidate_warnings:
        candidate_warnings.append(code)
    if sink is not None:
        candidate_id = str(getattr(candidate, "candidate_id", "") or "")
        sink.append(
            {
                "code": code,
                "message": reason,
                "path": f"entities.{candidate_id}" if candidate_id else "entities",
                "value": {"type": _type(candidate), "name": getattr(candidate, "name", "")},
            }
        )


def _relation_endpoints(relation: Any) -> tuple[str, str]:
    if isinstance(relation, dict):
        return (
            str(
                relation.get("from_candidate_id")
                or relation.get("from_id")
                or relation.get("source_id")
                or ""
            ),
            str(
                relation.get("to_candidate_id")
                or relation.get("to_id")
                or relation.get("target_id")
                or ""
            ),
        )
    return (
        str(
            getattr(relation, "from_candidate_id", "")
            or getattr(relation, "source_id", "")
            or ""
        ),
        str(
            getattr(relation, "to_candidate_id", "")
            or getattr(relation, "target_id", "")
            or ""
        ),
    )


def _relation_accepted(relation: Any) -> bool:
    if isinstance(relation, dict):
        return str(relation.get("mapping_status") or "accepted") == "accepted"
    return str(getattr(relation, "mapping_status", "accepted")) == "accepted"


def _relation_type(relation: Any) -> str:
    if isinstance(relation, dict):
        return str(
            relation.get("canonical_relation")
            or relation.get("rel_type")
            or relation.get("proposed_relation")
            or ""
        )
    return str(
        getattr(relation, "canonical_relation", "")
        or getattr(relation, "proposed_relation", "")
        or ""
    )


def _heading_types(plan: Any) -> tuple[str, str] | None:
    resolver = getattr(plan, "resolve_relation", None)
    signatures = tuple(resolver("hasSubClause")) if callable(resolver) else tuple(
        spec
        for specs in (getattr(plan, "relation_specs", {}) or {}).values()
        for spec in specs
        if canonical_name(getattr(spec, "relation_id", "")) == "hassubclause"
    )
    sources = {str(spec.source) for spec in signatures}
    targets = {str(spec.target) for spec in signatures}
    top = sources - targets
    self_targets = {str(spec.target) for spec in signatures if spec.source == spec.target}
    nested = self_targets or (targets - top)
    if len(top) != 1 or len(nested) != 1:
        return None
    return next(iter(top)), next(iter(nested))


def _numbered_heading(candidate: Any) -> str:
    for text in (*(_quote(candidate).splitlines()), str(getattr(candidate, "name", "") or "")):
        match = _NUMBERED_HEADING.match(text)
        if match is not None:
            return match.group("number")
    return ""


def _has_date_property(plan: Any, type_id: str) -> bool:
    spec = _spec(plan, type_id)
    return any(
        _SCALAR_PROPERTY.search(
            " ".join(
                (
                    str(getattr(prop, "identifier", "") or ""),
                    str(getattr(prop, "display_name", "") or ""),
                    str(getattr(prop, "data_type", "") or ""),
                )
            )
        )
        for prop in (getattr(spec, "properties", ()) if spec else ())
    )


def _math_atom(value: str) -> bool:
    normalized = value.strip().strip("$ ").replace(" ", "")
    return bool(_MATH_ATOM.fullmatch(normalized))


class CandidateSelector:
    """Apply cheap pre-relation and support-aware post-relation rejection."""

    def __init__(
        self,
        plan: Any,
        manager: Any | None = None,
        warnings: list[dict[str, Any]] | None = None,
    ) -> None:
        self.plan = plan
        self.manager = manager
        self.warnings = warnings

    def pre_relation(self, candidates: Iterable[Any]) -> list[Any]:
        items = list(candidates)
        self._structured_document_dominance(items)
        self._semantic_source_dominance(items)
        self._technical_source_dominance(items)
        self._dense_enumerations(items)
        self._heading_conflicts(items)
        for candidate in items:
            if getattr(candidate, "mapping_status", "accepted") != "accepted":
                continue
            name = str(getattr(candidate, "name", "") or "").strip()
            date_match = _DATE_PREFIX.match(name)
            if date_match is not None and _has_date_property(self.plan, _type(candidate)):
                remainder = name[date_match.end() :].strip(" ：:，,。.;；()（）")
                if len(remainder) <= 10:
                    _warn(
                        candidate,
                        "CANDIDATE_SCALAR_VALUE",
                        "候选主要由 ontology 日期属性值构成，未形成独立实体标识",
                        self.warnings,
                    )
                    continue
            lineage = _lineage_text(self.plan, _type(candidate))
            if any(token in lineage for token in ("equation", "formula", "math", "公式")) and _math_atom(name):
                _warn(
                    candidate,
                    "CANDIDATE_ISOLATED_MATH_VARIABLE",
                    "单一数学标识符是公式组成部分，不是独立公式实体",
                    self.warnings,
                )
        return items

    def _structured_document_dominance(self, items: list[Any]) -> None:
        structured_sources = {"document_anchor", "normative_reference"}
        anchored_classes = {
            canonical_name(_type(candidate))
            for candidate in items
            if getattr(candidate, "mapping_status", "accepted") == "accepted"
            and getattr(candidate, "source", "") in structured_sources
        }
        if not anchored_classes:
            return
        for candidate in items:
            if getattr(candidate, "mapping_status", "accepted") != "accepted":
                continue
            if canonical_name(_type(candidate)) not in anchored_classes:
                continue
            if getattr(candidate, "source", "") in structured_sources:
                continue
            _warn(
                candidate,
                "CANDIDATE_UNSCOPED_DOCUMENT",
                "同类文档已由主文档锚点或规范性引用章节完整界定，非结构化随文提及不构成额外文档实体",
                self.warnings,
            )

    def _semantic_source_dominance(self, items: list[Any]) -> None:
        structural_names: dict[str, set[str]] = defaultdict(set)
        form_spans: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
        terminology_types = {
            canonical_name(_type(candidate))
            for candidate in items
            if getattr(candidate, "source", "") == "terminology_definition"
        }
        terminology_aliases: set[str] = set()
        normative_titles: set[str] = set()
        for candidate in items:
            source = str(getattr(candidate, "source", "") or "")
            if source == "markdown_structure" or source == "content_element":
                structural_names[canonical_name(getattr(candidate, "name", ""))].add(
                    canonical_name(_type(candidate))
                )
            elif source == "document_form":
                for evidence in _evidence(candidate):
                    start = getattr(evidence, "start_char", None)
                    end = getattr(evidence, "end_char", None)
                    if not isinstance(start, int) or not isinstance(end, int):
                        continue
                    for block_id in getattr(evidence, "source_block_ids", None) or ():
                        form_spans[canonical_name(_type(candidate))].append(
                            (str(block_id), start, end)
                        )
            elif source == "terminology_definition":
                for key, value in (getattr(candidate, "properties", None) or {}).items():
                    if "synonym" not in canonical_name(key) and "同义" not in canonical_name(key):
                        continue
                    values = value if isinstance(value, list) else [value]
                    terminology_aliases.update(canonical_name(item) for item in values if item)
            elif source == "normative_reference":
                for key, value in (getattr(candidate, "properties", None) or {}).items():
                    marker = canonical_name(key)
                    if "title" in marker or "name" in marker or "名称" in marker or "标题" in marker:
                        normative_titles.add(canonical_name(value))

        for candidate in items:
            if getattr(candidate, "mapping_status", "accepted") != "accepted":
                continue
            source = str(getattr(candidate, "source", "") or "")
            if source in {
                "markdown_structure",
                "document_form",
                "terminology_definition",
                "standard_semantics",
            }:
                continue
            name = canonical_name(getattr(candidate, "name", ""))
            type_id = canonical_name(_type(candidate))
            if name in structural_names and type_id not in structural_names[name]:
                _warn(
                    candidate,
                    "CANDIDATE_STRUCTURAL_ALIAS",
                    "候选仅复述已解析的结构标题，但 ontology 类型不是该结构实体",
                    self.warnings,
                )
                continue
            if type_id in terminology_types and (
                name in terminology_aliases or name in normative_titles
            ):
                _warn(
                    candidate,
                    "CANDIDATE_TERMINOLOGY_ALIAS",
                    "候选是双语术语别名或规范性引用文件标题，不构成额外术语实体",
                    self.warnings,
                )
                continue
            if type_id not in form_spans:
                continue
            overlaps = False
            for evidence in _evidence(candidate):
                start = getattr(evidence, "start_char", None)
                end = getattr(evidence, "end_char", None)
                if not isinstance(start, int) or not isinstance(end, int):
                    continue
                for block_id in getattr(evidence, "source_block_ids", None) or ():
                    overlaps = overlaps or any(
                        str(block_id) == anchor_block and start < anchor_end and end > anchor_start
                        for anchor_block, anchor_start, anchor_end in form_spans[type_id]
                    )
            if overlaps:
                _warn(
                    candidate,
                    "CANDIDATE_FORM_ALIAS",
                    "候选是已结构化文档表述形式的原始标题、定界符或公式语法，不构成额外实体",
                    self.warnings,
                )

    def _technical_source_dominance(self, items: list[Any]) -> None:
        """Freeze technical families covered by the deterministic scope parser.

        The rule is source-authority based, not vocabulary based: it activates
        only when this run produced accepted ``technical_semantics`` entities,
        and rejects only otherwise-unscoped raw LLM candidates in the same
        ontology lineage.  Candidates that resolved into an authoritative
        entity already carry the authoritative source and are preserved.
        """
        authoritative_lineage: set[str] = set()
        for candidate in items:
            if (
                getattr(candidate, "mapping_status", "accepted") != "accepted"
                or str(getattr(candidate, "source", "") or "") != "technical_semantics"
            ):
                continue
            type_id = _type(candidate)
            authoritative_lineage.add(canonical_name(type_id))
            authoritative_lineage.update(
                canonical_name(parent) for parent in _ancestors(self.plan, type_id)
            )
        if not authoritative_lineage:
            return
        for candidate in items:
            if getattr(candidate, "mapping_status", "accepted") != "accepted":
                continue
            if str(getattr(candidate, "source", "") or ""):
                continue
            lineage = {canonical_name(_type(candidate))}
            lineage.update(canonical_name(parent) for parent in _ancestors(self.plan, _type(candidate)))
            if not (lineage & authoritative_lineage):
                continue
            _warn(
                candidate,
                "CANDIDATE_UNSCOPED_TECHNICAL_ENTITY",
                "该 ontology 技术实体族已由文档范围/结构解析器闭集界定，随文原始提及不构成额外实体",
                self.warnings,
            )

    def _dense_enumerations(self, items: list[Any]) -> None:
        groups: dict[tuple[str, int, int, str], list[Any]] = defaultdict(list)
        for candidate in items:
            if getattr(candidate, "mapping_status", "accepted") != "accepted":
                continue
            if getattr(candidate, "source", "") in {"markdown_structure", "standard_semantics", "technical_semantics"} or any(
                getattr(evidence, "evidence_type", "") == "structural"
                for evidence in _evidence(candidate)
            ):
                continue
            for block_id, start, end, _ in _evidence_units(candidate, self.manager):
                groups[(block_id, start, end, canonical_name(_type(candidate)))].append(candidate)

        for (block_id, _, _, _), group in groups.items():
            unique = list({id(candidate): candidate for candidate in group}.values())
            if len(unique) < 5:
                continue
            # JSON / TSV 等机器结构化内容（不限文件后缀）里每条记录本身就是独立
            # 结构单元，不能按"同一散文段落里的高密度枚举"处理。
            if _is_structured_chunk(_chunk_text(self.manager, block_id)):
                continue
            unit = _evidence_units(unique[0], self.manager)
            unit_text = unit[0][3] if unit else ""
            # Markdown 表格行各自具有独立结构（列分隔），不属于"缺少独立实体
            # 结构"的散文枚举；xlsx/需求表等表格型文档按行建实体是合法形态。
            if sum(1 for line in unit_text.splitlines() if _TABLE_ROW.match(line)) >= 2:
                continue
            separators = len(_SEPARATOR.findall(unit_text))
            distinct_names = {
                canonical_name(getattr(candidate, "name", "")) for candidate in unique
            }
            if len(distinct_names) < 5 or separators < len(unique) - 1:
                continue
            for candidate in unique:
                _warn(
                    candidate,
                    "CANDIDATE_DENSE_ENUMERATION",
                    "同一原文段落中的高密度同类枚举缺少独立实体结构",
                    self.warnings,
                )

    def _heading_conflicts(self, items: list[Any]) -> None:
        resolved = _heading_types(self.plan)
        if resolved is None:
            return
        parent_type, child_type = resolved
        for candidate in items:
            if getattr(candidate, "mapping_status", "accepted") != "accepted":
                continue
            number = _numbered_heading(candidate)
            candidate_type = _type(candidate)
            if not number or candidate_type not in {parent_type, child_type}:
                continue
            expected = parent_type if "." not in number else child_type
            if canonical_name(candidate_type) != canonical_name(expected):
                _warn(
                    candidate,
                    "CANDIDATE_HEADING_TYPE_CONFLICT",
                    "numbered heading 的结构类型与 ontology hasSubClause 层级不一致",
                    self.warnings,
                )

    def post_relation(
        self,
        candidates: Iterable[Any],
        relations: Iterable[Any],
    ) -> tuple[list[Any], list[Any]]:
        items = list(candidates)
        relation_items = list(relations)
        supporting_relations: dict[str, list[Any]] = defaultdict(list)
        for relation in relation_items:
            if _relation_accepted(relation):
                source_id, target_id = _relation_endpoints(relation)
                supporting_relations[source_id].append(relation)
                supporting_relations[target_id].append(relation)

        for candidate in items:
            if getattr(candidate, "mapping_status", "accepted") != "accepted":
                continue
            candidate_id = str(getattr(candidate, "candidate_id", "") or "")
            type_id = _type(candidate)
            lineage = _lineage_text(self.plan, type_id)
            type_identifiers, type_display = _own_type_text(self.plan, type_id)
            window = _support_window(candidate, self.manager)
            candidate_relations = supporting_relations.get(candidate_id, [])
            relation_support = bool(candidate_relations)

            is_information_form = any(
                token in lineage
                for token in ("informationform", "information form", "信息单元表述形式")
            )
            is_external = any(
                token in lineage
                for token in ("externalresource", "external resource", "外部约束", "外部资源")
            )
            is_additional = any(
                token in lineage
                for token in ("additionalinformation", "additional information", "附加信息")
            )

            if is_information_form:
                if any(token in type_identifiers for token in ("figure", "diagram", "image")) or type_display == "图":
                    structural_support = bool(_IMAGE.search(window))
                elif "table" in type_identifiers or type_display == "表":
                    following = _following_paragraph(candidate, self.manager)
                    adjacent = _adjacent_chunk_text(candidate, self.manager)
                    structural_support = bool(
                        _TABLE.search(window)
                        or _TABLE.search(following)
                        or _TABLE.search(adjacent)
                        or has_json_table_payload(window)
                        or has_json_table_payload(following)
                        or has_json_table_payload(adjacent)
                    )
                elif any(token in type_identifiers for token in ("equation", "formula", "math")) or type_display == "数学公式":
                    structural_support = bool(_EQUATION.search(window)) and not _math_atom(
                        str(getattr(candidate, "name", "") or "")
                    )
                elif "annex" in type_identifiers or type_display == "附录":
                    structural_support = bool(_ANNEX.search(window))
                elif "reference" in type_identifiers or type_display == "引用":
                    structural_support = bool(_REFERENCE.search(window))
                else:
                    structural_support = any(
                        canonical_name(_relation_type(relation))
                        in {"hasnote", "hasexample"}
                        for relation in candidate_relations
                    )
                # A generic relation cannot turn a caption, variable, heading,
                # or prose label into a representation entity.  The source
                # document must contain the matching representation form.
                if not structural_support:
                    _warn(
                        candidate,
                        "CANDIDATE_UNSUPPORTED_INFORMATION_FORM",
                        "信息表述形式候选缺少匹配的文档结构或 ontology 关系支撑",
                        self.warnings,
                    )
            elif (is_external or is_additional) and not relation_support:
                _warn(
                    candidate,
                    "CANDIDATE_UNSUPPORTED_INFORMATION",
                    "外部/附加信息候选缺少 ontology 关系支撑",
                    self.warnings,
                )

        rejected_ids = {
            str(getattr(candidate, "candidate_id", "") or "")
            for candidate in items
            if getattr(candidate, "mapping_status", "accepted") == "rejected"
        }
        for relation in relation_items:
            source_id, target_id = _relation_endpoints(relation)
            if source_id not in rejected_ids and target_id not in rejected_ids:
                continue
            if isinstance(relation, dict):
                relation["mapping_status"] = "rejected"
                relation_id = str(relation.get("candidate_id") or "")
            else:
                relation.mapping_status = "rejected"
                relation_id = str(getattr(relation, "candidate_id", "") or "")
            if self.warnings is not None:
                self.warnings.append(
                    {
                        "code": "RELATION_REJECTED_ENDPOINT",
                        "message": "关系端点已被候选选择器拒绝",
                        "path": f"relations.{relation_id}" if relation_id else "relations",
                        "value": {"from_id": source_id, "to_id": target_id},
                    }
                )
        return items, relation_items


def select_pre_relation(
    candidates: Iterable[Any],
    plan: Any,
    manager: Any | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> list[Any]:
    return CandidateSelector(plan, manager, warnings).pre_relation(candidates)


def select_post_relation(
    candidates: Iterable[Any],
    relations: Iterable[Any],
    plan: Any,
    manager: Any | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> tuple[list[Any], list[Any]]:
    return CandidateSelector(plan, manager, warnings).post_relation(candidates, relations)


__all__ = ["CandidateSelector", "select_post_relation", "select_pre_relation"]
