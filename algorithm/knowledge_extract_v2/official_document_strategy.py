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

from .chunk_context import ChunkContextManager, ExtractionRequest
from .coverage import CoverageGap
from .graph_utils import canonical_name
from .ontology_compiler import ExtractionPlan
from .standard_strategy import STRATEGY_REGISTRY


_DOC_TITLE = re.compile(
    r"(?m)^(?:关于|关于印发|关于开展|关于同意|关于做好|关于进一步|关于切实|关于全面|关于组织)"
    r"\S{2,80}?(?:的通知|的公告|的决定|的意见|的办法|的规定|的批复|的请示|的报告|的函)[。]?$"
)
_SOURCE_DOC_NUMBER = re.compile(r"(?m)^(?P<number>[^\n]{1,30}?〔\d{4}〕\d+号)[。]?$")
_SOURCE_ORG = re.compile(r"(?:来文单位|发文机关|印发机关)[：:]\s*(?P<org>[^。\n]+)")
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

_ACTIVITY_VERBS = "建设、管理、评估、报送、监督、实施、试点、完善、推进、加强、组织"
_EXCHANGE_TYPES = "报送、审批、反馈、协同、监督、通知、备案"
_CONSTRAINT_TYPES = "时间约束、权限约束、安全约束、流程约束、质量约束、合规约束"
_MANDATORY_LEVELS = "必须、应当、原则上、鼓励、禁止"


def _has_class(plan: ExtractionPlan, type_id: str) -> bool:
    return type_id in plan.allowed_types


def _has_property(plan: ExtractionPlan, type_id: str, property_id: str) -> bool:
    spec = plan.class_specs.get(type_id)
    return spec is not None and any(prop.identifier == property_id for prop in spec.properties)


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

        units = _document_units(text) if _has_class(plan, "DocumentUnit") else []
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
                        "to_name": unit["unit_id"],
                        "evidence": {"quote": unit["unit_title"]},
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
        title = title_match.group(0).strip().rstrip("。")
        properties: dict[str, Any] = {}
        if _has_property(plan, "OfficialDocument", "documentTitle"):
            properties["documentTitle"] = title
        org = _SOURCE_ORG.search(text)
        if _has_property(plan, "OfficialDocument", "sourceOrganization") and org:
            properties["sourceOrganization"] = org.group("org").strip().rstrip("。")
        number = _SOURCE_DOC_NUMBER.search(text)
        if _has_property(plan, "OfficialDocument", "sourceDocumentNumber") and number:
            properties["sourceDocumentNumber"] = number.group("number").strip().rstrip("。")
        return {
            "type": "OfficialDocument",
            "name": title,
            "properties": properties,
            "evidence": {"quote": title},
        }

    def _document_unit_entity(
        self,
        plan: ExtractionPlan,
        unit: dict[str, Any],
    ) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        for prop_id, value in (
            ("unitId", unit["unit_id"]),
            ("unitType", unit["unit_type"]),
            ("unitTitle", unit["unit_title"]),
            ("unitOrder", unit["unit_order"]),
            ("originalText", unit["original_text"]),
        ):
            if _has_property(plan, "DocumentUnit", prop_id):
                properties[prop_id] = value
        return {
            "type": "DocumentUnit",
            "name": unit["unit_id"],
            "properties": properties,
            "evidence": {"quote": unit["unit_title"]},
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


__all__ = ["OfficialDocumentExtractionStrategy"]
