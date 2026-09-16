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
_MARKDOWN_HEADING = re.compile(
    r"(?m)^\s*#{1,6}\s*(?P<number>\d+(?:\.\d+){0,3})\s+(?P<title>.+?)\s*$"
)
_MD_TITLE = re.compile(r"(?m)^#\s+(?P<title>\S.*?)\s*$")

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


def _resolve_property(plan: ExtractionPlan, type_id: str, *candidates: str) -> str | None:
    """把逻辑属性名解析为 schema 的稳定标识。

    兼容英文标识（standardNumber）与中文标识/展示名（文件编号），
    后者见 benchmark/standard 的标准文件统一抽取本体。
    """
    spec = plan.class_specs.get(type_id)
    if spec is None:
        return None
    for candidate in candidates:
        for prop in spec.properties:
            if prop.identifier == candidate or prop.display_name == candidate:
                return prop.identifier
    return None


def _resolve_class(plan: ExtractionPlan, *candidates: str) -> str | None:
    """返回第一个存在于运行时 ontology 的类标识（兼容新旧命名）。"""
    for candidate in candidates:
        if candidate in plan.allowed_types:
            return candidate
    return None


def _iso_date(value: str) -> str:
    match = _CN_DATE.search(value or "")
    if not match:
        return (value or "").strip()
    return (
        f"{match.group('year')}-{int(match.group('month')):02d}-{int(match.group('day')):02d}"
    )


def _normalize_standard_name(title: str) -> str:
    """按 golden 口径规范化标准名称。

    保留“标准数字化 第1部分：…”中“第N部分”前的分隔空格，去掉
    “电性能要求 及试验方法”这类中文之间的多余空格。
    """
    title = re.sub(r"\s+", " ", (title or "").strip())
    protected = title.replace(" 第", "\x00第")
    protected = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", protected)
    return protected.replace("\x00第", " 第")


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
        """本地确定性抽取：元数据 + 结构单元 + 分类代码（§11.1）。"""
        entities: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        text = _full_text(manager)

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
        number_key = _resolve_property(plan, "Standard", "standardNumber", "文件编号")
        if number_key:
            properties[number_key] = standard_number

        title_match = _MD_TITLE.search(text)
        document_name = _normalize_standard_name(title_match.group("title")) if title_match else ""
        if not document_name:
            name_match = _DOCUMENT_NAME_BRACKET.search(text, match.start())
            if name_match:
                document_name = name_match.group("name").strip()
        name_key = _resolve_property(plan, "Standard", "documentName", "文件名称")
        if name_key and document_name:
            properties[name_key] = document_name

        issued = _ISSUED_DATE.search(text)
        issued_key = _resolve_property(plan, "Standard", "issuedDate", "发布日期")
        if issued_key and issued:
            properties[issued_key] = _iso_date(issued.group("date"))
        effective = _EFFECTIVE_DATE.search(text)
        effective_key = _resolve_property(plan, "Standard", "effectiveDate", "实施日期")
        if effective_key and effective:
            properties[effective_key] = _iso_date(effective.group("date"))
        status = _STATUS.search(text)
        status_key = _resolve_property(plan, "Standard", "status", "标准状态")
        if status_key and status:
            properties[status_key] = status.group("status")
        kind = _STANDARD_KIND.search(text)
        kind_key = _resolve_property(plan, "Standard", "constraintType", "标准约束类型")
        if kind_key and kind:
            properties[kind_key] = kind.group("kind")
        version = _LANGUAGE_VERSION.search(text)
        version_key = _resolve_property(plan, "Standard", "languageVersion", "语言版本")
        if version_key and version:
            properties[version_key] = version.group("version")

        return {
            "type": "Standard",
            "name": f"{standard_number} {document_name}".strip(),
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
            return self._heading_units(plan, chunk.text or "")
        if not clause_number:
            return []
        title = (chunk.section_title or "").strip()
        is_section = "." not in clause_number
        if is_section:
            type_id = "Section"
        elif _has_class(plan, "Subclause"):
            type_id = "Subclause"
        else:
            type_id = "Clause"
        if not _has_class(plan, type_id):
            return []
        properties: dict[str, Any] = {}
        number_key = _resolve_property(
            plan,
            type_id,
            "sectionNumber" if is_section else "clauseNumber",
            "章编号" if is_section else "条编号",
        )
        if number_key:
            properties[number_key] = clause_number
        title_key = _resolve_property(
            plan,
            type_id,
            "sectionTitle" if is_section else "clauseTitle",
            "章标题" if is_section else "条标题",
        )
        if title and title_key:
            properties[title_key] = title
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

    def _heading_units(self, plan: ExtractionPlan, text: str) -> list[dict[str, Any]]:
        """无上游结构元数据时，从 Markdown 标题行解析章/条（文本级兜底）。"""
        units: list[dict[str, Any]] = []
        seen: set[str] = set()
        for match in _MARKDOWN_HEADING.finditer(text or ""):
            number = match.group("number").strip()
            if not number or number in seen:
                continue
            seen.add(number)
            title = match.group("title").strip()
            is_section = "." not in number
            if is_section:
                type_id = "Section"
            else:
                type_id = _resolve_class(plan, "Subclause", "Clause") or "Clause"
            if not _has_class(plan, type_id):
                continue
            properties: dict[str, Any] = {}
            number_key = _resolve_property(
                plan,
                type_id,
                "sectionNumber" if is_section else "clauseNumber",
                "章编号" if is_section else "条编号",
            )
            if number_key:
                properties[number_key] = number
            title_key = _resolve_property(
                plan,
                type_id,
                "sectionTitle" if is_section else "clauseTitle",
                "章标题" if is_section else "条标题",
            )
            if title and title_key:
                properties[title_key] = title
            units.append(
                {
                    "type": type_id,
                    "name": number,
                    "properties": properties,
                    "evidence": {"quote": f"{number} {title}".strip()},
                    "_clause_number": number,
                }
            )
        return units

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
        subclause_type = _resolve_class(plan, "Subclause", "Clause")
        if subclause_type and plan.resolve_relation("hasSubClause"):
            for unit in units:
                if unit["type"] != subclause_type:
                    continue
                parent_number = ".".join(unit["_clause_number"].split(".")[:-1])
                parent = by_number.get(parent_number)
                if parent is None or parent["type"] not in {"Section", subclause_type}:
                    continue
                relations.append(
                    {
                        "from_type": parent["type"],
                        "from_name": parent["name"],
                        "rel_type": "hasSubClause",
                        "to_type": subclause_type,
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
        ics_type = _resolve_class(plan, "InternationalClassificationofStandard", "InternationalClassificationStandard")
        ics_key = _resolve_property(plan, ics_type, "ICS分类代码", "ICS_code") if ics_type else None
        if ics_type and ics_key:
            for match in _ICS.finditer(text):
                code = match.group("code")
                raw_code = code
                code = f"ICS {code}"
                entities.append(
                    {
                        "type": ics_type,
                        "name": code,
                        "properties": {ics_key: raw_code},
                        "evidence": {"quote": _entity_quote(text, match.group(0))},
                    }
                )
        ccs_type = _resolve_class(plan, "ChineseClassificationofStandard")
        ccs_key = _resolve_property(plan, ccs_type, "CCS分类代码", "CCS_code") if ccs_type else None
        if (
            ccs_type and ccs_key
        ):
            for match in _CCS.finditer(text):
                code = match.group("code")
                raw_code = code
                code = f"CCS {code}"
                entities.append(
                    {
                        "type": ccs_type,
                        "name": code,
                        "properties": {ccs_key: raw_code},
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
                expected_type = "Section" if "." not in clause_number else (
                    _resolve_class(plan, "Subclause", "Clause") or "Clause"
                )
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
