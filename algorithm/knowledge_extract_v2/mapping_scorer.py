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

from dataclasses import dataclass
from typing import Any, Iterable


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
        for key in self.DEFAULT_WEIGHTS:
            raw_weights.setdefault(key, 0.0)
        total = sum(raw_weights.values())
        if total <= 0:
            raise ValueError("评分权重之和必须大于 0")
        self.weights = {key: value / total for key, value in raw_weights.items()}
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

    def score_relation(self, relation: Any, plan: Any) -> MappingScore:
        """§19：关系候选 = 证据（attribute） + 合法性（relation） + cardinality（context）。"""
        legality = 1.0 if relation.canonical_relation else 0.0
        evidence = 1.0 if relation.evidence else 0.0
        cardinality = (
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

__all__ = ["MappingScore", "MappingScorer"]
