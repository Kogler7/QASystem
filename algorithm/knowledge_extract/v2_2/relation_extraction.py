"""小批关系分类：候选、原文和输入输出预算一起装进一次调用。

模型只判断给定候选是支持、当前证据不支持，还是证据不足。
程序核对候选集合是否被完整覆盖，缺项重试一次；负例不覆盖其他窗口里的正例。
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from algorithm.output_batching import generate_batched, output_batch_size

from .chunk_context import ChunkContextManager, ExtractionRequest
from .clef_gate import ClefGate
from ._logging import get_step_logger
from .ontology_compiler import ExtractionPlan
from .prompt_router import DocumentRoute, build_relation_task_messages
from .relation_candidates import RelationCandidate, active_signatures, recall_candidates
from .relation_context import build_relation_index


logger = get_step_logger(__name__)

# 32 是单次调用上限，不是一篇文档的关系总数。输出预算更紧时继续缩小。
RELATION_CANDIDATE_BATCH_SIZE = 32
# 约 4K token 的保守字符预算，为提示词、端点和 schema 留出余量。
_EVIDENCE_CHAR_BUDGET = 8000


def relation_decision_schema(
    candidates: list[RelationCandidate],
    fragment_ids: list[str],
) -> dict[str, Any]:
    """每个候选一次决策。证据是否属于本次片段由本地再核对。"""
    decision = {
        "type": "object",
        "properties": {
            "candidate_id": {
                "type": "string",
                "enum": [item.candidate_id for item in candidates],
            },
            "status": {
                "type": "string",
                "enum": ["supported", "not_supported", "insufficient_evidence"],
            },
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "fragment_id": {"type": "string", "enum": fragment_ids or [""]},
                        "quote": {"type": "string"},
                    },
                    "required": ["fragment_id", "quote"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["candidate_id", "status", "evidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"decisions": {"type": "array", "items": decision}},
        "required": ["decisions"],
        "additionalProperties": False,
    }


def _append_warning(
    warnings: list[dict[str, Any]] | None,
    code: str,
    message: str,
    path: str = "",
    value: Any = None,
) -> None:
    if warnings is None:
        return
    warning: dict[str, Any] = {"code": code, "message": message, "path": path}
    if value is not None:
        warning["value"] = value
    warnings.append(warning)


def _fragments_of(candidates: list[RelationCandidate]) -> list[Any]:
    seen: set[str] = set()
    rows = []
    for candidate in candidates:
        for fragment in candidate.fragments:
            if fragment.fragment_id in seen:
                continue
            seen.add(fragment.fragment_id)
            rows.append(fragment)
    return rows


def _evidence_too_wide(candidates: list[RelationCandidate]) -> bool:
    return sum(len(fragment.text) for fragment in _fragments_of(candidates)) > _EVIDENCE_CHAR_BUDGET


def _bind_quote(manager: ChunkContextManager, candidate: RelationCandidate, quote: str) -> dict[str, Any] | None:
    quote = (quote or "").strip()
    if not quote:
        return None
    for fragment in candidate.fragments:
        if quote not in fragment.text:
            continue
        local = fragment.text.find(quote)
        start = fragment.start + local
        end = start + len(quote)
        chunk = manager.get_chunk(fragment.chunk_id)
        if (chunk.text or "")[start:end] != quote:
            continue
        return {
            "quote": quote,
            "source_block_ids": [fragment.chunk_id],
            "start_char": start,
            "end_char": end,
        }
    return None


def _quote_covers_endpoints(candidate: RelationCandidate, quote: str) -> bool:
    """端点字面门禁：同片局部要两端都在引文里；锚点/跨片允许引文只点明一侧。"""
    quote = quote or ""
    source_name = (candidate.source_name or "").strip()
    target_name = (candidate.target_name or "").strip()
    source_hit = bool(source_name) and source_name in quote
    target_hit = bool(target_name) and target_name in quote
    if candidate.anchor or candidate.scope_kind == "anchor":
        return source_hit or target_hit
    if candidate.scope_kind in {"adjacent", "cross_reference"}:
        # 同片合并成一条连续邻接片段时，视同局部，仍要求引文覆盖两端。
        cross_chunk = len({item.chunk_id for item in candidate.fragments}) > 1
        if not cross_chunk and len(candidate.fragments) == 1:
            return source_hit and target_hit
        # 跨片邻接/条款引用常靠「该部门」指代，另一端只在配对片段中出现。
        fragment_text = "\n".join(item.text for item in candidate.fragments)
        if source_name and source_name not in fragment_text:
            return False
        if target_name and target_name not in fragment_text:
            return False
        return source_hit or target_hit
    return source_hit and target_hit


def _apply_decisions(
    parsed: dict[str, Any] | None,
    candidates: list[RelationCandidate],
    manager: ChunkContextManager,
    warnings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    by_id = {item.candidate_id: item for item in candidates}
    seen: set[str] = set()
    accepted: list[dict[str, Any]] = []
    if not isinstance(parsed, dict):
        return accepted, set(by_id)
    for index, decision in enumerate(parsed.get("decisions") or []):
        path = f"decisions[{index}]"
        if not isinstance(decision, dict):
            _append_warning(warnings, "INVALID_RELATION_DECISION", "关系决策必须是对象", path)
            continue
        candidate_id = str(decision.get("candidate_id") or "")
        candidate = by_id.get(candidate_id)
        if candidate is None:
            _append_warning(warnings, "UNKNOWN_RELATION_CANDIDATE", "关系决策引用未知候选", path, candidate_id)
            continue
        if candidate_id in seen:
            _append_warning(warnings, "DUPLICATE_RELATION_DECISION", "同一候选重复决策", path, candidate_id)
            continue
        status = str(decision.get("status") or "")
        if status not in {"supported", "not_supported", "insufficient_evidence"}:
            _append_warning(warnings, "INVALID_RELATION_STATUS", "关系决策状态无效", path, status)
            continue
        seen.add(candidate_id)
        if status != "supported":
            continue
        bound = None
        for item in decision.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            quote = str(item.get("quote") or "")
            bound = _bind_quote(manager, candidate, quote)
            if bound is None:
                continue
            if not _quote_covers_endpoints(candidate, bound["quote"]):
                _append_warning(
                    warnings,
                    "RELATION_EVIDENCE_ENDPOINT_MISS",
                    "supported 的引文未覆盖关系两端名称",
                    path,
                    candidate_id,
                )
                bound = None
                continue
            break
        if bound is None:
            _append_warning(
                warnings,
                "RELATION_EVIDENCE_INVALID",
                "supported 的引文不在本次证据片段中，或未覆盖端点名称",
                path,
                candidate_id,
            )
            continue
        accepted.append({
            "from_id": candidate.source_id,
            "to_id": candidate.target_id,
            "rel_type": candidate.relation_id,
            "evidence": bound,
            "candidate_id": candidate_id,
        })
    return accepted, set(by_id) - seen


def _decide_group(
    group: list[RelationCandidate],
    *,
    plan: ExtractionPlan,
    route: DocumentRoute,
    manager: ChunkContextManager,
    llm: Any,
    generate_structured: Callable[..., Any],
    use_scheduler: bool,
    max_attempts: int,
    retry_base_seconds: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    if _evidence_too_wide(group):
        _append_warning(
            warnings,
            "RELATION_INPUT_BUDGET",
            "单个证据窗口放不进输入预算，本窗口候选未送模型",
            group[0].fragments[0].chunk_id,
            len(group),
        )
        return [], attempts, warnings, metrics

    def build_request(subset: list[RelationCandidate]):
        fragments = _fragments_of(subset)
        schema = relation_decision_schema(subset, [item.fragment_id for item in fragments])
        messages = build_relation_task_messages(
            plan=plan,
            route=route,
            candidates=subset,
            fragments=fragments,
            output_schema=schema,
        )
        return messages, schema

    batch_size = output_batch_size(llm, tokens_per_item=96, ceiling=RELATION_CANDIDATE_BATCH_SIZE)
    label = group[0].fragments[0].chunk_id
    parsed = generate_batched(
        group,
        build_request,
        generate_structured,
        llm,
        batch_size=batch_size,
        use_scheduler=use_scheduler,
        attempts=attempts,
        pass_name="relation",
        chunk_id=label,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        call_metrics=metrics,
    )
    accepted, missing_ids = _apply_decisions(parsed, group, manager, warnings)
    if missing_ids:
        missing = [item for item in group if item.candidate_id in missing_ids]
        retried = generate_batched(
            missing,
            build_request,
            generate_structured,
            llm,
            batch_size=batch_size,
            use_scheduler=use_scheduler,
            attempts=attempts,
            pass_name="relation",
            chunk_id=f"{label}.missing",
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
            call_metrics=metrics,
        )
        recovered, still_missing = _apply_decisions(retried, missing, manager, warnings)
        accepted.extend(recovered)
        if still_missing:
            _append_warning(
                warnings,
                "RELATION_DECISION_MISSING",
                "关系候选在重试后仍没有决策，未当成无关系",
                label,
                sorted(still_missing),
            )
    return accepted, attempts, warnings, metrics


def _clef_partition(
    candidates: list[RelationCandidate],
    clef: ClefGate | bool | None,
    *,
    attempts: list[dict[str, Any]],
) -> tuple[list[tuple[RelationCandidate, str]], list[RelationCandidate]]:
    if not getattr(clef, "enabled", False) or not candidates:
        return [], list(candidates)
    grouped: OrderedDict[str, list[RelationCandidate]] = OrderedDict()
    for candidate in candidates:
        text = "\n".join(fragment.text for fragment in candidate.fragments)
        grouped.setdefault(text, []).append(candidate)
    grounded: list[tuple[RelationCandidate, str]] = []
    pending: list[RelationCandidate] = []
    for text, group in grouped.items():
        by_id = {item.candidate_id: item for item in group}
        accepted, still = clef.apply_relation_gate(
            text,
            [item.pair_row() for item in group],
            chunk_id=group[0].fragments[0].chunk_id,
            attempts=attempts,
        )
        grounded_ids: set[str] = set()
        for pair, quote in accepted:
            candidate = by_id.get(str(pair.get("pair_id") or ""))
            if candidate is None:
                continue
            if any(quote in fragment.text for fragment in candidate.fragments):
                grounded.append((candidate, quote))
                grounded_ids.add(candidate.candidate_id)
            else:
                pending.append(candidate)
                grounded_ids.add(candidate.candidate_id)
        for pair in still:
            candidate = by_id.get(str(pair.get("pair_id") or ""))
            if candidate is not None and candidate.candidate_id not in grounded_ids:
                pending.append(candidate)
    clef.note(
        relation_grounded_without_llm=len(grounded),
        relation_sent_to_llm=len(pending),
    )
    return grounded, pending


def run_relation_stage(
    plan: ExtractionPlan,
    manager: ChunkContextManager,
    request: ExtractionRequest,
    llm: Any,
    pool: Any,
    *,
    route: DocumentRoute,
    max_workers: int,
    use_scheduler: bool,
    attempts: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    local_previous: int,
    local_next: int,
    same_section: bool,
    max_attempts: int,
    retry_base_seconds: float,
    call_metrics: list[dict[str, Any]] | None,
    clef: ClefGate | bool | None,
    generate_structured: Callable[..., Any],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """在最终实体集合上召回并判断关系。返回值仍按 chunk 分组，供原流水线入库。"""
    chunks = list(request.chunks)
    index = build_relation_index(
        pool.accepted_entities(),
        manager,
        local_previous=local_previous,
        local_next=local_next,
        same_section=same_section,
    )
    if index.mixed_document_scope:
        _append_warning(
            warnings,
            "RELATION_DOCUMENT_SCOPE_UNSUPPORTED",
            "部分切片没有文档归属，关系阶段不把它们和已标明文档的切片合并",
            "relations",
        )
    specs = active_signatures(plan, route.kind)
    if not specs:
        _append_warning(
            warnings,
            "RELATION_NO_SCHEMA",
            "本体没有可判断的关系签名",
            "relations",
        )
        return [(chunk.chunk_id, []) for chunk in chunks]

    candidates = recall_candidates(plan, index, route_kind=route.kind)
    grounded, pending = _clef_partition(candidates, clef, attempts=attempts)
    logger.info(
        "[步骤][抽取] 关系阶段: 候选=%d 待模型=%d 预判落地=%d 未定位实体=%d",
        len(candidates),
        len(pending),
        len(grounded),
        len(index.coverage_gaps),
    )
    grouped: OrderedDict[tuple[Any, ...], list[RelationCandidate]] = OrderedDict()
    for candidate in pending:
        key = tuple((fragment.chunk_id, fragment.start, fragment.end) for fragment in candidate.fragments)
        grouped.setdefault(key, []).append(candidate)

    def run_group(group: list[RelationCandidate]):
        return _decide_group(
            group,
            plan=plan,
            route=route,
            manager=manager,
            llm=llm,
            generate_structured=generate_structured,
            use_scheduler=use_scheduler,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
        )

    groups = list(grouped.values())
    worker_count = min(max(1, max_workers), len(groups)) if groups else 1
    if worker_count <= 1:
        group_results = [run_group(group) for group in groups]
    else:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ke-relation") as executor:
            group_results = list(executor.map(run_group, groups))

    by_chunk: dict[str, list[dict[str, Any]]] = {chunk.chunk_id: [] for chunk in chunks}
    seen_facts: set[tuple[str, str, str, str]] = set()

    def _emit(raw: dict[str, Any]) -> None:
        evidence = raw.get("evidence") or {}
        fact = (
            raw["from_id"],
            raw["rel_type"],
            raw["to_id"],
            str(evidence.get("quote") or ""),
        )
        if fact in seen_facts:
            return
        seen_facts.add(fact)
        chunk_id = str((evidence.get("source_block_ids") or [""])[0])
        if chunk_id not in by_chunk:
            chunk_id = chunks[0].chunk_id if chunks else chunk_id
        by_chunk.setdefault(chunk_id, []).append(raw)

    for candidate, quote in grounded:
        bound = _bind_quote(manager, candidate, quote)
        if bound is None or not _quote_covers_endpoints(candidate, bound["quote"]):
            continue
        _emit({
            "from_id": candidate.source_id,
            "to_id": candidate.target_id,
            "rel_type": candidate.relation_id,
            "evidence": bound,
        })
    for relations, local_attempts, local_warnings, local_metrics in group_results:
        attempts.extend(local_attempts)
        warnings.extend(local_warnings)
        if call_metrics is not None:
            call_metrics.extend(local_metrics)
        for raw in relations:
            _emit(raw)
    return [(chunk.chunk_id, by_chunk.get(chunk.chunk_id, [])) for chunk in chunks]


__all__ = [
    "RELATION_CANDIDATE_BATCH_SIZE",
    "relation_decision_schema",
    "run_relation_stage",
]
