"""Entity mapping scores from attributes and declared contextual features."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EntityMappingInput:
    type: str
    properties: dict[str, Any]


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

    DEFAULT_WEIGHTS = {"attribute": 0.5, "context": 0.2}

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
        raw_weights = {k: v for k, v in (self.DEFAULT_WEIGHTS if weights is None else weights).items() if k in self.DEFAULT_WEIGHTS}
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
    ) -> MappingScore:
        """§17：实体候选 = 属性 + 上下文（不适用分量重新归一化）。"""
        type_id = entity.type
        feature_specs = plan.feature_specs.get(type_id, ())
        intrinsic = [spec for spec in feature_specs if spec.kind == "intrinsic"]
        context_features = [spec for spec in feature_specs if spec.kind == "context"]

        attribute = self._entity_attribute_score(entity, plan, intrinsic)
        context = self._context_score(entity, context_features)
        return self._compose(attribute, context)

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

        spec = plan.class_specs.get(entity.type)
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


    # ─────────────────────────── 合成 ───────────────────────────

    def _compose(
        self,
        attribute: float | None,
        context: float | None,
    ) -> MappingScore:
        components: dict[str, float | None] = {
            "attribute": attribute,
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

__all__ = ["EntityMappingInput", "MappingScore", "MappingScorer"]
