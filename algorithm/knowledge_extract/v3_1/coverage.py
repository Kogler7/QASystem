"""Phase 1：Coverage Gap 模型（§22）与通用 Coverage Auditor（§23 / Step 9）。

实现 §23 的五类覆盖检查（只发现 gap，不自动修复）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .graph_utils import canonical_name


@dataclass(frozen=True)
class CoverageGap:
    gap_id: str
    span_id: str
    gap_type: str
    evidence: str
    reason: str
    target_type: str | None = None
    severity: str = "info"
    expected_features: tuple[str, ...] = ()
    expected_relations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "span_id": self.span_id,
            "target_type": self.target_type,
            "gap_type": self.gap_type,
            "severity": self.severity,
            "evidence": self.evidence,
            "reason": self.reason,
            "expected_features": list(self.expected_features),
            "expected_relations": list(self.expected_relations),
        }


class CoverageAuditor:
    """§23 通用 Coverage Auditor：只发现 gap，不自动修复（§22）。"""

    def __init__(self) -> None:
        self._gap_counter = 0

    def audit(
        self,
        plan: Any,
        manager: Any,
        request: Any,
        entities: list[dict[str, Any]],
        relations: list[dict[str, Any]],
    ) -> list[CoverageGap]:
        gaps: list[CoverageGap] = []
        self._gap_counter = 0
        gaps.extend(self._feature_coverage(plan, entities))
        gaps.extend(self._evidence_coverage(entities, relations))
        gaps.extend(self._structural_coverage(plan, manager, entities))
        return gaps

    def _next_gap(
        self,
        gap_type: str,
        *,
        span_id: str,
        target_type: str | None,
        evidence: str,
        reason: str,
        severity: str = "info",
        expected_features: tuple[str, ...] = (),
        expected_relations: tuple[str, ...] = (),
    ) -> CoverageGap:
        self._gap_counter += 1
        return CoverageGap(
            gap_id=f"generic-{gap_type}-{self._gap_counter}",
            span_id=span_id,
            gap_type=gap_type,
            target_type=target_type,
            severity=severity,
            evidence=evidence,
            reason=reason,
            expected_features=expected_features,
            expected_relations=expected_relations,
        )

    # ── 23.2 Feature Coverage ──

    def _feature_coverage(
        self,
        plan: Any,
        entities: list[dict[str, Any]],
    ) -> list[CoverageGap]:
        gaps: list[CoverageGap] = []
        for entity in entities:
            type_id = entity.get("type", "")
            features = plan.feature_specs.get(type_id, ())
            intrinsic = [spec for spec in features if spec.kind == "intrinsic"]
            if not intrinsic:
                continue
            provided = set(entity.get("properties") or {})
            missing = [spec for spec in intrinsic if spec.identifier not in provided]
            if not missing:
                continue
            hard = any(spec.required is True for spec in missing)
            gaps.append(
                self._next_gap(
                    "feature_coverage",
                    span_id=_entity_span(entity),
                    target_type=type_id,
                    severity="warning" if hard else "info",
                    evidence=str(entity.get("name") or ""),
                    reason=(
                        f"{type_id}::{entity.get('name')} 缺少特性 "
                        f"{', '.join(spec.identifier for spec in missing)}"
                        + ("（本体声明 required，硬错误）" if hard else "（提示，非 required）")
                    ),
                    expected_features=tuple(spec.identifier for spec in missing),
                )
            )
        return gaps


    # ── 23.4 Evidence Coverage ──

    def _evidence_coverage(
        self,
        entities: list[dict[str, Any]],
        relations: list[dict[str, Any]],
    ) -> list[CoverageGap]:
        gaps: list[CoverageGap] = []
        for entity in entities:
            if not _has_evidence(entity):
                gaps.append(
                    self._next_gap(
                        "evidence_missing",
                        span_id=_entity_span(entity),
                        target_type=entity.get("type", ""),
                        severity="warning",
                        evidence=str(entity.get("name") or ""),
                        reason=f"{entity.get('type')}::{entity.get('name')} 缺少 evidence（§23.4）",
                    )
                )
        return gaps

    # ── AC-8 结构漏抽：带结构元数据的 chunk 必须绑定实体 ──

    def _structural_coverage(
        self,
        plan: Any,
        manager: Any,
        entities: list[dict[str, Any]],
    ) -> list[CoverageGap]:
        gaps: list[CoverageGap] = []
        bound_ids = {cid for entity in entities for evidence in
                     [entity.get("evidence") or {}, *entity.get("evidences", [])]
                     for cid in evidence.get("source_block_ids", [])}
        unbound = [e for e in entities if not (e.get("evidence") or {}).get("source_block_ids")]
        for chunk in manager:
            if not (
                chunk.clause_number
                or chunk.section_id
                or (chunk.metadata or {}).get("unit")
            ):
                continue
            if chunk.chunk_id in bound_ids or any(_entity_bound_to_chunk(entity, chunk) for entity in unbound):
                continue
            gaps.append(
                self._next_gap(
                    "structural_coverage",
                    span_id=f"{chunk.chunk_id}:0:{len(chunk.text)}",
                    target_type=None,
                    severity="warning",
                    evidence=chunk.text[:80],
                    reason=f"带结构元数据的 chunk {chunk.chunk_id} 未绑定任何实体（结构漏抽）",
                )
            )
        return gaps


def _entity_span(entity: dict[str, Any]) -> str:
    evidence = entity.get("evidence") or {}
    block_ids = evidence.get("source_block_ids") or []
    if block_ids:
        return f"{block_ids[0]}:{evidence.get('start_char') or 0}:{evidence.get('end_char') or 0}"
    return ""


def _has_evidence(item: dict[str, Any]) -> bool:
    return bool((item.get("evidence") or {}).get("quote"))


def _entity_bound_to_chunk(entity: dict[str, Any], chunk: Any) -> bool:
    evidence = entity.get("evidence") or {}
    if chunk.chunk_id in (evidence.get("source_block_ids") or []):
        return True
    quote = (evidence.get("quote") or "").strip()
    if quote:
        return re.sub(r"\s+", "", quote) in re.sub(r"\s+", "", chunk.text or "")
    return False


__all__ = ["CoverageAuditor", "CoverageGap"]
