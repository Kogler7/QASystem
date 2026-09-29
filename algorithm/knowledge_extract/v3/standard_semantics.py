"""Deterministic semantic entities exposed by conventional standard structure."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .candidate_models import EntityCandidate
from .evidence import Evidence
from .graph_utils import canonical_name

_HEADING = re.compile(r"^(?P<marks>#{1,6})[ \t]+(?P<body>.*?)[ \t]*$")
_NUMBERED = re.compile(r"^(?P<number>\d+(?:\.\d+)*)\s+(?P<title>.+?)\s*$")
_BILINGUAL = re.compile(
    r"^(?P<cjk>[\u3400-\u9fff][\u3400-\u9fff·（）()、/\-\s]*?)\s+"
    r"(?P<english>[A-Za-z][A-Za-z0-9 /'\-]{2,})$"
)
_SYMBOL = re.compile(r"^\s*\$\s*(?P<symbol>[A-Za-z](?:_\{?\d+\}?)?)\s*\$\s*[:：](?P<definition>.+)$")
_ANNEX = re.compile(
    r"^附录\s*(?P<label>[A-Za-z0-9一二三四五六七八九十]+)\s*"
    r"(?:[（(][^）)]*(?:规范|normative)[^）)]*[）)])?\s*(?P<title>.+)$",
    re.IGNORECASE,
)
_REFERENCE_HEADING = re.compile(r"参考文献|bibliograph", re.IGNORECASE)
_REFERENCE_ENTRY = re.compile(r"^\s*\[(?P<index>\d+)\]\s*(?P<entry>\S.+?)\s*$")
_EQUATION_LABEL = re.compile(
    r"公式\s*[（(]\s*(?P<number>\d+)\s*[）)]\s*计算\s*"
    r"(?P<label>[\u3400-\u9fffA-Za-z][\u3400-\u9fffA-Za-z0-9 _\-]{1,30}?)(?=\s*\$|[:：，,。；;\n])"
)
_FORMULA_BLOCK = re.compile(r"\$\$[\s\S]+?\$\$")
_TABLE_LABEL = re.compile(r"(?m)^\s*(?P<label>表\s*\d+(?:[.\-]\d+)?\s+[^\n<]{2,80})\s*$")
#: 表题之后的结构支撑。解析层 §10 已把表格统一输出为 JSON（docx/OCR/Excel），
#: .md/.txt 原文直读时仍是 <table> 或管道表格；切片还可能把表题与表格数据
#: 分到相邻 chunk，因此裸 JSON 开头的形态也要认。
_TABLE_BLOCK = re.compile(
    r"<table\b"
    r"|```\s*json\b"
    r"|^\s*\{\s*\"(?:columns|rows)\""
    r"|^\s*\|.+\|\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_ISSUED_DATE = re.compile(r"(?m)^(?P<date>\d{4}-\d{2}-\d{2})\s*发布\s*$")
_STANDARDIZED_OBJECT = re.compile(
    r"本文件规定了(?P<name>[^，。；;]{2,80}?)(?:（以下简称|\(以下简称|的(?:技术|电|性能|要求))"
)
_ADMINISTERED_BY = re.compile(
    r"(?:本文件|本标准)由(?P<name>[^。\n]{2,120}?)(?:提出并)?归口(?:[。\n]|$)"
)
_REQUIREMENT_MODAL = re.compile(r"(?P<subject>[\u3400-\u9fff、和及]{2,40}?)(?:不应|应符合|应无|应正确|应满足)")
_TECHNICAL_PROPERTY_ENDING = re.compile(
    r"(?:容量|效率|外观|标识|质量|尺寸|温度|电压|电流|功率|能量|速度|压力|强度|精度|时间|频率|性能)$"
)
_ACTION_TITLE = re.compile(r"(?:试验|测试|检验|检查|测量|校准)$")
_ABBREVIATION = re.compile(r"以下简称[“\"](?P<name>[^”\"]{2,30})[”\"]")
_APPLIES_TO = re.compile(r"本文件适用于(?P<body>[^。；]+)")
_EXTERNAL_RESOURCE = re.compile(
    r"(?:应符合|依据|按照|依照|参照|使用|满足)\s*"
    r"(?P<phrase>(?P<party>[\u3400-\u9fffA-Za-z]{1,20}?(?:商|方|机构|部门|委员会|组织))"
    r"(?:提供|发布|规定|维护)的(?P<kind>[^，。；;\n]{2,40}?(?:技术条件|说明书|规范|指南|手册|文件|标准|数据库)))"
)
_COMPARATOR = re.compile(r"不应低于|不得低于|不低于|不应大于|不得大于|不大于|不超过")
_PERCENT = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>%|％)")
_ENVIRONMENT_PREFIX = re.compile(r"^(?:室温|常温|低温|高温|超低温|超高温)")

_FIGURE_LABEL = re.compile(r"(?m)^\s*图\s*(?P<label>[A-Z]?\.?\d+)\s+(?P<title>.+?)\s*$")


def _spec(plan: Any, type_id: str) -> Any | None:
    specs = getattr(plan, "class_specs", {}) or {}
    if type_id in specs:
        return specs[type_id]
    key = canonical_name(type_id)
    return next(
        (value for name, value in specs.items() if canonical_name(name) == key),
        None,
    )


def _relation_targets(plan: Any, relation_id: str) -> list[str]:
    key = canonical_name(relation_id)
    return sorted(
        {
            str(spec.target)
            for relation_specs in (getattr(plan, "relation_specs", {}) or {}).values()
            for spec in relation_specs
            if canonical_name(getattr(spec, "relation_id", "")) == key
        }
    )


def _descendants(plan: Any, type_id: str) -> list[str]:
    result: list[str] = []
    pending = [type_id]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if canonical_name(current) in seen:
            continue
        seen.add(canonical_name(current))
        spec = _spec(plan, current)
        for child in getattr(spec, "children", ()) if spec else ():
            result.append(str(child))
            pending.append(str(child))
    return result


def _stage_type(plan: Any, target: str, source_text: str) -> str:
    candidates = [target, *_descendants(plan, target)]
    ranked: list[tuple[int, str]] = []
    for type_id in candidates:
        spec = _spec(plan, type_id)
        symbols = canonical_name(
            " ".join(
                (
                    type_id,
                    str(getattr(spec, "display_name", "") or ""),
                    " ".join(str(value) for value in (getattr(spec, "stereotypes", ()) or ())),
                )
            )
        )
        score = 0
        if "发布" in source_text and ("发布" in symbols or "publication" in symbols):
            score += 4
        if "批准" in symbols or "approval" in symbols:
            score += 2
        score += len(_descendants(plan, type_id)) == 0
        ranked.append((score, type_id))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][1]


def _property(spec: Any, *markers: str) -> str | None:
    normalized = tuple(canonical_name(marker) for marker in markers)
    for prop in getattr(spec, "properties", ()) or ():
        symbols = canonical_name(
            " ".join(
                (
                    str(getattr(prop, "identifier", "") or ""),
                    str(getattr(prop, "display_name", "") or ""),
                )
            )
        )
        if any(marker in symbols for marker in normalized):
            return str(getattr(prop, "identifier", "") or getattr(prop, "display_name", ""))
    return None


def _properties(spec: Any, values: tuple[tuple[tuple[str, ...], Any], ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for markers, value in values:
        property_id = _property(spec, *markers)
        if property_id and value not in (None, ""):
            result[property_id] = value
    return result


def _candidate(
    type_id: str,
    name: str,
    properties: dict[str, Any],
    chunk_id: str,
    text: str,
    start: int,
    end: int,
    source: str,
) -> EntityCandidate:
    return EntityCandidate(
        candidate_id="",
        proposed_type=type_id,
        canonical_type=type_id,
        name=name,
        properties=properties,
        evidence=[
            Evidence(
                span_id=f"{chunk_id}:{start}:{end}",
                source_block_ids=[chunk_id],
                quote=text[start:end],
                start_char=start,
                end_char=end,
                evidence_type="structural",
            )
        ],
        source=source,
    )


def _from_entity(
    type_id: str,
    name: str,
    properties: dict[str, Any],
    entity: Any,
    source: str,
) -> EntityCandidate:
    return EntityCandidate(
        candidate_id="",
        proposed_type=type_id,
        canonical_type=type_id,
        name=name,
        properties=properties,
        evidence=list(getattr(entity, "evidence", ()) or ()),
        source=source,
    )


def _line_rows(text: str) -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    position = 0
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        rows.append((position, position + len(line), line))
        position += len(raw)
    return rows


def _next_definition(rows: list[tuple[int, int, str]], index: int) -> tuple[int, int, str] | None:
    for start, end, line in rows[index + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        if _HEADING.match(line):
            return None
        return start + len(line) - len(line.lstrip()), end, stripped
    return None


def _add_terminology(pool: Any, manager: Any, plan: Any, warnings: Any) -> int:
    spec = _spec(plan, "Terminology")
    if spec is None:
        return 0
    type_id = str(getattr(spec, "class_id", "") or "Terminology")
    before = pool.entity_candidate_count
    active: str | None = None
    active_level: int | None = None
    for chunk in manager:
        text = chunk.text or ""
        rows = _line_rows(text)
        for index, (start, end, line) in enumerate(rows):
            heading = _HEADING.match(line)
            if heading is not None:
                level = len(heading.group("marks"))
                body = heading.group("body").strip()
                normalized = canonical_name(body)
                if ("术语" in normalized and "定义" in normalized) or "termsanddefinitions" in normalized:
                    active, active_level = "terms", level
                    continue
                if "符号" in normalized or normalized in {"symbols", "symbolsandabbreviatedterms"}:
                    active, active_level = "symbols", level
                    continue
                if active_level is not None and level <= active_level:
                    active, active_level = None, None
                if active != "terms":
                    continue
                numbered = _NUMBERED.match(body)
                bilingual = _BILINGUAL.match(numbered.group("title")) if numbered else None
                definition = _next_definition(rows, index)
                if bilingual is None or definition is None:
                    continue
                term = re.sub(r"\s+", "", bilingual.group("cjk")).strip()
                english = re.sub(r"\s+", " ", bilingual.group("english")).strip()
                properties = _properties(
                    spec,
                    (
                        (("termname", "术语标准名称", "术语名称"), term),
                        (("termdefinition", "术语标准定义", "定义"), definition[2]),
                        (("synonym", "同义词"), english),
                    ),
                )
                pool.add_entity(
                    _candidate(
                        type_id,
                        term,
                        properties,
                        chunk.chunk_id,
                        text,
                        start,
                        definition[1],
                        "terminology_definition",
                    ),
                    warnings=warnings,
                )
                continue
            if active != "symbols":
                continue
            symbol = _SYMBOL.match(line)
            if symbol is None:
                continue
            name = re.sub(r"[_{}\\]", "", symbol.group("symbol"))
            definition = symbol.group("definition").strip()
            properties = _properties(
                spec,
                (
                    (("termname", "术语标准名称", "术语名称"), name),
                    (("termdefinition", "术语标准定义", "定义"), definition),
                ),
            )
            accepted = pool.add_entity(
                _candidate(
                    type_id,
                    name,
                    properties,
                    chunk.chunk_id,
                    text,
                    start + len(line) - len(line.lstrip()),
                    end,
                    "terminology_definition",
                ),
                warnings=warnings,
            )
            if accepted is not None:
                accepted.properties = dict(properties)
    return pool.entity_candidate_count - before


def _add_forms(pool: Any, manager: Any, plan: Any, warnings: Any) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for chunk in manager:
        text = chunk.text or ""
        rows = _line_rows(text)
        for index, (start, end, line) in enumerate(rows):
            heading = _HEADING.match(line)
            if heading is None:
                continue
            body = heading.group("body").strip()
            annex = _ANNEX.match(body)
            annex_spec = _spec(plan, "Annex")
            if annex is not None and annex_spec is not None:
                name = f"附录{annex.group('label').upper()} {annex.group('title').strip()}"
                before = pool.entity_candidate_count
                pool.add_entity(
                    _candidate("Annex", name, {}, chunk.chunk_id, text, start, end, "document_form"),
                    warnings=warnings,
                )
                counts["Annex"] += pool.entity_candidate_count - before
            if not _REFERENCE_HEADING.search(canonical_name(body)):
                continue
            reference_spec = _spec(plan, "Reference")
            if reference_spec is None:
                continue
            entry = next(
                (
                    (entry_start, entry_end, match.group("entry").strip())
                    for entry_start, entry_end, entry_line in rows[index + 1 :]
                    if (match := _REFERENCE_ENTRY.match(entry_line)) is not None
                ),
                None,
            )
            if entry is None:
                continue
            before = pool.entity_candidate_count
            pool.add_entity(
                _candidate(
                    "Reference",
                    f"参考文献 {entry[2]}",
                    {},
                    chunk.chunk_id,
                    text,
                    start,
                    entry[1],
                    "document_form",
                ),
                warnings=warnings,
            )
            counts["Reference"] += pool.entity_candidate_count - before

    equation_spec = _spec(plan, "Equation")
    chunks = list(manager)
    if equation_spec is not None:
        for chunk_index, chunk in enumerate(chunks):
            text = chunk.text or ""
            for label in _EQUATION_LABEL.finditer(text):
                formula_chunk = chunk
                formula = _FORMULA_BLOCK.search(text, label.end(), min(len(text), label.end() + 800))
                if formula is None and chunk_index + 1 < len(chunks):
                    next_chunk = chunks[chunk_index + 1]
                    formula_chunk = next_chunk
                    formula = _FORMULA_BLOCK.search(next_chunk.text or "", 0, min(len(next_chunk.text or ""), 800))
                if formula is None:
                    continue
                name = f"公式（{label.group('number')}）{label.group('label').strip()}"
                candidate = _candidate(
                    "Equation",
                    name,
                    {},
                    chunk.chunk_id,
                    text,
                    label.start(),
                    label.end(),
                    "document_form",
                )
                if formula_chunk.chunk_id != chunk.chunk_id:
                    formula_text = formula_chunk.text or ""
                    candidate.evidence.append(
                        Evidence(
                            span_id=f"{formula_chunk.chunk_id}:{formula.start()}:{formula.end()}",
                            source_block_ids=[formula_chunk.chunk_id],
                            quote=formula_text[formula.start():formula.end()],
                            start_char=formula.start(),
                            end_char=formula.end(),
                            evidence_type="structural",
                        )
                    )
                else:
                    candidate.evidence[0] = Evidence(
                        span_id=f"{chunk.chunk_id}:{label.start()}:{formula.end()}",
                        source_block_ids=[chunk.chunk_id],
                        quote=text[label.start():formula.end()],
                        start_char=label.start(),
                        end_char=formula.end(),
                        evidence_type="structural",
                    )
                before = pool.entity_candidate_count
                pool.add_entity(candidate, warnings=warnings)
                counts["Equation"] += pool.entity_candidate_count - before
    table_spec = _spec(plan, "Table")
    if table_spec is not None:
        for chunk_index, chunk in enumerate(chunks):
            text = chunk.text or ""
            for label in _TABLE_LABEL.finditer(text):
                has_table = _TABLE_BLOCK.search(text, label.end(), min(len(text), label.end() + 1200))
                if has_table is None and chunk_index + 1 < len(chunks):
                    following = chunks[chunk_index + 1].text or ""
                    has_table = _TABLE_BLOCK.search(following, 0, min(len(following), 1200))
                if has_table is None:
                    continue
                name = re.sub(r"\s+", " ", label.group("label")).strip()
                before = pool.entity_candidate_count
                pool.add_entity(
                    _candidate(
                        "Table",
                        name,
                        {},
                        chunk.chunk_id,
                        text,
                        label.start("label"),
                        label.end("label"),
                        "document_form",
                    ),
                    warnings=warnings,
                )
                counts["Table"] += pool.entity_candidate_count - before
    figure_spec = _spec(plan, "Figure")
    if figure_spec is not None:
        for chunk in chunks:
            text = chunk.text or ""
            for label in _FIGURE_LABEL.finditer(text):
                name = f"图{label.group('label')} {label.group('title').strip()}"
                before = pool.entity_candidate_count
                pool.add_entity(
                    _candidate(
                        "Figure",
                        name,
                        {},
                        chunk.chunk_id,
                        text,
                        label.start(),
                        label.end(),
                        "document_form",
                    ),
                    warnings=warnings,
                )
                counts["Figure"] += pool.entity_candidate_count - before
    return dict(counts)


def _numbered_name(entity: Any) -> tuple[str, str] | None:
    match = _NUMBERED.match(str(getattr(entity, "name", "") or ""))
    return (match.group("number"), match.group("title").strip()) if match else None


def _entity_scope(entity: Any, manager: Any) -> tuple[str, str, int, int] | None:
    evidence = next(iter(getattr(entity, "evidence", ()) or ()), None)
    if evidence is None or not evidence.source_block_ids:
        return None
    block_id = str(evidence.source_block_ids[0])
    try:
        text = manager.get_chunk(block_id).text or ""
    except KeyError:
        return None
    start = evidence.start_char if isinstance(evidence.start_char, int) else text.find(evidence.quote)
    if start < 0:
        return None
    body_start = evidence.end_char if isinstance(evidence.end_char, int) else start + len(evidence.quote)
    heading = re.match(r"(?P<marks>#{1,6})", evidence.quote or "")
    max_level = len(heading.group("marks")) if heading else 6
    next_heading = re.search(rf"(?m)^#{{1,{max_level}}}\s+", text[body_start:])
    end = body_start + next_heading.start() if next_heading else len(text)
    return block_id, text, start, end


def _normalize_property_name(value: str, preferred_names: tuple[str, ...] = ()) -> list[str]:
    value = re.sub(r"^(?:其|该|上述)", "", value.strip())
    value = re.sub(r"^(?:同时)?(?:所有测试对象)?(?:(?:室温及高温|室温|高温|低温))?", "", value)
    value = re.sub(r"极差$", "", value)
    parts = re.split(r"[、和及]", value)
    result: list[str] = []
    for part in parts:
        part = re.sub(r"^(?:其|该|上述)", "", part.strip())
        part = re.sub(r"^(?:同时)?(?:所有测试对象)?(?:(?:室温|高温|低温))?", "", part)
        part = re.sub(r"极差$", "", part)
        preferred = sorted(
            (name for name in preferred_names if part.endswith(name)),
            key=lambda item: (-len(item), canonical_name(item)),
        )
        if not preferred and "的" in part:
            preferred = sorted(
                {
                    name[index:]
                    for name in preferred_names
                    for index in range(len(name))
                    if len(name[index:]) >= 2
                    and part.endswith(name[index:])
                    and _TECHNICAL_PROPERTY_ENDING.search(name[index:])
                },
                key=lambda item: (-len(item), canonical_name(item)),
            )
        if preferred:
            part = preferred[0]
        if _TECHNICAL_PROPERTY_ENDING.search(part):
            result.append(part)
    return result


def _scope_subjects(text: str) -> list[tuple[str, int, int]]:
    """Expand coordinated scope nouns using the document-declared short name.

    Example shape (without knowing the domain nouns): ``A类X和B类X单元`` can
    inherit the trailing part of the declared short name ``X单元``.  The rule
    is syntactic and applies only inside an explicit ``本文件适用于`` span.
    """
    abbreviation = _ABBREVIATION.search(text)
    applies = _APPLIES_TO.search(text)
    if abbreviation is None or applies is None:
        return []
    base = abbreviation.group("name").strip()
    rows = [(base, abbreviation.start("name"), abbreviation.end("name"))]
    body = re.split(r"[，,](?:其他|其余)", applies.group("body"), maxsplit=1)[0]
    for raw_part in re.split(r"[、和及]", body):
        part = raw_part.strip()
        part = re.sub(r"^.*(?:上的|中的|内的)", "", part)
        part = re.sub(r"^(?:各类|各种|相关|适用的)", "", part)
        if len(part) < 2:
            continue
        name = part
        if not name.endswith(base):
            for prefix_length in range(len(base), 0, -1):
                prefix = base[:prefix_length]
                if name.endswith(prefix):
                    name += base[prefix_length:]
                    break
        if name == base or not name.endswith(base):
            continue
        source_start = applies.start("body") + applies.group("body").find(raw_part)
        source_end = source_start + len(raw_part)
        rows.append((name, source_start, source_end))
    return list(dict.fromkeys(rows))


def _constraint_class(plan: Any) -> Any | None:
    candidates: list[Any] = []
    for target in _relation_targets(plan, "imposesConstraint"):
        for type_id in (target, *_descendants(plan, target)):
            spec = _spec(plan, type_id)
            if spec is not None and spec not in candidates:
                candidates.append(spec)
    if not candidates:
        candidates = [spec for symbol in ("Constrain", "Constraint") if (spec := _spec(plan, symbol))]
    ranked = sorted(
        candidates,
        key=lambda spec: (
            bool(getattr(spec, "is_abstract", False)),
            not any(
                marker in canonical_name(f"{prop.identifier} {prop.display_name}")
                for prop in getattr(spec, "properties", ()) or ()
                for marker in ("minvalue", "maxvalue", "最小值", "最大值")
            ),
            len(_descendants(plan, str(getattr(spec, "class_id", "") or ""))),
            str(getattr(spec, "class_id", "") or ""),
        ),
    )
    return ranked[0] if ranked else None


def _common_suffix(values: list[str]) -> str:
    if not values:
        return ""
    suffix = values[0]
    for value in values[1:]:
        while suffix and not value.endswith(suffix):
            suffix = suffix[1:]
    return suffix


def _property_in_title(property_name: str, title: str) -> bool:
    if property_name in title:
        return True
    compact_property = re.sub(r"\s+", "", property_name)
    compact_title = re.sub(r"\s+", "", title)
    bigrams = {
        compact_property[index : index + 2]
        for index in range(max(0, len(compact_property) - 1))
    }
    if not bigrams:
        return False
    overlap = sum(token in compact_title for token in bigrams)
    return overlap >= 2 and overlap / len(bigrams) >= 0.5


def _comparison_records(
    clause: Any,
    manager: Any,
    properties: list[Any],
    subjects: list[Any],
    terms: list[Any],
) -> list[dict[str, Any]]:
    resolved = _entity_scope(clause, manager)
    if resolved is None:
        return []
    block_id, text, start, end = resolved
    property_names = sorted(
        {str(entity.name) for entity in properties if str(entity.name)},
        key=lambda item: (-len(item), canonical_name(item)),
    )
    reference_names = sorted(
        {
            str(entity.name)
            for entity in [*properties, *terms]
            if str(entity.name)
        },
        key=lambda item: (-len(item), canonical_name(item)),
    )
    contextual_entities = sorted(
        [*subjects, *terms],
        key=lambda entity: (-len(str(entity.name)), canonical_name(entity.name)),
    )
    records: list[dict[str, Any]] = []
    cursor = start
    for raw_line in text[start:end].splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            cursor += len(raw_line)
            continue
        previous_target = ""
        for match in _COMPARATOR.finditer(line):
            left_boundary = max(
                line.rfind(marker, 0, match.start())
                for marker in ("，", ",", "；", ";", "。", "。\n")
            )
            left = line[left_boundary + 1 : match.start()]
            target = next((name for name in property_names if name in left), "")
            if not target:
                target = previous_target
            if not target:
                continue
            previous_target = target
            right_boundary_candidates = [
                position
                for marker in ("，", ",", "；", ";", "。")
                if (position := line.find(marker, match.end())) >= 0
            ]
            right_boundary = min(right_boundary_candidates) if right_boundary_candidates else len(line)
            right = line[match.end() : right_boundary]
            value_match = _PERCENT.search(right)
            reference = next(
                (name for name in reference_names if name != target and name in right),
                "",
            )
            operator = "min" if "低于" in match.group(0) else "max"
            value = value_match.group("value") if value_match is not None else ""
            inferred_relative = operator == "min" and not value and bool(reference)
            if inferred_relative:
                value = "100"
            if not value:
                continue
            spread = "极差" in left[left.rfind(target) + len(target) :]
            preceding = line[: match.start()]
            contextual = next(
                (entity for entity in contextual_entities if str(entity.name) in preceding),
                None,
            )
            records.append(
                {
                    "block_id": block_id,
                    "quote": line.strip(),
                    "start": cursor + len(line) - len(line.lstrip()),
                    "end": cursor + len(line.rstrip()),
                    "target": target,
                    "operator": operator,
                    "value": value,
                    "unit": "%",
                    "reference": reference,
                    "relative": inferred_relative,
                    "spread": spread,
                    "contextual": contextual,
                }
            )
        cursor += len(raw_line)
    return records


def _constraint_name(
    *,
    clause_title: str,
    title_properties: list[str],
    record: dict[str, Any],
    alternatives: list[dict[str, Any]],
    subjects: list[Any],
    terms: list[Any],
) -> str:
    target = str(record["target"])
    if len(title_properties) > 1:
        return f"{target}约束"
    contextual = record.get("contextual")
    if contextual is None:
        return f"{clause_title}约束"
    term_ids = {entity.candidate_id for entity in terms}
    if contextual.candidate_id in term_ids:
        if record is alternatives[0]:
            return f"{clause_title}约束"
        suffix = _common_suffix(
            [str(item["contextual"].name) for item in alternatives if item.get("contextual")]
        )
        qualifier = str(contextual.name)
        if suffix and qualifier.endswith(suffix):
            qualifier = qualifier[: -len(suffix)]
        title_tail = _ENVIRONMENT_PREFIX.sub("", clause_title)
        return f"{qualifier}{title_tail}约束"
    subject_names = [str(entity.name) for entity in subjects]
    base = min(
        (name for name in subject_names if str(contextual.name).endswith(name)),
        key=len,
        default="",
    )
    qualifier = str(contextual.name)
    if base and qualifier.endswith(base):
        qualifier = qualifier[: -len(base)]
    title_index = clause_title.find(target)
    if qualifier and title_index >= 0:
        return f"{clause_title[:title_index]}{qualifier}{clause_title[title_index:]}约束"
    return f"{clause_title}约束"


def _add_constraints(
    pool: Any,
    manager: Any,
    plan: Any,
    clauses: list[Any],
    properties: list[Any],
    warnings: Any,
) -> int:
    spec = _constraint_class(plan)
    if spec is None or not properties:
        return 0
    type_id = str(getattr(spec, "class_id", "") or "")
    subjects = [
        entity
        for entity in pool.accepted_entities()
        if entity.source == "technical_semantics"
        and canonical_name(entity.canonical_type) == canonical_name("Subject of Standardization")
    ]
    terms = [
        entity
        for entity in pool.accepted_entities()
        if entity.source == "terminology_definition"
    ]
    before = pool.entity_candidate_count
    seen_names: set[str] = set()
    for clause in clauses:
        numbered = _numbered_name(clause)
        if numbered is None:
            continue
        clause_title = re.sub(r"(?:要求|条款)$", "", numbered[1])
        title_properties = [
            str(prop.name)
            for prop in properties
            if _property_in_title(str(prop.name), clause_title)
        ]
        if not title_properties:
            continue
        records = _comparison_records(clause, manager, properties, subjects, terms)
        if not records:
            continue
        selected: list[dict[str, Any]] = []
        by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            by_target[str(record["target"])].append(record)
        for target, target_records in by_target.items():
            relative = next(
                (record for record in target_records if record["operator"] == "min" and record["relative"]),
                None,
            )
            upper = next(
                (
                    record
                    for record in target_records
                    if record["operator"] == "max" and not record["spread"]
                ),
                None,
            )
            if relative is not None and upper is not None:
                range_record = dict(relative)
                range_record["name"] = f"{target}相对{relative['reference']}约束"
                range_record["minimum"] = relative["value"]
                range_record["maximum"] = upper["value"]
                selected.append(range_record)
                spread = next((record for record in target_records if record["spread"]), None)
                if spread is not None:
                    spread_record = dict(spread)
                    spread_record["name"] = f"{target}极差约束"
                    spread_record["minimum"] = ""
                    spread_record["maximum"] = spread["value"]
                    selected.append(spread_record)
                continue
            lower = [
                record
                for record in target_records
                if record["operator"] == "min" and not record["spread"] and not record["relative"]
            ]
            if target not in title_properties or not lower:
                continue
            unique_values = {record["value"] for record in lower}
            if len(title_properties) > 1 and len(unique_values) > 1:
                continue
            contextual_ids = {
                record["contextual"].candidate_id
                for record in lower
                if record.get("contextual") is not None
            }
            term_ids = {entity.candidate_id for entity in terms}
            if contextual_ids and contextual_ids <= term_ids:
                alternatives = lower
            elif len(unique_values) > 1:
                alternatives = [min(lower, key=lambda record: float(record["value"]))]
            else:
                alternatives = [lower[0]]
            for record in alternatives:
                selected_record = dict(record)
                selected_record["name"] = _constraint_name(
                    clause_title=clause_title,
                    title_properties=title_properties,
                    record=record,
                    alternatives=alternatives,
                    subjects=subjects,
                    terms=terms,
                )
                selected_record["minimum"] = record["value"]
                selected_record["maximum"] = ""
                selected.append(selected_record)

        for record in selected:
            name = str(record["name"])
            if name in seen_names:
                continue
            seen_names.add(name)
            minimum = str(record.get("minimum") or "")
            maximum = str(record.get("maximum") or "")
            threshold = f"{minimum}-{maximum}" if minimum and maximum else ""
            constraint_properties = _properties(
                spec,
                (
                    (("constrainttype", "约束类型"), "质量约束"),
                    (("minvalue", "最小值"), minimum),
                    (("maxvalue", "最大值"), maximum),
                    (("thresholdrange", "阈值范围"), threshold),
                    (("measurementunit", "测量单位"), record["unit"]),
                    (("constrainttarget", "约束目标"), record["target"]),
                ),
            )
            accepted = pool.add_entity(
                _candidate(
                    type_id,
                    name,
                    constraint_properties,
                    str(record["block_id"]),
                    manager.get_chunk(str(record["block_id"])).text or "",
                    int(record["start"]),
                    int(record["end"]),
                    "technical_semantics",
                ),
                warnings=warnings,
            )
            if accepted is not None:
                # The comparison parser is the authoritative owner of
                # constraint properties.  A one-sided limit must not acquire
                # an invented range or opposite bound from an LLM merge.
                accepted.properties = dict(constraint_properties)
    return pool.entity_candidate_count - before


def _add_technical_entities(pool: Any, manager: Any, plan: Any, warnings: Any) -> dict[str, int]:
    action_spec = _spec(plan, "ActionClass")
    subject_spec = _spec(plan, "Subject of Standardization")
    property_spec = _spec(plan, "Property")
    resource_spec = _spec(plan, "ExternalResource")
    counts: dict[str, int] = defaultdict(int)
    structural = [
        entity
        for entity in pool.accepted_entities()
        if getattr(entity, "source", "") == "markdown_structure"
    ]
    method_parent = next(
        (
            entity
            for entity in structural
            if (numbered := _numbered_name(entity)) is not None
            and len(numbered[0].split(".")) == 2
            and "试验方法" in numbered[1]
        ),
        None,
    )
    method_number = _numbered_name(method_parent)[0] if method_parent is not None else ""
    if action_spec is not None and method_number:
        for entity in structural:
            numbered = _numbered_name(entity)
            if numbered is None or not re.fullmatch(re.escape(method_number) + r"\.\d+", numbered[0]):
                continue
            number, title = numbered
            reference_word = next(
                (
                    match.group("kind")
                    for chunk in manager
                    if (
                        match := re.search(
                            rf"按\s*{re.escape(number)}(?:\.\d+)*\s*(?P<kind>检验|试验)",
                            chunk.text or "",
                        )
                    )
                    is not None
                ),
                "",
            )
            name = title if _ACTION_TITLE.search(title) or not reference_word else f"{title}{reference_word}"
            properties = _properties(
                action_spec,
                ((("actionname", "行动名称", "动作名称"), name),),
            )
            before = pool.entity_candidate_count
            pool.add_entity(
                _from_entity("ActionClass", name, properties, entity, "technical_semantics"),
                warnings=warnings,
            )
            counts["ActionClass"] += pool.entity_candidate_count - before

    if subject_spec is not None:
        seen: set[str] = set()
        for chunk in manager:
            text = chunk.text or ""
            for name, start, end in _scope_subjects(text):
                if name in seen:
                    continue
                seen.add(name)
                properties = _properties(
                    subject_spec,
                    ((("objectname", "对象名称", "主题名称"), name),),
                )
                before = pool.entity_candidate_count
                pool.add_entity(
                    _candidate(
                        "Subject of Standardization",
                        name,
                        properties,
                        chunk.chunk_id,
                        text,
                        start,
                        end,
                        "technical_semantics",
                    ),
                    warnings=warnings,
                )
                counts["Subject of Standardization"] += pool.entity_candidate_count - before

    requirement_clauses = sorted(
        (
            entity
            for entity in pool.accepted_entities()
            if entity.source == "standard_semantics"
            and canonical_name(entity.canonical_type) == "clause"
            and str(entity.name).endswith("要求")
        ),
        key=lambda entity: tuple(
            int(part) for part in ((_numbered_name(entity) or ("", ""))[0]).split(".") if part.isdigit()
        ),
    )
    if property_spec is not None:
        seen_properties: set[str] = set()
        for clause in requirement_clauses:
            scope = _entity_scope(clause, manager)
            if scope is None:
                continue
            block_id, text, start, end = scope
            numbered = _numbered_name(clause)
            clause_title = re.sub(r"(?:要求|条款)$", "", numbered[1]) if numbered else ""
            preferred_names = tuple(
                item.strip()
                for item in re.split(r"[、和及]", clause_title)
                if item.strip()
            )
            for match in _REQUIREMENT_MODAL.finditer(text, start, end):
                for name in _normalize_property_name(
                    match.group("subject"),
                    preferred_names,
                ):
                    if name in seen_properties:
                        continue
                    seen_properties.add(name)
                    mention = match.start("subject") + match.group("subject").rfind(name)
                    mention = max(mention, match.start("subject"))
                    properties = _properties(
                        property_spec,
                        ((("propertyname", "特性名称"), name),),
                    )
                    before = pool.entity_candidate_count
                    pool.add_entity(
                        _candidate(
                            "Property",
                            name,
                            properties,
                            block_id,
                            text,
                            mention,
                            match.end("subject"),
                            "technical_semantics",
                        ),
                        warnings=warnings,
                    )
                    counts["Property"] += pool.entity_candidate_count - before

    if resource_spec is not None:
        for chunk in manager:
            text = chunk.text or ""
            for match in _EXTERNAL_RESOURCE.finditer(text):
                phrase = match.group("phrase")
                resource_properties = _properties(
                    resource_spec,
                    (
                        (("filetype", "文件类型", "资源类型"), "文献"),
                        (("responsibleparty", "责任主体"), match.group("party")),
                    ),
                )
                before = pool.entity_candidate_count
                accepted = pool.add_entity(
                    _candidate(
                        "ExternalResource",
                        phrase,
                        resource_properties,
                        chunk.chunk_id,
                        text,
                        match.start("phrase"),
                        match.end("phrase"),
                        "technical_semantics",
                    ),
                    warnings=warnings,
                )
                if accepted is not None:
                    accepted.properties = dict(resource_properties)
                counts["ExternalResource"] += pool.entity_candidate_count - before
    technical_properties = [
        entity
        for entity in pool.accepted_entities()
        if entity.source == "technical_semantics"
        and canonical_name(entity.canonical_type) == "property"
    ]
    if property_spec is not None:
        clause_bodies: list[str] = []
        for clause in requirement_clauses:
            resolved = _entity_scope(clause, manager)
            if resolved is None:
                continue
            _, text, start, end = resolved
            clause_bodies.append(text[start:end])
        for prop in technical_properties:
            quantitative = any(
                str(prop.name) in body
                and (_COMPARATOR.search(body) is not None or _PERCENT.search(body) is not None)
                for body in clause_bodies
            )
            property_properties = _properties(
                property_spec,
                (
                    (("propertyname", "特性名称"), prop.name),
                    (("propertytype", "特性类型"), "能力型" if quantitative else "描述型"),
                ),
            )
            # Requirement values belong to the related constraint entity, not
            # to the reusable characteristic definition itself.
            prop.properties = dict(property_properties)
    counts["Constrain"] += _add_constraints(
        pool,
        manager,
        plan,
        requirement_clauses,
        technical_properties,
        warnings,
    )
    return dict(counts)


def _first_sentence_text(body: Any, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", str(body or "")).strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[。！？；])", text)
    return (parts[0] if parts else text)[:limit].strip()


def _short_name(value: Any, limit: int = 24) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    head = re.split(r"[，,；;：:]", text)[0].strip()
    if 4 <= len(head) <= limit:
        return head
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("。", "；", ";", "，", ",", "：", ":"):
        pos = cut.rfind(sep)
        if pos >= limit // 2:
            cut = cut[:pos]
            break
    return cut.strip("；;，,。：: ")


def _clause_type_text(text: Any) -> str:
    text = str(text or "")
    if re.search(r"不应|不得|应(?:当|该|能|予|按)", text):
        return "要求型"
    if "宜" in text:
        return "推荐型"
    if re.search(r"(?<!不)可(?:以|按|采用|使用|设置|选)", text):
        return "允许型"
    if re.search(r"说明|指明|给出|表示", text):
        return "指示型"
    return "陈述型"


def _constraint_type_text(text: Any) -> str:
    text = str(text or "")
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


_LIST_LETTER = re.compile(r"(?m)^\s*([a-z])[）)]\s*(.+?)\s*$")
_LIST_DASH = re.compile(r"(?m)^\s*——\s*(.+?)\s*$")


def _heading_body(entity: Any, manager: Any) -> tuple[Any, int, str] | None:
    evidence = next(iter(getattr(entity, "evidence", ()) or ()), None)
    if evidence is None:
        return None
    chunk = None
    for block_id in list(getattr(evidence, "source_block_ids", ()) or ()):
        try:
            chunk = manager.get_chunk(block_id)
            break
        except Exception:
            continue
    if chunk is None:
        return None
    text = chunk.text or ""
    start = evidence.end_char if isinstance(evidence.end_char, int) else 0
    if not 0 <= start <= len(text):
        start = 0
    match = re.search(r"(?m)^\s{0,3}#{1,6}\s+", text[start:])
    end = start + match.start() if match else len(text)
    return chunk, start, text[start:end]


def _clause_properties(spec: Any, name: str, body: Any) -> dict[str, Any]:
    return _properties(
        spec,
        (
            (("uniqueidentifier", "unique_identifier", "identifier", "唯一标识符"),
             "IU-" + re.sub(r"[^\w.]+", "-", name)[:24]),
            (("contentdescription", "content_description", "内容描述"), _first_sentence_text(body)),
            (("clausetype", "clause_type", "条款类型"), _clause_type_text(body)),
            (("constrainttype", "constraint_type", "约束类型"), _constraint_type_text(body)),
        ),
    )


def _add_content_entities(pool: Any, manager: Any, plan: Any, warnings: Any) -> dict[str, int]:
    normative_spec = _spec(plan, "Normative Element")
    informative_spec = _spec(plan, "Informative Element")
    clause_spec = _spec(plan, "Clause")
    extra_spec = _spec(plan, "Additional Information")
    if normative_spec is None or informative_spec is None or clause_spec is None:
        return {}
    counts: dict[str, int] = defaultdict(int)
    structural = [
        entity
        for entity in pool.accepted_entities()
        if getattr(entity, "source", "") == "markdown_structure"
    ]
    # 1) every numbered heading yields a Clause ("<number> <title>条款")
    for entity in structural:
        numbered = _numbered_name(entity)
        if numbered is None:
            continue
        number, title = numbered
        if "." not in number:
            before = pool.entity_candidate_count
            pool.add_entity(
                _from_entity("Normative Element", title, {}, entity, "content_element"),
                warnings=warnings,
            )
            counts["Normative Element"] += pool.entity_candidate_count - before
        name = f"{number} {title}条款"
        body = ""
        info = _heading_body(entity, manager)
        if info is not None:
            body = info[2]
        before = pool.entity_candidate_count
        pool.add_entity(
            _from_entity(
                "Clause",
                name,
                _clause_properties(clause_spec, name, body),
                entity,
                "standard_semantics",
            ),
            warnings=warnings,
        )
        counts["Clause"] += pool.entity_candidate_count - before
    # 2) explicit list items inside each heading body (a/b/c -> Clause, —— -> Additional Information)
    for entity in structural:
        numbered = _numbered_name(entity)
        if numbered is None:
            continue
        number, _title = numbered
        info = _heading_body(entity, manager)
        if info is None:
            continue
        chunk, body_start, body = info
        text = chunk.text or ""
        for match in _LIST_LETTER.finditer(body):
            item = re.sub(r"\s+", " ", match.group(2)).strip()
            if not item:
                continue
            name = f"{number}.{match.group(1)} {_short_name(item)}条款"
            before = pool.entity_candidate_count
            pool.add_entity(
                _candidate(
                    "Clause",
                    name,
                    _clause_properties(clause_spec, name, item),
                    chunk.chunk_id,
                    text,
                    body_start + match.start(2),
                    body_start + match.end(2),
                    "standard_semantics",
                ),
                warnings=warnings,
            )
            counts["Clause"] += pool.entity_candidate_count - before
        if extra_spec is None:
            continue
        for match in _LIST_DASH.finditer(body):
            item = re.sub(r"\s+", " ", match.group(1)).strip()
            if not item:
                continue
            name = f"{number}：{_short_name(item)}"
            properties = _properties(
                extra_spec,
                (
                    (("uniqueidentifier", "unique_identifier", "identifier", "唯一标识符"),
                     "AI-" + re.sub(r"[^\w.]+", "-", name)[:24]),
                    (("contentdescription", "content_description", "内容描述"), item),
                ),
            )
            before = pool.entity_candidate_count
            pool.add_entity(
                _candidate(
                    "Additional Information",
                    name,
                    properties,
                    chunk.chunk_id,
                    text,
                    body_start + match.start(1),
                    body_start + match.end(1),
                    "standard_semantics",
                ),
                warnings=warnings,
            )
            counts["Additional Information"] += pool.entity_candidate_count - before
    # 3) annex content element + clause
    forms = [
        entity
        for entity in pool.accepted_entities()
        if getattr(entity, "source", "") == "document_form"
    ]
    annex = next((entity for entity in forms if canonical_name(entity.canonical_type) == "annex"), None)
    if annex is not None:
        before = pool.entity_candidate_count
        pool.add_entity(
            _from_entity("Normative Element", annex.name, {}, annex, "content_element"),
            warnings=warnings,
        )
        counts["Normative Element"] += pool.entity_candidate_count - before
        before = pool.entity_candidate_count
        pool.add_entity(
            _from_entity("Clause", f"{annex.name}条款", {}, annex, "standard_semantics"),
            warnings=warnings,
        )
        counts["Clause"] += pool.entity_candidate_count - before
    # 4) reference section
    reference = next((entity for entity in forms if canonical_name(entity.canonical_type) == "reference"), None)
    if reference is not None:
        before = pool.entity_candidate_count
        pool.add_entity(
            _from_entity("Informative Element", "参考文献", {}, reference, "content_element"),
            warnings=warnings,
        )
        counts["Informative Element"] += pool.entity_candidate_count - before
        before = pool.entity_candidate_count
        pool.add_entity(
            _from_entity("Clause", "参考文献条款", {}, reference, "standard_semantics"),
            warnings=warnings,
        )
        counts["Clause"] += pool.entity_candidate_count - before
    return dict(counts)


def _add_metadata_semantics(pool: Any, manager: Any, plan: Any, warnings: Any) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    stage_targets = _relation_targets(plan, "hasDevelopmentStage")
    object_targets = _relation_targets(plan, "standardizes")
    administered_targets = _relation_targets(plan, "administeredBy")
    for chunk in manager:
        text = chunk.text or ""
        if len(stage_targets) == 1:
            match = _ISSUED_DATE.search(text)
            spec = _spec(plan, stage_targets[0])
            if match is not None and spec is not None:
                type_id = _stage_type(plan, stage_targets[0], match.group(0))
                spec = _spec(plan, type_id)
                if spec is None:
                    continue
                name = str(getattr(spec, "display_name", "") or type_id)
                date = match.group("date")
                properties = _properties(
                    spec,
                    (
                        (("stagecode", "阶段代码"), "60"),
                        (("startdate", "开始日期"), date),
                        (("enddate", "结束日期"), date),
                    ),
                )
                before = pool.entity_candidate_count
                accepted = pool.add_entity(
                    _candidate(
                        type_id,
                        name,
                        properties,
                        chunk.chunk_id,
                        text,
                        match.start(),
                        match.end(),
                        "standard_semantics",
                    ),
                    warnings=warnings,
                )
                if accepted is not None:
                    accepted.properties = dict(properties)
                counts[type_id] += pool.entity_candidate_count - before
        if len(administered_targets) == 1:
            match = _ADMINISTERED_BY.search(text)
            type_id = administered_targets[0]
            spec = _spec(plan, type_id)
            if match is not None and spec is not None:
                name = match.group("name").strip()
                properties = _properties(
                    spec,
                    ((("partyname", "organizationname", "相关方名称", "机构名称", "名称"), name),),
                )
                before = pool.entity_candidate_count
                accepted = pool.add_entity(
                    _candidate(
                        type_id,
                        name,
                        properties,
                        chunk.chunk_id,
                        text,
                        match.start(),
                        match.end(),
                        "standard_semantics",
                    ),
                    warnings=warnings,
                )
                if accepted is not None:
                    accepted.properties = dict(properties)
                counts[type_id] += pool.entity_candidate_count - before
        if len(object_targets) == 1:
            match = _STANDARDIZED_OBJECT.search(text)
            spec = _spec(plan, object_targets[0])
            if match is not None and spec is not None:
                type_id = object_targets[0]
                name = match.group("name").strip()
                properties = _properties(
                    spec,
                    ((("subjectname", "主题名称", "对象名称"), name),),
                )
                before = pool.entity_candidate_count
                pool.add_entity(
                    _candidate(
                        type_id,
                        name,
                        properties,
                        chunk.chunk_id,
                        text,
                        match.start(),
                        match.end(),
                        "standard_semantics",
                    ),
                    warnings=warnings,
                )
                counts[type_id] += pool.entity_candidate_count - before
    return dict(counts)


def enrich_standard_semantics(
    pool: Any,
    manager: Any,
    plan: Any,
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Add only entities with explicit, conventional standard-document structure."""
    terminology = _add_terminology(pool, manager, plan, warnings)
    forms = _add_forms(pool, manager, plan, warnings)
    metadata = _add_metadata_semantics(pool, manager, plan, warnings)
    content = _add_content_entities(pool, manager, plan, warnings)
    technical = _add_technical_entities(pool, manager, plan, warnings)
    return {
        "terminology": terminology,
        "forms": forms,
        "metadata": metadata,
        "content": content,
        "technical": technical,
    }


__all__ = ["enrich_standard_semantics"]
