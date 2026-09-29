"""Phase 1 Step 8：Mapping Scorer（§17-§19）。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md：
- §17：``score = w_attr * attribute_score + w_rel * relation_score
  + w_ctx * context_score``，w_attr + w_rel + w_ctx = 1；权重与阈值必须可配置，
  正式上线前通过 benchmark 校准；参考阈值 accepted >= 0.85、
  0.70-0.85 低置信度、< 0.70 拒绝映射（§34）；
- §18 属性匹配：required feature presence（本体未声明 required 时不惩罚）、
  optional feature coverage、datatype/enum/identity consistency、evidence support；
- §19 关系匹配：relation identifier / 端点类型合法性、cardinality、
  endpoint existence、local evidence；
- §16 L4：只有本体 FeatureSpec 明确允许上下文推断时才接受 contextual 分数，
  否则该分量视为不适用（不猜测）。

本模块只做确定性评分；默认“注释模式”不影响抽取结果，`apply_mapping_threshold`
开启后才会按阈值过滤 rejected 候选（需先在校准后使用）。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import math
from typing import Any, Iterable


CONFIDENCE_METHOD = "mapping_bounded_multi_signal_v3"
CONFIDENCE_SIGNAL_WEIGHTS = {
    "mapping": 0.55,
    "evidence": 0.25,
    "canonical_mapping": 0.20,
}
_AGGREGATION_DETAIL_KEYS = frozenset(
    {
        "aggregation",
        "source_scores",
        "source_mapping_scores",
        "observation_count",
        "score_min",
        "score_max",
        "score_spread",
    }
)


def _base_confidence_details(item: dict[str, Any]) -> dict[str, Any]:
    details = item.get("confidence_details") or {}
    if not isinstance(details, dict):
        return {}
    return {
        key: deepcopy(value)
        for key, value in details.items()
        if key not in _AGGREGATION_DETAIL_KEYS
    }


def mapping_confidence_payload(
    score: float | None,
    status: str,
    *,
    evidences: Iterable[Any] | None = None,
    canonical_mapping_valid: bool | None = None,
    warnings: Iterable[str] = (),
    aggregate_evidence: bool = True,
    aggregate_canonical_mapping: bool = True,
) -> dict[str, Any]:
    """Build a mapping-bounded heuristic confidence from quality signals.

    ``mapping_score`` is the deterministic baseline and therefore an upper bound
    for ``confidence``.  Evidence/canonicalization may only lower that baseline.
    Relation mapping already includes evidence and canonical legality, so callers
    can keep their decision gates while excluding those correlated signals from
    the numeric aggregation.
    """
    if score is None:
        return {}
    mapping_score = float(score)
    if not math.isfinite(mapping_score):
        raise ValueError("映射置信度必须是有限数值")
    mapping_score = round(min(1.0, max(0.0, mapping_score)), 4)

    signal_values: dict[str, float] = {"mapping": mapping_score}
    gates: list[str] = []

    if evidences is not None:
        evidence_items = list(evidences)
        if evidence_items:
            if aggregate_evidence:
                signal_values["evidence"] = max(
                    _evidence_quality(item) for item in evidence_items
                )
        else:
            # Missing evidence is a decision gate rather than a fabricated score.
            gates.append("missing_evidence")
    if canonical_mapping_valid is not None:
        if aggregate_canonical_mapping:
            signal_values["canonical_mapping"] = (
                1.0 if canonical_mapping_valid else 0.0
            )
        if not canonical_mapping_valid:
            gates.append("canonical_mapping_missing")
    warning_set = {str(item) for item in warnings}
    if "RELATION_CARDINALITY_EXCEEDED" in warning_set:
        gates.append("cardinality_exceeded")
    if status == "low_confidence":
        gates.append("mapping_low_confidence")
    elif status == "rejected":
        gates.append("mapping_rejected")

    applicable_weight = sum(CONFIDENCE_SIGNAL_WEIGHTS[key] for key in signal_values)
    if applicable_weight <= 0:
        confidence = 0.0
    elif any(value <= 0.0 for value in signal_values.values()):
        confidence = 0.0
    else:
        confidence = math.exp(
            sum(
                (CONFIDENCE_SIGNAL_WEIGHTS[key] / applicable_weight) * math.log(value)
                for key, value in signal_values.items()
            )
        )

    if "missing_evidence" in gates:
        confidence = min(confidence, 0.8499)
    if "cardinality_exceeded" in gates:
        confidence = min(confidence, 0.6999)
    if "canonical_mapping_missing" in gates:
        confidence = 0.0
    # Supporting signals can reduce a mapping score, never promote it above the
    # deterministic ontology-fit baseline.
    confidence = min(confidence, mapping_score)
    confidence = round(min(1.0, max(0.0, confidence)), 4)

    confidence_status = _confidence_status(confidence)
    if "missing_evidence" in gates and confidence_status == "accepted":
        confidence_status = "needs_review"
    if "mapping_low_confidence" in gates and confidence_status == "accepted":
        confidence_status = "needs_review"
    if "canonical_mapping_missing" in gates or "mapping_rejected" in gates:
        confidence_status = "rejected"
    gate_status = (
        "blocked"
        if {"canonical_mapping_missing", "mapping_rejected"}.intersection(gates)
        else "review"
        if gates
        else "passed"
    )

    return {
        "mapping_score": mapping_score,
        "confidence": confidence,
        "confidence_status": confidence_status,
        "gate_status": gate_status,
        "confidence_details": {
            "type": "heuristic_score",
            "source": "multi_signal",
            "method": CONFIDENCE_METHOD,
            "calibrated": False,
            "status": confidence_status,
            "mapping_status": status,
            "mapping_score_status": status,
            "signals": {
                key: {
                    "score": round(value, 4),
                    "weight": CONFIDENCE_SIGNAL_WEIGHTS[key],
                }
                for key, value in signal_values.items()
            },
            "gates": gates,
        },
    }


def _evidence_quality(item: Any) -> float:
    payload = item.to_dict() if hasattr(item, "to_dict") else item
    if not isinstance(payload, dict):
        return 0.0
    quote = bool(str(payload.get("quote") or "").strip())
    span = bool(str(payload.get("span_id") or "").strip())
    sources = bool(payload.get("source_block_ids"))
    offsets = payload.get("start_char") is not None and payload.get("end_char") is not None
    completeness = sum((quote, span, sources, offsets)) / 4.0
    type_factor = {
        "explicit": 1.0,
        "structural": 0.9,
        "contextual": 0.75,
    }.get(str(payload.get("evidence_type") or "explicit"), 0.75)
    return round(completeness * type_factor, 4)


def _confidence_status(value: float) -> str:
    if value >= 0.85:
        return "accepted"
    if value >= 0.70:
        return "needs_review"
    return "rejected"


def merge_confidence_payload(
    first: dict[str, Any],
    second: dict[str, Any],
) -> dict[str, Any]:
    """Merge duplicate-fact confidence deterministically and conservatively."""
    candidates = [item for item in (first, second) if item.get("confidence") is not None]
    if not candidates:
        return {}
    if len(candidates) == 1:
        return deepcopy(candidates[0])

    def _tie_break_details(item: dict[str, Any]) -> dict[str, Any]:
        details = _base_confidence_details(item)
        # These fields are recomputed from all observations below.  Excluding
        # them keeps repeated pairwise merges associative and order-independent.
        details.pop("status", None)
        details.pop("gates", None)
        return details

    selected = min(
        candidates,
        key=lambda item: (
            float(item["confidence"]),
            float(item.get("mapping_score", float("inf"))),
            json.dumps(
                _tie_break_details(item),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        ),
    )

    def _status_value(item: dict[str, Any], key: str, default: str) -> str:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
        details = item.get("confidence_details") or {}
        detail_value = details.get(key) if isinstance(details, dict) else None
        if (
            key == "confidence_status"
            and not isinstance(detail_value, str)
            and isinstance(details, dict)
        ):
            detail_value = details.get("status")
        return detail_value if isinstance(detail_value, str) and detail_value else default

    confidence_status_rank = {
        "accepted": 0,
        "needs_review": 1,
        "rejected": 2,
    }
    gate_status_rank = {
        "passed": 0,
        "review": 1,
        "blocked": 2,
    }
    merged_confidence_status = max(
        (
            _status_value(item, "confidence_status", _confidence_status(float(item["confidence"])))
            for item in candidates
        ),
        key=lambda value: confidence_status_rank.get(value, -1),
    )
    merged_gate_status = max(
        (_status_value(item, "gate_status", "passed") for item in candidates),
        key=lambda value: gate_status_rank.get(value, -1),
    )
    merged_gates: set[str] = set()
    for item in candidates:
        item_details = item.get("confidence_details") or {}
        if not isinstance(item_details, dict):
            continue
        item_gates = item_details.get("gates")
        if isinstance(item_gates, (list, tuple, set)):
            merged_gates.update(str(gate) for gate in item_gates)

    source_scores: list[float] = []
    source_mapping_scores: list[float] = []
    for item in candidates:
        details = item.get("confidence_details") or {}
        previous = details.get("source_scores") if isinstance(details, dict) else None
        values = previous if isinstance(previous, list) else [item["confidence"]]
        source_scores.extend(float(value) for value in values)
        previous_mapping = details.get("source_mapping_scores") if isinstance(details, dict) else None
        mapping_values = previous_mapping if isinstance(previous_mapping, list) else [item.get("mapping_score")]
        source_mapping_scores.extend(
            float(value) for value in mapping_values if value is not None
        )
    details = _base_confidence_details(selected)
    score_min = min(source_scores)
    score_max = max(source_scores)
    details.update(
        {
            "aggregation": "minimum_duplicate_score",
            "source_scores": sorted(round(value, 4) for value in source_scores),
            "source_mapping_scores": sorted(
                round(value, 4) for value in source_mapping_scores
            ),
            "observation_count": len(source_scores),
            "score_min": round(score_min, 4),
            "score_max": round(score_max, 4),
            "score_spread": round(score_max - score_min, 4),
            "status": merged_confidence_status,
            "gates": sorted(merged_gates),
        }
    )
    payload = {
        "confidence": round(float(selected["confidence"]), 4),
        "confidence_status": merged_confidence_status,
        "gate_status": merged_gate_status,
        "confidence_details": details,
    }
    if selected.get("mapping_score") is not None:
        payload["mapping_score"] = round(float(selected["mapping_score"]), 4)
    return payload


@dataclass(frozen=True)
class MappingScore:
    score: float
    status: str  # accepted | low_confidence | rejected
    breakdown: dict[str, float | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "status": self.status,
            "breakdown": dict(self.breakdown),
        }


class MappingScorer:
    """§17：可配置权重的候选映射评分器。"""

    DEFAULT_WEIGHTS = {"attribute": 0.5, "relation": 0.3, "context": 0.2}

    def __init__(
        self,
        weights: dict[str, float] | None = None,
        *,
        accepted_threshold: float = 0.85,
        low_confidence_threshold: float = 0.70,
    ):
        if not (0.0 <= low_confidence_threshold <= accepted_threshold <= 1.0):
            raise ValueError(
                "阈值需满足 0 <= low_confidence <= accepted <= 1，"
                f"收到 accepted={accepted_threshold}, low={low_confidence_threshold}"
            )
        raw_weights = dict(self.DEFAULT_WEIGHTS if weights is None else weights)
        unknown = set(raw_weights).difference(self.DEFAULT_WEIGHTS)
        if unknown:
            raise ValueError(f"未知评分权重: {', '.join(sorted(unknown))}")
        for key in self.DEFAULT_WEIGHTS:
            raw_weights.setdefault(key, 0.0)
        normalized_weights: dict[str, float] = {}
        for key, raw_value in raw_weights.items():
            value = float(raw_value)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"评分权重 {key} 必须是非负有限数值")
            normalized_weights[key] = value
        total = sum(normalized_weights.values())
        if total <= 0:
            raise ValueError("评分权重之和必须大于 0")
        self.weights = {key: value / total for key, value in normalized_weights.items()}
        self.accepted_threshold = float(accepted_threshold)
        self.low_confidence_threshold = float(low_confidence_threshold)

    # ─────────────────────────── 实体评分 ───────────────────────────

    def score_entity(
        self,
        entity: Any,
        plan: Any,
        relations: Iterable[Any] = (),
    ) -> MappingScore:
        """§17：实体候选 = 属性 + 关系参与 + 上下文（不适用分量重新归一化）。"""
        type_id = entity.canonical_type or entity.proposed_type
        feature_specs = plan.feature_specs.get(type_id, ())
        intrinsic = [spec for spec in feature_specs if spec.kind == "intrinsic"]
        relation_features = [spec for spec in feature_specs if spec.kind == "relation"]
        context_features = [spec for spec in feature_specs if spec.kind == "context"]

        attribute = self._entity_attribute_score(entity, plan, intrinsic)
        relation = (
            self._entity_relation_score(entity, list(relations), relation_features)
            if relation_features
            else None
        )
        context = self._context_score(entity, context_features)
        return self._compose(attribute, relation, context)

    def _entity_attribute_score(
        self,
        entity: Any,
        plan: Any,
        intrinsic: list[Any],
    ) -> float | None:
        if not intrinsic:
            return None  # 类没有可观测属性时分量不适用
        properties = entity.properties or {}
        provided = set(properties)
        covered = [spec for spec in intrinsic if spec.identifier in provided]
        coverage = len(covered) / len(intrinsic)

        # §18：本体声明 required 时才纳入 required penalty
        required = [spec for spec in intrinsic if spec.required is True]
        if required:
            required_present = sum(1 for spec in required if spec.identifier in provided)
            required_ratio = required_present / len(required)
            return round(0.6 * required_ratio + 0.4 * coverage, 4)

        spec = plan.class_specs.get(entity.canonical_type or entity.proposed_type)
        valid_property_ids = {prop.identifier for prop in spec.properties} if spec else set()
        validity = (
            len(provided & valid_property_ids) / len(provided) if provided else 0.0
        )
        identity_hit = bool(
            spec
            and spec.identity_properties
            and any(identity in provided for identity in spec.identity_properties)
        )
        identity_score = 1.0 if identity_hit else 0.0
        return round(0.5 * coverage + 0.3 * validity + 0.2 * identity_score, 4)

    def _entity_relation_score(
        self,
        entity: Any,
        relations: list[Any],
        relation_features: list[Any],
    ) -> float:
        """§19 实体侧：候选作为源端参与的关系覆盖。"""
        feature_relations = {
            spec.identifier for spec in relation_features
        }
        participated = {
            relation.canonical_relation or relation.proposed_relation
            for relation in relations
            if relation.from_candidate_id == entity.candidate_id
        }
        if not feature_relations:
            return 0.0
        return round(len(participated & feature_relations) / len(feature_relations), 4)

    def _context_score(self, entity: Any, context_features: list[Any]) -> float | None:
        """§16 L4：只有本体允许上下文推断时才给分。"""
        inferable = [spec for spec in context_features if spec.context_inferable is True]
        if not inferable:
            return None
        provided = sum(
            1
            for spec in inferable
            if spec.identifier in (entity.properties or {})
        )
        return round(provided / len(inferable), 4)

    # ─────────────────────────── 关系评分 ───────────────────────────

    def score_relation(
        self,
        relation: Any,
        plan: Any,
        *,
        include_cardinality: bool = True,
    ) -> MappingScore:
        """§19：关系候选 = 证据（attribute） + 合法性（relation） + cardinality（context）。"""
        legality = 1.0 if relation.canonical_relation else 0.0
        evidence = 1.0 if relation.evidence else 0.0
        cardinality = None if not include_cardinality else (
            0.5
            if "RELATION_CARDINALITY_EXCEEDED" in (relation.warnings or [])
            else 1.0
        )
        return self._compose(evidence, legality, cardinality)

    # ─────────────────────────── 合成 ───────────────────────────

    def _compose(
        self,
        attribute: float | None,
        relation: float | None,
        context: float | None,
    ) -> MappingScore:
        components: dict[str, float | None] = {
            "attribute": attribute,
            "relation": relation,
            "context": context,
        }
        applicable = {
            key for key, value in components.items() if value is not None
        }
        if not applicable:
            score = 0.0
        else:
            weight_sum = sum(self.weights[key] for key in applicable)
            if weight_sum <= 0:
                score = 0.0
            else:
                score = round(
                    sum(self.weights[key] * float(components[key]) for key in applicable)
                    / weight_sum,
                    4,
                )
        score = min(1.0, max(0.0, score))
        return MappingScore(
            score=score,
            status=self._status(score),
            breakdown=components,
        )

    def _status(self, score: float) -> str:
        if score >= self.accepted_threshold:
            return "accepted"
        if score >= self.low_confidence_threshold:
            return "low_confidence"
        return "rejected"

__all__ = [
    "CONFIDENCE_METHOD",
    "CONFIDENCE_SIGNAL_WEIGHTS",
    "MappingScore",
    "MappingScorer",
    "mapping_confidence_payload",
    "merge_confidence_payload",
]
