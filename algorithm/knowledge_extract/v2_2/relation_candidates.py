"""按本体关系签名和局部证据召回候选，不生成全文笛卡尔积。

每个候选固定源实体、关系标识和尾实体，并带上证明它所需的原文片段。
同句/同行是局部候选；相邻句、相邻行和相邻切片是有界邻接；明确条款号另做补查。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .graph_utils import canonical_name
from .ontology_compiler import ExtractionPlan, RelationSpec
from .relation_context import RelationIndex, TextUnit, cited_units, clause_refs
from .relation_resolver import relation_token, withheld_relation_tokens


# 弱共现边：评测里几乎全是多抽，召回阶段直接跳过，不进模型。
_WEAK_RELATION_TOKENS = frozenset({"relatedto"})

# 签发 / 适用类边：锚点或局部共现时按原文角色线索过滤，避免收文对象、联系人挂成签发方。
_ISSUED_BY_TARGET_CUES = ("办公室", "办公厅", "秘书处")
_ISSUED_BY_TEXT_CUES = ("签发", "印发", "来文单位", "发文", "发布")
_APPLIES_TO_TARGET_CUES = ("各", "子公司", "所属", "单位", "部门", "企业")
_APPLIES_TO_TEXT_CUES = ("主送", "抄送", "报送", "适用于", "贯彻执行", "落实")
_CONTACT_NOISE = ("联系人", "联系电话", "电话：", "电话:")


@dataclass(frozen=True)
class EvidenceFragment:
    fragment_id: str
    chunk_id: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class RelationCandidate:
    candidate_id: str
    source_id: str
    target_id: str
    source_type: str
    target_type: str
    source_name: str
    target_name: str
    relation_id: str
    relation_key: str
    comment: str
    signature: str
    scope_kind: str
    fragments: tuple[EvidenceFragment, ...]
    anchor: bool = False

    def pair_row(self) -> dict[str, Any]:
        return {
            "pair_id": self.candidate_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "source_type": self.source_type,
            "target_type": self.target_type,
            "source_name": self.source_name,
            "target_name": self.target_name,
            "relation_id": self.relation_key,
            "relation_ids": [self.relation_key],
            "preserve_on_none": self.anchor,
        }


def _fragment(unit: TextUnit) -> EvidenceFragment:
    raw = f"{unit.chunk_id}:{unit.start}:{unit.end}"
    return EvidenceFragment(
        fragment_id="f" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10],
        chunk_id=unit.chunk_id,
        start=unit.start,
        end=unit.end,
        text=unit.text,
    )


def _candidate_id(source_id: str, relation_key: str, target_id: str, fragments: tuple[EvidenceFragment, ...], kind: str) -> str:
    raw = "|".join((kind, source_id, relation_key, target_id, *(item.fragment_id for item in fragments)))
    return "c-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _type_keys(plan: ExtractionPlan, type_name: str) -> frozenset[str]:
    result: set[str] = set()
    pending = [type_name]
    while pending:
        current = pending.pop()
        key = canonical_name(current)
        if not key or key in result:
            continue
        result.add(key)
        spec = plan.class_specs.get(current)
        if spec is None:
            spec = next(
                (value for name, value in plan.class_specs.items() if canonical_name(name) == key),
                None,
            )
        for parent in (spec.parents if spec else ()):
            pending.append(parent)
    return frozenset(result)


def _assignable(keys: frozenset[str], expected: str) -> bool:
    return canonical_name(expected) in keys


def active_signatures(plan: ExtractionPlan, route_kind: str) -> list[RelationSpec]:
    """本体允许的签名进入关系阶段。结构组装边与弱共现边跳过，避免重复或低质建边。"""
    skip = withheld_relation_tokens(route_kind) | _WEAK_RELATION_TOKENS
    specs: list[RelationSpec] = []
    seen: set[tuple[str, str, str]] = set()
    for group in plan.relation_specs.values():
        for spec in group:
            if relation_token(spec.relation_id) in skip:
                continue
            key = (canonical_name(spec.relation_id), canonical_name(spec.source), canonical_name(spec.target))
            if key in seen:
                continue
            seen.add(key)
            specs.append(spec)
    return specs


def _evidence_text(left: TextUnit, right: TextUnit | None) -> str:
    if right is None or (left.chunk_id == right.chunk_id and left.start == right.start and left.end == right.end):
        return left.text
    if left.chunk_id == right.chunk_id:
        return f"{left.text}\n{right.text}"
    return f"{left.text}\n{right.text}"


def _looks_like_contact(name: str, text: str) -> bool:
    compact = " ".join((name or "").split())
    if any(token in compact for token in ("联系人", "联系电话")):
        return True
    if not any(token in text for token in _CONTACT_NOISE):
        return False
    # 原文出现联系人栏时，拒绝不像组织全称的短署名（如「信息公司张振威」）。
    if any(cue in compact for cue in ("办公室", "办公厅", "各", "子公司", "集团有限公司", "股份有限公司")):
        return False
    return 2 <= len(compact) <= 16


def _role_gate(spec: RelationSpec, *, text: str, source_name: str, target_name: str) -> bool:
    """对易混角色边做原文线索门禁。其他签名一律放行，避免误伤召回。"""
    token = relation_token(spec.relation_id)
    if token == "issuedby":
        if _looks_like_contact(target_name, text):
            return False
        if any(cue in target_name for cue in _ISSUED_BY_TARGET_CUES):
            return True
        if any(cue in text for cue in _ISSUED_BY_TEXT_CUES):
            # 有签发线索时仍拒绝明显收文对象。
            if "各" in target_name and ("公司" in target_name or "企业" in target_name or "单位" in target_name):
                return False
            return True
        return False
    if token == "appliesto":
        if _looks_like_contact(target_name, text):
            return False
        # 办公室等签发主体不当适用对象。
        if any(cue in target_name for cue in _ISSUED_BY_TARGET_CUES):
            return False
        if any(cue in target_name for cue in _APPLIES_TO_TARGET_CUES):
            return True
        if any(cue in text for cue in _APPLIES_TO_TEXT_CUES):
            return True
        return False
    return True


def _fragments_for(index: RelationIndex, left: TextUnit, right: TextUnit | None = None) -> tuple[EvidenceFragment, ...]:
    if right is None or (left.chunk_id == right.chunk_id and left.start == right.start and left.end == right.end):
        return (_fragment(left),)
    if left.chunk_id == right.chunk_id:
        chunk = index.chunk(left.chunk_id)
        text = (chunk.text if chunk else "")[left.start:right.end]
        raw = f"{left.chunk_id}:{left.start}:{right.end}"
        return (
            EvidenceFragment(
                fragment_id="f" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10],
                chunk_id=left.chunk_id,
                start=left.start,
                end=right.end,
                text=text,
            ),
        )
    # 跨片保持两个独立片段，不能把拼接字符串当成一个连续区间。
    return (_fragment(left), _fragment(right))


def _add(
    sink: dict[str, RelationCandidate],
    *,
    index: RelationIndex,
    spec: RelationSpec,
    source_id: str,
    target_id: str,
    keys: dict[str, frozenset[str]],
    left: TextUnit,
    right: TextUnit | None,
    kind: str,
) -> None:
    if source_id == target_id:
        return
    source = index.entities.get(source_id)
    target = index.entities.get(target_id)
    if source is None or target is None:
        return
    if not _assignable(keys[source_id], spec.source) or not _assignable(keys[target_id], spec.target):
        return
    text = _evidence_text(left, right)
    if not _role_gate(spec, text=text, source_name=source.name, target_name=target.name):
        return
    fragments = _fragments_for(index, left, right)
    relation_key = canonical_name(spec.relation_id)
    candidate_id = _candidate_id(source_id, relation_key, target_id, fragments, kind)
    if candidate_id in sink:
        return
    anchor = kind == "anchor" or source.anchor or target.anchor
    sink[candidate_id] = RelationCandidate(
        candidate_id=candidate_id,
        source_id=source_id,
        target_id=target_id,
        source_type=source.type_name,
        target_type=target.type_name,
        source_name=source.name,
        target_name=target.name,
        relation_id=spec.relation_id,
        relation_key=relation_key,
        comment=" ".join((spec.comment or "").split()),
        signature=spec.signature,
        scope_kind=kind,
        fragments=fragments,
        anchor=anchor and kind == "anchor",
    )


def recall_candidates(
    plan: ExtractionPlan,
    index: RelationIndex,
    *,
    route_kind: str,
) -> list[RelationCandidate]:
    """局部、邻接和明确条款引用三类候选。没有签名时返回空列表。"""
    specs = active_signatures(plan, route_kind)
    if not specs:
        return []
    keys = {
        entity_id: _type_keys(plan, entity.type_name)
        for entity_id, entity in index.entities.items()
    }
    anchors_by_scope: dict[str, list[str]] = {}
    for entity in index.entities.values():
        if entity.anchor:
            anchors_by_scope.setdefault(entity.scope, []).append(entity.entity_id)
    found: dict[str, RelationCandidate] = {}

    for unit in index.units:
        present = list(index.unit_entities.get(unit.unit_id, []))
        anchors = [entity_id for entity_id in anchors_by_scope.get(unit.scope, []) if entity_id not in present]
        for spec in specs:
            sources = [entity_id for entity_id in present if _assignable(keys[entity_id], spec.source)]
            targets = [entity_id for entity_id in present if _assignable(keys[entity_id], spec.target)]
            for source_id in sources:
                for target_id in targets:
                    _add(
                        found, index=index, spec=spec, source_id=source_id, target_id=target_id,
                        keys=keys, left=unit, right=None, kind="local",
                    )
            # 文档主体不一定出现在当前句里，只和本句已出现的另一端配对。
            for anchor_id in anchors:
                if _assignable(keys[anchor_id], spec.source):
                    for target_id in targets:
                        _add(
                            found, index=index, spec=spec, source_id=anchor_id, target_id=target_id,
                            keys=keys, left=unit, right=None, kind="anchor",
                        )
                if _assignable(keys[anchor_id], spec.target):
                    for source_id in sources:
                        _add(
                            found, index=index, spec=spec, source_id=source_id, target_id=anchor_id,
                            keys=keys, left=unit, right=None, kind="anchor",
                        )

    units = index.units
    for left, right in zip(units, units[1:]):
        if left.scope != right.scope:
            continue
        same_chunk = left.chunk_id == right.chunk_id
        # 跨片邻接只保留相邻切片，避免远距离共现；同片相邻句/行保持，以稳住召回。
        if not same_chunk and (left.chunk_id, right.chunk_id) not in index.neighbor_chunks:
            continue
        left_ids = set(index.unit_entities.get(left.unit_id, []))
        right_ids = set(index.unit_entities.get(right.unit_id, []))
        if not left_ids or not right_ids:
            continue
        for spec in specs:
            for source_id in left_ids | right_ids:
                if not _assignable(keys[source_id], spec.source):
                    continue
                for target_id in left_ids | right_ids:
                    if source_id == target_id or not _assignable(keys[target_id], spec.target):
                        continue
                    source_left = source_id in left_ids
                    source_right = source_id in right_ids
                    target_left = target_id in left_ids
                    target_right = target_id in right_ids
                    crosses = (source_left and target_right) or (source_right and target_left)
                    if not crosses:
                        continue
                    if source_left and source_right and target_left and target_right:
                        continue
                    _add(
                        found, index=index, spec=spec, source_id=source_id, target_id=target_id,
                        keys=keys, left=left, right=right, kind="adjacent",
                    )

    _recall_references(found, plan_specs=specs, index=index, keys=keys)
    rows = list(found.values())
    rows.sort(key=lambda item: (item.fragments[0].chunk_id, item.fragments[0].start, item.candidate_id))
    return rows


def _recall_references(
    found: dict[str, RelationCandidate],
    *,
    plan_specs: list[RelationSpec],
    index: RelationIndex,
    keys: dict[str, frozenset[str]],
) -> None:
    """明确条款号才跨出邻接窗口。孤立实体不多就两两遍历。"""
    for unit in index.units:
        numbers = clause_refs(unit.text)
        if not numbers:
            continue
        citing = list(index.unit_entities.get(unit.unit_id, []))
        if not citing:
            continue
        neighbor_ids = {
            other
            for left, right in index.neighbor_chunks
            if left == unit.chunk_id
            for other in (right,)
        }
        neighbor_ids.update(
            left for left, right in index.neighbor_chunks if right == unit.chunk_id
        )
        for number in numbers:
            for cited in cited_units(index, unit.scope, number, limit=3):
                if cited.chunk_id == unit.chunk_id or cited.chunk_id in neighbor_ids:
                    continue
                cited_ids = list(index.unit_entities.get(cited.unit_id, []))
                if not cited_ids:
                    continue
                for spec in plan_specs:
                    for source_id in citing + cited_ids:
                        if not _assignable(keys[source_id], spec.source):
                            continue
                        for target_id in citing + cited_ids:
                            if source_id == target_id or not _assignable(keys[target_id], spec.target):
                                continue
                            source_here = source_id in citing
                            target_here = target_id in citing
                            source_there = source_id in cited_ids
                            target_there = target_id in cited_ids
                            if not ((source_here and target_there) or (source_there and target_here)):
                                continue
                            _add(
                                found, index=index, spec=spec, source_id=source_id, target_id=target_id,
                                keys=keys, left=unit, right=cited, kind="cross_reference",
                            )


__all__ = [
    "EvidenceFragment",
    "RelationCandidate",
    "active_signatures",
    "recall_candidates",
]
