"""Phase 1 Step 7：复合元素（§21 CompositeElementHandler）。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md：
- §21 CompositeSpec：composite_type / member_type / membership_relation /
  combination_property / allowed_combinations；
- AC-6：member extraction、membership relation、combination type 全部正确；
- 不得把整个复合规则只保存成一段字符串：CompositeRule 必须拆出 SimpleRule
  成员并建立 hasSubRule。

默认示例（规则本体）：``CompositeRule hasSubRule SimpleRule`` +
``combinationType = AND | OR | SEQUENCE``。

本模块只做确定性组装与校验（不调用 LLM）；Process / FaultTree 等复合类型
通过同一 CompositeSpec 机制扩展。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .chunk_context import ChunkContextManager
from .candidate_models import RelationCandidate
from .evidence import locate_quote
from .graph_utils import canonical_name
from .ontology_schema import OntologySchema


_MEMBERSHIP_RELATION_IDS = {"hassubrule", "hasmember"}
_COMBINATION_PROPERTY_HINTS = ("combinationtype", "combination")


def _parse_combinations(comment: str) -> frozenset[str]:
    return frozenset(re.findall(r"\b[A-Z][A-Z0-9_]*\b", comment or ""))


@dataclass(frozen=True)
class CompositeSpec:
    """§21：复合元素声明（由本体编译发现或显式配置）。"""

    composite_type: str
    member_type: str
    membership_relation: str
    combination_property: str | None = None
    allowed_combinations: frozenset[str] = frozenset()
    min_members: int = 2


def discover_composite_specs(schema: OntologySchema) -> dict[str, CompositeSpec]:
    """从本体自动发现复合元素：membership 关系 + combination 属性。"""
    specs: dict[str, CompositeSpec] = {}
    for ontology_class in schema.classes:
        for relation in schema.relations:
            if relation.source != ontology_class.identifier:
                continue
            if canonical_name(relation.identifier) not in _MEMBERSHIP_RELATION_IDS:
                continue
            combination_property: str | None = None
            allowed_combinations: frozenset[str] = frozenset()
            for prop in ontology_class.properties:
                prop_key = canonical_name(prop.identifier)
                if any(hint in prop_key for hint in _COMBINATION_PROPERTY_HINTS):
                    combination_property = prop.identifier
                    allowed_combinations = _parse_combinations(prop.comment)
                    break
            specs[ontology_class.identifier] = CompositeSpec(
                composite_type=ontology_class.identifier,
                member_type=relation.target,
                membership_relation=relation.identifier,
                combination_property=combination_property,
                allowed_combinations=allowed_combinations,
                min_members=2,
            )
    return specs


def _composite_text(entity: Any) -> str:
    properties = entity.properties or {}
    return str(properties.get("ruleText") or properties.get("text") or entity.name or "")


def _member_supported_by(composite_text: str, member: Any) -> bool:
    """成员是否被复合文本支撑：ruleText 子串，或 triggerCondition + action 均出现。"""
    properties = member.properties or {}
    rule_text = str(properties.get("ruleText") or properties.get("text") or "")
    trigger = str(properties.get("triggerCondition") or "")
    action = str(properties.get("action") or "")
    composite_key = canonical_name(composite_text)
    if rule_text and canonical_name(rule_text) in composite_key:
        return True
    return bool(
        trigger
        and action
        and canonical_name(trigger) in composite_key
        and canonical_name(action) in composite_key
    )


def _source_block_ids(entity: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        str(block_id)
        for evidence in (getattr(entity, "evidence", ()) or ())
        for block_id in (getattr(evidence, "source_block_ids", ()) or ())
        if block_id
    ))


def _same_known_document(left: Any, right: Any, manager: ChunkContextManager) -> bool:
    def scopes(entity: Any) -> set[tuple[str, str]]:
        resolved: set[tuple[str, str]] = set()
        for block_id in _source_block_ids(entity):
            try:
                chunk = manager.get_chunk(block_id)
            except KeyError:
                continue
            resolved.add((
                str(chunk.document_id or "").strip(),
                str((chunk.metadata or {}).get("source_file") or "").strip(),
            ))
        return resolved

    left_scopes = scopes(left)
    right_scopes = scopes(right)
    for left_doc, left_file in left_scopes:
        for right_doc, right_file in right_scopes:
            if left_doc and right_doc:
                if left_doc != right_doc:
                    continue
                if left_file and right_file:
                    if left_file == right_file:
                        return True
                    continue
                return True
            if left_file and right_file:
                if left_file == right_file:
                    return True
                continue
    shared_blocks = set(_source_block_ids(left)) & set(_source_block_ids(right))
    return bool(shared_blocks) or len(manager) == 1


def _bind_membership_evidence(member: Any, composite: Any, manager: ChunkContextManager) -> Any | None:
    member_blocks = _source_block_ids(member)
    composite_blocks = _source_block_ids(composite)
    allowed_blocks = member_blocks or composite_blocks
    if not allowed_blocks and len(manager) == 1:
        allowed_blocks = tuple(manager.chunk_ids)
    if not allowed_blocks:
        return None
    for candidate_text in (
        (member.properties or {}).get("ruleText"),
        (member.properties or {}).get("triggerCondition"),
        (composite.properties or {}).get("ruleText"),
    ):
        if candidate_text:
            located = locate_quote(manager, candidate_text, chunk_ids=allowed_blocks)
            if located is not None:
                return located
    return None


class CompositeElementHandler:
    """§21：复合元素组装与校验（只做确定性操作，不调用 LLM）。"""

    def __init__(self, specs: dict[str, CompositeSpec] | None = None):
        self.specs = dict(specs or {})

    @classmethod
    def from_plan(cls, plan: Any) -> "CompositeElementHandler":
        return cls(getattr(plan, "composite_specs", {}) or {})

    def assemble(
        self,
        pool: Any,
        plan: Any,
        manager: ChunkContextManager,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """组装缺失的成员关系并校验复合实例；返回问题列表。"""
        issues: list[dict[str, Any]] = []
        for spec in self.specs.values():
            composites = [
                entity
                for entity in pool.accepted_entities()
                if entity.canonical_type == spec.composite_type
            ]
            if not composites:
                continue
            members = {
                entity.candidate_id: entity
                for entity in pool.accepted_entities()
                if pool.schema.is_assignable(entity.canonical_type or "", spec.member_type)
            }
            existing = {
                (relation.from_candidate_id, relation.to_candidate_id)
                for relation in pool.accepted_relations()
                if relation.canonical_relation == spec.membership_relation
            }
            for composite in composites:
                composite_text = _composite_text(composite)
                for member_id, member in members.items():
                    if (composite.candidate_id, member_id) in existing:
                        continue
                    if not _same_known_document(composite, member, manager):
                        continue
                    if not _member_supported_by(composite_text, member):
                        continue
                    evidence = _bind_membership_evidence(member, composite, manager)
                    if evidence is None:
                        if warnings is not None:
                            warnings.append(
                                {
                                    "code": "COMPOSITE_MEMBERSHIP_EVIDENCE_NOT_FOUND",
                                    "message": (
                                        f"复合元素 {composite.name} 与成员 {member.name} "
                                        "缺少可定位的同域证据，已跳过自动关系"
                                    ),
                                    "path": f"composite.{spec.composite_type}",
                                    "value": {
                                        "from_id": composite.candidate_id,
                                        "to_id": member_id,
                                        "rel_type": spec.membership_relation,
                                    },
                                }
                            )
                        continue
                    candidate = RelationCandidate(
                        candidate_id="",
                        from_candidate_id=composite.candidate_id,
                        to_candidate_id=member_id,
                        proposed_relation=spec.membership_relation,
                        canonical_relation=None,
                        evidence=[evidence],
                    )
                    resolved = pool.add_relation(candidate, warnings=warnings)
                    if resolved is None:
                        continue
                    issues.append(
                        {
                            "code": "AUTO_MEMBERSHIP_ESTABLISHED",
                            "message": (
                                f"复合元素 {composite.name} 自动建立 "
                                f"{spec.membership_relation} -> {member.name}"
                            ),
                            "path": f"composite.{spec.composite_type}",
                        }
                    )

                self._validate_composite(
                    issues,
                    warnings,
                    spec,
                    composite,
                    pool,
                )
        return issues

    def _validate_composite(
        self,
        issues: list[dict[str, Any]],
        warnings: list[dict[str, Any]] | None,
        spec: CompositeSpec,
        composite: Any,
        pool: Any,
    ) -> None:
        member_ids = {
            relation.to_candidate_id
            for relation in pool.accepted_relations()
            if relation.from_candidate_id == composite.candidate_id
            and relation.canonical_relation == spec.membership_relation
        }
        if len(member_ids) < spec.min_members:
            self._warn(
                warnings,
                "INSUFFICIENT_COMPOSITE_MEMBERS",
                f"复合元素 {composite.name} 成员数 {len(member_ids)} < {spec.min_members}",
                f"composite.{spec.composite_type}",
            )
            issues.append(
                {
                    "code": "INSUFFICIENT_COMPOSITE_MEMBERS",
                    "message": f"复合元素 {composite.name} 成员数不足",
                    "path": f"composite.{spec.composite_type}",
                }
            )

        if not spec.combination_property:
            return
        combination = (composite.properties or {}).get(spec.combination_property)
        if combination is None:
            self._warn(
                warnings,
                "MISSING_COMBINATION_TYPE",
                f"复合元素 {composite.name} 缺少 {spec.combination_property}",
                f"composite.{spec.composite_type}.{spec.combination_property}",
            )
            issues.append(
                {
                    "code": "MISSING_COMBINATION_TYPE",
                    "message": f"复合元素 {composite.name} 缺少组合类型",
                    "path": f"composite.{spec.composite_type}",
                }
            )
        elif spec.allowed_combinations and combination not in spec.allowed_combinations:
            self._warn(
                warnings,
                "INVALID_COMBINATION_TYPE",
                (
                    f"复合元素 {composite.name} 的组合类型 {combination!r} 不在 "
                    f"允许集合 {sorted(spec.allowed_combinations)}"
                ),
                f"composite.{spec.composite_type}.{spec.combination_property}",
            )
            issues.append(
                {
                    "code": "INVALID_COMBINATION_TYPE",
                    "message": f"复合元素 {composite.name} 的组合类型不合法",
                    "path": f"composite.{spec.composite_type}",
                }
            )

    @staticmethod
    def _warn(
        warnings: list[dict[str, Any]] | None,
        code: str,
        message: str,
        path: str,
    ) -> None:
        if warnings is not None:
            warnings.append({"code": code, "message": message, "path": path})


__all__ = ["CompositeElementHandler", "CompositeSpec", "discover_composite_specs"]
