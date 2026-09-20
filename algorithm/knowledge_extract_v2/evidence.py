"""Phase 1 Step 3：Evidence 模型、绑定、验证（L1/L2）与覆盖统计。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md：
- §14.3 Evidence：span_id / source_block_ids / quote / start_char / end_char /
  evidence_type（explicit | structural | contextual）；
- §16 Evidence Verifier：L1 Source Existence、L2 Literal Match
  （L3 Property Support、L4 Contextual Support 留待 Step 8 之后的特性/评分阶段）；
- AC-4：需要证据的业务类 accepted instance evidence coverage。

本模块不依赖 openai，可独立加载与测试。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .chunk_context import ChunkContextManager, ExtractionChunk
from .graph_utils import coerce_non_empty_name


EVIDENCE_TYPES = ("explicit", "structural", "contextual")


@dataclass(frozen=True)
class Evidence:
    span_id: str
    source_block_ids: list[str]
    quote: str
    start_char: int | None = None
    end_char: int | None = None
    evidence_type: str = "explicit"

    def __post_init__(self) -> None:
        if self.evidence_type not in EVIDENCE_TYPES:
            raise ValueError(
                f"evidence_type 仅支持 {'/'.join(EVIDENCE_TYPES)}，收到 {self.evidence_type!r}"
            )
        object.__setattr__(self, "source_block_ids", list(self.source_block_ids))

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "source_block_ids": list(self.source_block_ids),
            "quote": self.quote,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "evidence_type": self.evidence_type,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "Evidence | None":
        payload = payload or {}
        if not coerce_non_empty_name(payload.get("quote")):
            return None
        return cls(
            span_id=coerce_non_empty_name(payload.get("span_id")) or "",
            source_block_ids=payload.get("source_block_ids") or [],
            quote=payload["quote"] if isinstance(payload.get("quote"), str) else coerce_non_empty_name(payload.get("quote")),
            start_char=payload.get("start_char"),
            end_char=payload.get("end_char"),
            evidence_type=payload.get("evidence_type") or "explicit",
        )


@dataclass(frozen=True)
class EvidenceVerdict:
    """§16 L1/L2 验证结果。"""

    ref: str
    span_id: str
    level: str
    status: str  # ok | fail
    message: str


def _as_manager(chunks: ChunkContextManager | Iterable[ExtractionChunk]) -> ChunkContextManager:
    return chunks if isinstance(chunks, ChunkContextManager) else ChunkContextManager(chunks)


def locate_quote(
    chunks: ChunkContextManager | Iterable[ExtractionChunk],
    quote: str,
    *,
    chunk_ids: Iterable[str] | None = None,
) -> Evidence | None:
    """在 chunks 中定位 quote，返回带 span 的 Evidence；找不到返回 None。"""
    manager = _as_manager(chunks)
    ids = list(chunk_ids) if chunk_ids is not None else manager.chunk_ids
    for chunk_id in ids:
        span = manager.find_span(chunk_id, quote)
        if span is None:
            continue
        start, end = span
        return Evidence(
            span_id=f"{chunk_id}:{start}:{end}",
            source_block_ids=[chunk_id],
            quote=manager.evidence_text(chunk_id, start, end),
            start_char=start,
            end_char=end,
            evidence_type="explicit",
        )
    return None


def _bind_item_evidence(
    item: dict[str, Any],
    manager: ChunkContextManager,
    *,
    fallback_fields: tuple[str, ...],
    chunk_ids: Iterable[str] | None = None,
) -> None:
    existing = item.get("evidence")
    if isinstance(existing, Evidence):
        existing = existing.to_dict()
    if isinstance(existing, dict) and coerce_non_empty_name(existing.get("quote")):
        located = locate_quote(
            manager,
            existing["quote"],
            chunk_ids=(
                chunk_ids
                if chunk_ids is not None
                else existing.get("source_block_ids") or None
            ),
        )
        if located is not None:
            item["evidence"] = {**existing, **located.to_dict()}
            return
        # 模型偶尔会把 Markdown/LaTeX 中的空格或定界符归一化。
        # 原 quote 无法定位时，仍可以尝试用实体的逐字 name 作为更小的
        # 可验证 span；若 fallback 也失败，保留原 evidence 交由 verifier 报告。

    for field_name in fallback_fields:
        value = item.get(field_name)
        if not coerce_non_empty_name(value):
            continue
        located = locate_quote(manager, str(value), chunk_ids=chunk_ids)
        if located is not None:
            item["evidence"] = located.to_dict()
            return


def bind_evidence(
    raw: dict[str, Any],
    chunks: ChunkContextManager | Iterable[ExtractionChunk],
    *,
    fallback_fields: tuple[str, ...] = ("name",),
    chunk_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """把抽取结果绑定 chunk_id 与 evidence span（§37 Step 3）。

    - 已有 evidence.quote：在 chunk 中定位并回填 span_id / source_block_ids /
      start_char / end_char；定位不到时尝试逐字 fallback field，
      fallback 仍失败则保留原样，由 verify_evidence 报告；
    - 没有 evidence：按 fallback_fields（实体 name / 关系端点 name）定位原文；
    - 不修改输入之外的语义，不伪造证据。
    """
    manager = _as_manager(chunks)
    resolved_chunk_ids = tuple(chunk_ids) if chunk_ids is not None else None
    raw = dict(raw or {})
    for entity in raw.get("entities", []):
        if isinstance(entity, dict):
            _bind_item_evidence(
                entity,
                manager,
                fallback_fields=fallback_fields,
                chunk_ids=resolved_chunk_ids,
            )
    for relation in raw.get("relations", []):
        if isinstance(relation, dict):
            _bind_item_evidence(
                relation,
                manager,
                fallback_fields=("from_name", "to_name"),
                chunk_ids=resolved_chunk_ids,
            )
    return raw


def verify_evidence(
    raw: dict[str, Any],
    chunks: ChunkContextManager | Iterable[ExtractionChunk],
) -> list[EvidenceVerdict]:
    """§16 L1/L2 验证：source、span、offset 与 quote 必须相互一致。

    ``span_id`` 使用 ``<chunk_id>:<start>:<end>``。chunk id 本身允许包含
    冒号，因此从右侧拆分。多 ``source_block_ids`` 不再只检查第一项；span
    指向的 block 只要存在于声明集合中且精确切片匹配即可通过。
    """
    manager = _as_manager(chunks)
    verdicts: list[EvidenceVerdict] = []

    items: list[tuple[str, int, dict[str, Any]]] = []
    for index, entity in enumerate(raw.get("entities", []) if isinstance(raw, dict) else []):
        if isinstance(entity, dict):
            items.append(("entities", index, entity))
    for index, relation in enumerate(raw.get("relations", []) if isinstance(raw, dict) else []):
        if isinstance(relation, dict):
            items.append(("relations", index, relation))

    expanded_items: list[tuple[str, dict[str, Any], Any]] = []
    for section, index, item in items:
        evidences = item.get("evidences")
        if isinstance(evidences, list) and evidences:
            expanded_items.extend(
                (f"{section}[{index}].evidences[{evidence_index}]", item, evidence)
                for evidence_index, evidence in enumerate(evidences)
            )
        else:
            expanded_items.append((f"{section}[{index}]", item, item.get("evidence")))

    for ref, item, evidence in expanded_items:
        if isinstance(evidence, Evidence):
            payload = evidence.to_dict()
        elif isinstance(evidence, dict):
            payload = evidence
        else:
            verdicts.append(
                EvidenceVerdict(ref, "", "L1", "fail", "缺少 evidence（未绑定 chunk_id 与 span）")
            )
            continue

        span_id = coerce_non_empty_name(payload.get("span_id"))
        quote = payload.get("quote")
        if not isinstance(quote, str):
            quote = coerce_non_empty_name(quote)
        block_ids = [coerce_non_empty_name(bid) for bid in (payload.get("source_block_ids") or [])]

        if not quote.strip():
            verdicts.append(EvidenceVerdict(ref, span_id, "L1", "fail", "evidence 缺少 quote"))
            continue
        if not block_ids:
            verdicts.append(EvidenceVerdict(ref, span_id, "L1", "fail", "evidence 缺少 source_block_ids"))
            continue
        existing_block_ids = [block_id for block_id in block_ids if manager.has_chunk(block_id)]
        if not existing_block_ids:
            verdicts.append(
                EvidenceVerdict(
                    ref,
                    span_id,
                    "L1",
                    "fail",
                    f"source_block 均不存在: {block_ids!r}",
                )
            )
            continue
        if not span_id:
            verdicts.append(EvidenceVerdict(ref, span_id, "L1", "fail", "evidence 缺少 span_id"))
            continue
        try:
            span_block_id, raw_start, raw_end = span_id.rsplit(":", 2)
            span_start = int(raw_start)
            span_end = int(raw_end)
        except (TypeError, ValueError):
            verdicts.append(EvidenceVerdict(ref, span_id, "L1", "fail", "span_id 格式无效"))
            continue
        start_char = payload.get("start_char")
        end_char = payload.get("end_char")
        if not isinstance(start_char, int) or not isinstance(end_char, int):
            verdicts.append(EvidenceVerdict(ref, span_id, "L1", "fail", "evidence 缺少整数 offset"))
            continue
        if (span_start, span_end) != (start_char, end_char):
            verdicts.append(EvidenceVerdict(ref, span_id, "L2", "fail", "span_id 与 offset 不一致"))
            continue
        if span_block_id not in block_ids or not manager.has_chunk(span_block_id):
            verdicts.append(
                EvidenceVerdict(ref, span_id, "L1", "fail", "span_id 指向的 block 未在 source_block_ids 中")
            )
            continue
        chunk = manager.get_chunk(span_block_id)
        if not (0 <= start_char <= end_char <= len(chunk.text)):
            verdicts.append(EvidenceVerdict(ref, span_id, "L2", "fail", "evidence offset 越界"))
            continue
        if chunk.text[start_char:end_char] == quote:
            verdicts.append(
                EvidenceVerdict(ref, span_id, "L2", "ok", "quote 与 source_block 精确 span 一致")
            )
        else:
            verdicts.append(
                EvidenceVerdict(
                    ref,
                    span_id,
                    "L2",
                    "fail",
                    "quote 与声明 span 不一致（L2 Literal Match 失败）",
                )
            )
    return verdicts


def evidence_coverage(raw: dict[str, Any]) -> dict[str, Any]:
    """AC-4 代理：统计实体/关系已绑定 evidence（含 quote）的覆盖率。"""
    entities = [item for item in (raw.get("entities") or []) if isinstance(item, dict)]
    relations = [item for item in (raw.get("relations") or []) if isinstance(item, dict)]

    def covered(items: list[dict[str, Any]]) -> tuple[int, int]:
        total = len(items)
        hits = sum(
            1
            for item in items
            if any(
                coerce_non_empty_name(evidence.get("quote"))
                for evidence in (
                    item.get("evidences")
                    if isinstance(item.get("evidences"), list) and item.get("evidences")
                    else [item.get("evidence") or {}]
                )
                if isinstance(evidence, dict)
            )
        )
        return total, hits

    entity_total, entity_hits = covered(entities)
    relation_total, relation_hits = covered(relations)
    by_type: dict[str, dict[str, Any]] = {}
    for entity_type in sorted({coerce_non_empty_name(item.get("type")) for item in entities}):
        typed = [item for item in entities if coerce_non_empty_name(item.get("type")) == entity_type]
        total, hits = covered(typed)
        by_type[entity_type] = {
            "total": total,
            "covered": hits,
            "rate": hits / total if total else 1.0,
        }

    def rate(hits: int, total: int) -> float:
        return hits / total if total else 1.0

    overall_total = entity_total + relation_total
    overall_hits = entity_hits + relation_hits
    return {
        "entities": {"total": entity_total, "covered": entity_hits, "rate": rate(entity_hits, entity_total)},
        "relations": {"total": relation_total, "covered": relation_hits, "rate": rate(relation_hits, relation_total)},
        "by_entity_type": by_type,
        "overall": {"total": overall_total, "covered": overall_hits, "rate": rate(overall_hits, overall_total)},
    }
