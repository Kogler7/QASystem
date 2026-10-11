"""Phase 1 Step 4：Entity / Relation 拆分（Two-Pass Candidate Proposal）。

依据 docs/method/Phase1_Knowledge_Extraction_Harness_Design_v3.md：
- §37 Step 4：从 one-shot ``entities + relations`` 变为
  ``entity candidate -> resolve -> relation candidate``；
- §14 Candidate Model：EntityCandidate / RelationCandidate / Evidence；
- §15 LLM Extraction：第一次 pass 只提实体候选（含 FeatureSpec 与 evidence），
  第二次 pass 基于 accepted/resolved 实体目录提关系候选，端点使用稳定
  candidate id（不再依赖自由字符串 name 对齐）；
- §19 关系匹配：签名、端点存在性、cardinality 作为结构 validator；
- §20 Entity Resolution：Identity 精确匹配 -> 规范化匹配（canonical_name），
  只有通过类型与证据约束后才允许 merge，不凭 LLM 主观判断合并；
- §25/§26/§27：O1 中间对象模型、O2 graph / triples，UID 只由稳定类型 +
  canonical identity 生成（普通属性不参与 UID）；
- AC-2 目标范围纪律、AC-7 无悬空关系。

LLM 通过标准 chat messages 调用，本模块不依赖 openai；live 环境可用
``llm_client.create_llm_client()`` 生成适配器。
"""
from __future__ import annotations

from algorithm.knowledge_extract.repair_scope import relation_repair_scope

import json
import re
import time
import uuid
from collections.abc import Callable, Iterable
from copy import deepcopy
from functools import lru_cache
from typing import Any

from algorithm.output_batching import generate_batched, output_batch_size
from .candidate_generation import (
    _normalize_span_entities,
    relation_catalog_for_context,
)
from .candidate_generation import (
    bind_entity_evidence as _bind_entity_evidence,
)
from .candidate_generation import (
    bind_relation_evidence as _bind_relation_evidence,
)
from .candidate_generation import (
    entity_pass as _entity_pass,
)
from .candidate_generation import (
    generate_structured as _generate_structured,
)
from .candidate_generation import (
    output_schema_for_types as _output_schema_for_types,
)
from .candidate_generation import (
    relation_pass as _relation_pass,
)
from .candidate_models import EntityCandidate, RelationCandidate
from .candidate_selector import CandidateSelector
from .chunk_context import ChunkContextManager, ExtractionChunk, ExtractionRequest
from .composite import CompositeElementHandler
from .coverage import CoverageAuditor
from .document_entities import enrich_document_entities
from .entity_resolver import CandidatePool
from .evidence import Evidence, evidence_coverage, locate_quote, verify_evidence
from .graph_assembler import GraphAssembler
from .graph_utils import canonical_name
from .hierarchy import (
    assemble_document_relations,
    assemble_document_unit_relations,
    assemble_numbered_hierarchy_relations,
    assemble_standard_semantic_relations,
    assemble_standard_technical_relations,
)
from .mapping_scorer import MappingScorer
from .ontology_compiler import ExtractionPlan, compile_plan
from .ontology_schema import OntologySchema
from .orchestrator import TargetedRepairOrchestrator
from .prompt_router import DocumentRoute, build_chat_messages, resolve_document_route
from .property_enrichment import enrich_entity_properties
from .run_state import ExtractionRun, ExtractionValidationError
from .standard_semantics import enrich_standard_semantics
from .structure_entities import add_structural_heading_entities
from .verification import ExtractionVerifier
from ._logging import get_step_logger, preview


logger = get_step_logger(__name__)


def _warning_codes(warnings: Iterable[dict[str, Any]]) -> dict[str, int]:
    """把告警按 code 归类，日志里一眼看出是哪一类问题。"""
    counter: dict[str, int] = {}
    for row in warnings or ():
        code = str((row or {}).get("code") or "UNKNOWN")
        counter[code] = counter.get(code, 0) + 1
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


@lru_cache(maxsize=32)
def _compile_runtime(
    ontology: str,
    target_types: tuple[str, ...] | None,
    include_subclasses: bool,
    mode: str,
    unknown_content_policy: str,
) -> tuple[ExtractionPlan, OntologySchema]:
    """Reuse immutable ontology artifacts across repeated harness runs."""
    plan = compile_plan(
        ontology,
        target_types=target_types,
        include_subclasses=include_subclasses,
        mode=mode,
        unknown_content_policy=unknown_content_policy,
    )
    return plan, OntologySchema.parse(ontology or "")


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


def _repair_context_chunks(
    issues: Iterable[dict[str, Any]],
    manager: ChunkContextManager,
    *,
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    max_chunks: int = 3,
) -> list[ExtractionChunk]:
    """Resolve verifier issues to evidence chunks without guessing document prefixes."""
    known_ids = set(manager.chunk_ids)
    selected: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"source_block_ids", "chunk_ids"} and isinstance(item, list):
                    selected.update(str(chunk_id) for chunk_id in item if str(chunk_id) in known_ids)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str):
            for chunk_id in known_ids:
                if value == chunk_id or value.startswith(f"{chunk_id}:"):
                    selected.add(chunk_id)

    issue_rows = list(issues)
    for row in issue_rows:
        path = str(row.get("path") or "")
        collect(path)
        collect(row.get("context"))
        match = re.match(r"^(entities|relations)\[(\d+)\]", path)
        if match is None:
            continue
        facts = entities if match.group(1) == "entities" else relations
        index = int(match.group(2))
        if index < len(facts):
            collect(facts[index])

    if not selected and len(manager.chunk_ids) <= 3:
        selected.update(manager.chunk_ids)
    return [
        manager.get_chunk(chunk_id)
        for chunk_id in manager.chunk_ids
        if chunk_id in selected
    ][: max(1, max_chunks)]


def _object_features(entity: EntityCandidate, feature_specs: tuple[Any, ...]) -> dict[str, Any]:
    intrinsic: dict[str, Any] = {}
    relation_features: list[str] = []
    for spec in feature_specs:
        if spec.kind == "intrinsic" and spec.identifier in entity.properties:
            intrinsic[spec.identifier] = entity.properties[spec.identifier]
        elif spec.kind == "relation":
            relation_features.append(spec.relation_signature or spec.identifier)
    return {"intrinsic": intrinsic, "relation": relation_features, "context": {}}


def _compat_properties(entity: EntityCandidate, plan: ExtractionPlan) -> dict[str, Any]:
    """Map stable property IDs to ontology display labels for legacy raw output."""
    spec = plan.class_specs.get(entity.canonical_type or "")
    if spec is None:
        return dict(entity.properties)
    labels = {
        canonical_name(prop.identifier): str(prop.display_name or prop.identifier)
        for prop in spec.properties
    }
    return {
        labels.get(canonical_name(key), key): value
        for key, value in entity.properties.items()
    }


def build_intermediate_model(
    pool: CandidatePool,
    plan: ExtractionPlan,
    *,
    run_id: str = "",
) -> dict[str, Any]:
    """§25：O1 中间对象模型（只含 accepted candidates）。"""
    accepted_entities = sorted(
        pool.accepted_entities(),
        key=lambda entity: (
            canonical_name(entity.canonical_type),
            canonical_name(entity.identity_value or entity.name),
            entity.candidate_id,
        ),
    )
    objects: list[dict[str, Any]] = []
    for entity in accepted_entities:
        features = _object_features(entity, plan.feature_specs.get(entity.canonical_type or "", ()))
        obj = entity.object_model(features)
        obj["compat_properties"] = _compat_properties(entity, plan)
        objects.append(obj)
    entity_keys = {
        entity.candidate_id: (
            canonical_name(entity.canonical_type),
            canonical_name(entity.identity_value or entity.name),
        )
        for entity in accepted_entities
    }
    accepted_entity_ids = set(entity_keys)
    accepted_relations = sorted(
        (
            relation
            for relation in pool.accepted_relations()
            if relation.from_candidate_id in accepted_entity_ids
            and relation.to_candidate_id in accepted_entity_ids
        ),
        key=lambda relation: (
            entity_keys.get(relation.from_candidate_id, ("", "")),
            canonical_name(relation.canonical_relation),
            entity_keys.get(relation.to_candidate_id, ("", "")),
            relation.candidate_id,
        ),
    )
    relations = [
        {
            "id": relation.candidate_id,
            "type": relation.canonical_relation,
            "from": relation.from_candidate_id,
            "to": relation.to_candidate_id,
            "evidence": [evidence.to_dict() for evidence in relation.evidence],
            "mapping": {"score": relation.mapping_score, "status": relation.mapping_status},
        }
        for relation in accepted_relations
    ]
    return {
        "run_id": run_id,
        "ontology": {
            "ontology_id": plan.ontology_id,
            "target_types": list(plan.target_scope.requested_types),
            "expanded_types": sorted(plan.allowed_types),
        },
        "objects": objects,
        "relations": relations,
        "coverage_gaps": [],
        "warnings": [],
    }


def _pool_fact_keys(pool: CandidatePool) -> set[str]:
    keys = {
        "entity:"
        + json.dumps(
            {
                "type": entity.canonical_type,
                "identity": entity.identity_value or entity.name,
                "properties": entity.properties,
                "evidence": sorted(item.span_id for item in entity.evidence),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        for entity in pool.accepted_entities()
    }
    keys.update(
        f"relation:{relation.from_candidate_id}:{relation.canonical_relation}:{relation.to_candidate_id}"
        for relation in pool.accepted_relations()
    )
    return keys


def _apply_repair_proposal(
    pool: CandidatePool,
    proposal: dict[str, Any] | None,
    manager: ChunkContextManager,
    warnings: list[dict[str, Any]],
) -> int:
    before = _pool_fact_keys(pool)
    payload = proposal if isinstance(proposal, dict) else {}
    entities = _bind_entity_evidence(payload.get("entities") or [], manager)
    for index, entity in enumerate(entities):
        bound_evidence = Evidence.from_dict(entity.get("evidence"))
        if (
            bound_evidence is None
            or not bound_evidence.span_id
            or not bound_evidence.source_block_ids
            or locate_quote(
                manager,
                bound_evidence.quote,
                chunk_ids=bound_evidence.source_block_ids,
            )
            is None
        ):
            _append_warning(
                warnings,
                "REPAIR_ENTITY_EVIDENCE_INVALID",
                "修复候选实体无法绑定到原始 chunk，已丢弃",
                f"repair.entities[{index}].evidence",
                (entity.get("evidence") or {}).get("quote"),
            )
            continue
        pool.add_entity(
            EntityCandidate(
                candidate_id="",
                proposed_type=str(entity.get("type") or ""),
                canonical_type=entity.get("type"),
                name=str(entity.get("name") or ""),
                properties=dict(entity.get("properties") or {}),
                evidence=[bound_evidence],
                source="repair",
            ),
            warnings=warnings,
        )

    for index, relation in enumerate(payload.get("relations") or []):
        if not isinstance(relation, dict):
            continue
        evidence = _bind_relation_evidence(relation, manager)
        if evidence is None:
            _append_warning(
                warnings,
                "REPAIR_RELATION_EVIDENCE_INVALID",
                "修复候选关系无法绑定到原始 chunk，已丢弃",
                f"repair.relations[{index}].evidence",
                (relation.get("evidence") or {}).get("quote"),
            )
            continue
        from_id = str(relation.get("from_id") or "")
        to_id = str(relation.get("to_id") or "")
        if not from_id:
            entity = pool.find_entity(
                str(relation.get("from_type") or ""),
                str(relation.get("from_name") or ""),
            )
            from_id = entity.candidate_id if entity is not None else ""
        if not to_id:
            entity = pool.find_entity(
                str(relation.get("to_type") or ""),
                str(relation.get("to_name") or ""),
            )
            to_id = entity.candidate_id if entity is not None else ""
        candidate = RelationCandidate(
            candidate_id="",
            from_candidate_id=from_id,
            to_candidate_id=to_id,
            proposed_relation=str(relation.get("rel_type") or ""),
            canonical_relation=None,
            evidence=[evidence],
        )
        resolved = pool.add_relation(candidate, warnings=warnings)
        if resolved is not None:
            resolved.evidence = [evidence]
    return len(_pool_fact_keys(pool) - before)


def _empty_result(
    plan: ExtractionPlan,
    *,
    route: DocumentRoute,
    warnings: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    run_id: str,
    run: ExtractionRun,
) -> dict[str, Any]:
    run.absorb_attempts(attempts)
    run.finish(accepted_entities=0, accepted_relations=0, stop_reason="empty_input")
    empty = {"nodes": [], "relationships": []}
    return {
        "raw": {"entities": [], "relations": []},
        "graph": empty,
        "triples": [],
        "intermediate_model": {
            "run_id": run_id,
            "ontology": {
                "ontology_id": plan.ontology_id,
                "target_types": list(plan.target_scope.requested_types),
                "expanded_types": sorted(plan.allowed_types),
            },
            "objects": [],
            "relations": [],
            "coverage_gaps": [],
            "warnings": [],
        },
        "warnings": warnings,
        "attempts": attempts,
        "evidence_coverage": {"entities": {"total": 0, "covered": 0, "rate": 1.0}},
        "coverage_gaps": [],
        "mapping": {
            "entities": [],
            "relations": [],
            "average_entity_score": None,
            "low_confidence": 0,
            "rejected": 0,
        },
        "profile": {
            "document_route": route.to_dict(),
            "pipeline": "two_pass",
            "chunks": 0,
            "repair_rounds": [],
            "repair_stop_reason": "empty_input",
        },
        "verification": {
            "valid": True,
            "hard_issue_count": 0,
            "repairable_issue_count": 0,
            "issues": [],
            "coverage_gaps": [],
        },
        "status": run.status.value,
        "run_trace": {
            **run.to_trace(),
            "calls": [],
            "repair_rounds": [],
            "verification": {"valid": True, "hard_issue_count": 0},
        },
        "error_code": run.error_code,
    }


def extract_split(
    ontology: str,
    chunks: Iterable[ExtractionChunk],
    *,
    target_types: Iterable[str] | None = None,
    include_subclasses: bool = True,
    validation_mode: str = "strict",
    llm: Any,
    model: str = "",
    unknown_content_policy: str = "ignore",
    local_previous: int = 1,
    local_next: int = 1,
    same_section: bool = True,
    max_workers: int = 1,
    use_scheduler: bool = False,
    run_id: str = "",
    mapping_weights: dict[str, float] | None = None,
    mapping_accepted_threshold: float = 0.85,
    mapping_low_confidence_threshold: float = 0.70,
    apply_mapping_threshold: bool = False,
    best_effort: bool = False,
    repairer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    entity_adjudicator: Callable[[dict[str, Any]], str | dict[str, Any]] | None = None,
    max_repair_rounds: int = 2,
    max_attempts: int = 3,
    retry_base_seconds: float = 0.05,
) -> dict[str, Any]:
    """§37 Step 4：entity candidate -> resolve -> relation candidate 两段式抽取。

    ``llm`` 是接收标准 ChatMessages 的 callable；返回契约兼容现有
    extractor（raw / graph / warnings / attempts / profile / error_code），
    并附带 triples / intermediate_model / evidence_coverage。
    """
    if isinstance(chunks, (str, bytes, dict)):
        raise TypeError("chunks 必须是 ExtractionChunk 序列，不能是完整文本或单个字典")
    resolved_chunks = list(chunks)
    if any(not isinstance(chunk, ExtractionChunk) for chunk in resolved_chunks):
        raise TypeError("extract_split 只接收 ExtractionChunk 序列")
    resolved_validation_mode = (validation_mode or "strict").casefold()
    if resolved_validation_mode not in {"strict", "permissive"}:
        raise ValueError("validation_mode 仅支持 strict 或 permissive")

    resolved_target_types = tuple(target_types) if target_types is not None else None
    plan, schema = _compile_runtime(
        ontology,
        resolved_target_types,
        include_subclasses,
        resolved_validation_mode,
        unknown_content_policy,
    )
    compile_errors = [warning for warning in plan.schema_warnings if warning.severity == "error"]
    if resolved_validation_mode == "strict" and compile_errors:
        raise ExtractionValidationError(
            "ontology strict validation failed",
            stage="ontology_compile",
            details={"warnings": [warning.to_dict() for warning in compile_errors]},
        )
    started = time.monotonic()
    logger.info(
        "[步骤][抽取] 输入: 本体=%d字符 chunks=%d target_types=%s model=%s 校验模式=%s include_subclasses=%s",
        len(ontology or ""),
        len(resolved_chunks),
        list(resolved_target_types) if resolved_target_types is not None else "全部",
        model or "env默认",
        resolved_validation_mode,
        include_subclasses,
    )
    logger.info(
        "[步骤][抽取] 本体编译输出: types=%s 关系=%d schema_warnings=%d",
        sorted(plan.allowed_types),
        len(getattr(plan, "relation_specs", {}) or {}),
        len(plan.schema_warnings),
    )
    request = ExtractionRequest(
        chunks=resolved_chunks,
        target_types=list(resolved_target_types) if resolved_target_types is not None else None,
    )
    extraction_chunks = list(request.chunks)
    manager = ChunkContextManager(extraction_chunks)
    route = resolve_document_route(plan, extraction_chunks)
    logger.info(
        "[步骤][抽取] 文档路由输出: kind=%s source=%s reasons=%s",
        route.kind,
        route.source,
        ",".join(route.reasons) or "-",
    )
    warnings: list[dict[str, Any]] = [warning.to_dict() for warning in plan.schema_warnings]
    attempts: list[dict[str, Any]] = []
    call_metrics: list[dict[str, Any]] = []
    resolved_run_id = run_id or f"ke-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    run = ExtractionRun.create(
        run_id=resolved_run_id,
        ontology=ontology,
        markdown="\n".join(chunk.text for chunk in extraction_chunks),
        model=model,
        prompt_version="knowledge-extract-v2-two-pass-v1",
        best_effort=best_effort,
    )
    run.start()
    for chunk in manager:
        metadata = chunk.metadata or {}
        run.register_block(
            chunk.chunk_id,
            block_id=str(metadata.get("block_id") or chunk.chunk_id),
            kind=str(metadata.get("block_kind") or "chunk"),
        )

    if not extraction_chunks:
        _append_warning(warnings, "EMPTY_INPUT", "请求没有 chunks", "chunks")
        return _empty_result(
            plan,
            route=route,
            warnings=warnings,
            attempts=attempts,
            run_id=resolved_run_id,
            run=run,
        )

    pool = CandidatePool(
        schema,
        allowed_types=plan.allowed_types,
        manager=manager,
        adjudicator=entity_adjudicator,
    )
    known_entities = pool.entity_catalog()

    # Pass 1：实体候选（LLM）
    entity_results = _entity_pass(
        plan,
        schema,
        manager,
        request,
        llm,
        route=route,
        max_workers=max_workers,
        use_scheduler=use_scheduler,
        attempts=attempts,
        warnings=warnings,
        local_previous=local_previous,
        local_next=local_next,
        same_section=same_section,
        known_entities=known_entities,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        call_metrics=call_metrics,
    )
    for _, entities in entity_results:
        for entity in entities:
            evidence = [item for item in [Evidence.from_dict(entity.get("evidence"))] if item is not None]
            pool.add_entity(
                EntityCandidate(
                    candidate_id="",
                    proposed_type=entity.get("type", ""),
                    canonical_type=entity.get("type"),
                    name=entity.get("name", ""),
                    properties=entity.get("properties") or {},
                    evidence=evidence,
                    source="",
                ),
                warnings=warnings,
            )

    logger.info(
        "[步骤][抽取] Pass1 实体候选输出: chunks=%d 候选=%d 逐片=%s 池内=%d",
        len(entity_results),
        sum(len(items) for _, items in entity_results),
        preview([len(items) for _, items in entity_results], 80),
        pool.entity_candidate_count,
    )

    # Deterministic supplement: the cover and normative-reference section
    # define the complete document-entity set before semantic relations.
    document_entities = enrich_document_entities(
        pool,
        manager,
        plan,
        warnings=warnings,
    )

    # Deterministic supplement: numbered Markdown headings are structural
    # evidence, and must be present before relation candidates are proposed.
    structural_headings = add_structural_heading_entities(
        pool,
        manager,
        allowed_types=plan.allowed_types,
        warnings=warnings,
    )
    standard_semantics = (
        enrich_standard_semantics(pool, manager, plan, warnings=warnings)
        if route.kind == "standard"
        else {}
    )
    logger.info(
        "[步骤][抽取] 确定性补充输出: 结构标题实体=%d 文档实体=%s 标准语义=%s 池内候选=%d",
        structural_headings,
        preview(document_entities, 120),
        preview(standard_semantics, 120),
        pool.entity_candidate_count,
    )
    selector = CandidateSelector(plan, manager, warnings)
    selector.pre_relation(pool.entity_candidates())
    logger.info(
        "[步骤][抽取] 候选选择 pre_relation 输出: accepted=%d rejected=%d warnings=%s",
        len(pool.accepted_entities()),
        sum(
            1
            for item in pool.entity_candidates()
            if getattr(item, "mapping_status", "accepted") != "accepted"
        ),
        preview(_warning_codes(warnings), 200),
    )

    # Closed-world property pass: entity identities/types are frozen here.
    # The model can only fill missing ontology-declared properties on the
    # accepted candidate-id enum and must cite the current source chunk.
    property_enrichment = enrich_entity_properties(
        pool,
        manager,
        plan,
        llm,
        route=route,
        attempts=attempts,
        warnings=warnings,
        call_metrics=call_metrics,
        use_scheduler=use_scheduler,
        local_previous=local_previous,
        local_next=local_next,
        same_section=same_section,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        max_workers=max_workers,
    )

    logger.info(
        "[步骤][抽取] 属性补全输出: %s",
        preview(property_enrichment, 240),
    )

    # Pass 2：关系候选（LLM，基于 accepted/resolved 实体目录）
    relation_results = _relation_pass(
        plan,
        manager,
        request,
        llm,
        pool,
        route=route,
        max_workers=max_workers,
        use_scheduler=use_scheduler,
        attempts=attempts,
        warnings=warnings,
        local_previous=local_previous,
        local_next=local_next,
        same_section=same_section,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        call_metrics=call_metrics,
    )
    for _, raw_relations in relation_results:
        for raw_relation in raw_relations:
            candidate = RelationCandidate(
                candidate_id="",
                from_candidate_id=str(raw_relation.get("from_id") or ""),
                to_candidate_id=str(raw_relation.get("to_id") or ""),
                proposed_relation=str(raw_relation.get("rel_type") or ""),
                canonical_relation=None,
                evidence=[],
            )
            resolved = pool.add_relation(candidate, warnings=warnings)
            if resolved is None:
                continue
            evidence = _bind_relation_evidence(raw_relation, manager)
            if evidence is not None:
                resolved.evidence = [evidence]

    logger.info(
        "[步骤][抽取] Pass2 关系候选输出: pairs=%d 池内关系=%d accepted=%d",
        sum(len(items) for _, items in relation_results),
        pool.relation_candidate_count,
        len(pool.accepted_relations()),
    )

    document_relations = assemble_document_relations(
        pool,
        manager,
        document_entities,
        warnings=warnings,
    )
    numbered_hierarchy_relations = assemble_numbered_hierarchy_relations(
        pool,
        manager,
        warnings=warnings,
    )
    # HAS_PART / EVIDENCED_BY are determined by the document-unit span, so they
    # are assembled here instead of being enumerated for the relation classifier.
    document_unit_relations = assemble_document_unit_relations(
        pool,
        manager,
        warnings=warnings,
    )
    standard_semantic_relations = (
        assemble_standard_semantic_relations(
            pool,
            manager,
            document_entities,
            warnings=warnings,
        )
        if route.kind == "standard"
        else {}
    )
    standard_technical_relations = (
        assemble_standard_technical_relations(pool, manager, warnings=warnings)
        if route.kind == "standard"
        else {}
    )

    # §21/AC-6：复合元素确定性组装与校验（补建 hasSubRule 成员关系）
    composite_handler = CompositeElementHandler.from_plan(plan)
    composite_issues = composite_handler.assemble(pool, plan, manager, warnings=warnings)
    selector.post_relation(pool.entity_candidates(), pool.relation_candidates())
    logger.info(
        "[步骤][抽取] 关系后处理输出: 复合组装问题=%d 文档关系=%s 层级关系=%d accepted_entities=%d accepted_relations=%d warnings=%s",
        len(composite_issues),
        preview(document_relations, 80),
        numbered_hierarchy_relations,
        len(pool.accepted_entities()),
        len(pool.accepted_relations()),
        preview(_warning_codes(warnings), 240),
    )

    repair_rounds: list[dict[str, Any]] = []
    repair_stop_reason = "validation_complete"
    verifier = ExtractionVerifier()

    def verify_pool(current_pool: CandidatePool):
        model = build_intermediate_model(current_pool, plan, run_id=resolved_run_id)
        current_raw = GraphAssembler().compat_raw(model["objects"], model["relations"])
        return verifier.verify(
            plan,
            schema,
            manager,
            request,
            current_raw["entities"],
            current_raw["relations"],
            rejected_warnings=current_pool.unresolved_relation_rejections(),
        )

    def builtin_repair(payload: dict[str, Any]) -> dict[str, Any]:
        issue_rows = payload["issues"]
        entity_repairs = {
            "required_property",
            "dangling_relation_endpoint",
            "uncovered_block",
        }
        needs_entity_pass = any(row.get("repair_type") in entity_repairs for row in issue_rows)
        referenced_chunks = _repair_context_chunks(
            issue_rows,
            manager,
            entities=payload.get("entities") or [],
            relations=payload.get("relations") or [],
        )
        if not referenced_chunks:
            _append_warning(
                warnings,
                "REPAIR_CONTEXT_UNRESOLVED",
                "verifier issue 无法定位到证据 chunk，已跳过本轮修复",
                f"repair[{payload['round']}]",
            )
            return {"entities": [], "relations": []}
        # repair 只需要证据定位用的原文，过长的上下文会撑爆 repair prompt。
        repair_text = "\n\n".join(chunk.text for chunk in referenced_chunks)[:15000]
        repair_chunk = ExtractionChunk(
            chunk_id=f"repair-{payload['round']}",
            text=repair_text,
            metadata={"block_id": f"repair-{payload['round']}", "block_kind": "repair"},
        )
        repair_catalog = relation_catalog_for_context(
            pool.entity_catalog(),
            referenced_chunks[0],
            referenced_chunks[1:],
        )

        if needs_entity_pass:
            output_schema = _output_schema_for_types(
                plan,
                "entity_candidates",
                plan.relevant_types(repair_text),
            )
            def build_repair_request(issue_batch):
                messages = build_chat_messages(
                    task="repair",
                    plan=plan,
                    route=route,
                    chunk=repair_chunk,
                    known_entities=repair_catalog,
                    output_schema=output_schema,
                    repair_issues=issue_batch,
                )
                return messages, output_schema
            parsed = generate_batched(
                issue_rows, build_repair_request, _generate_structured, llm,
                batch_size=output_batch_size(llm, tokens_per_item=2048, ceiling=4),
                use_scheduler=use_scheduler,
                attempts=attempts,
                pass_name="repair_entity",
                chunk_id=f"repair-{payload['round']}",
                max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds,
                call_metrics=call_metrics,
            )
            normalized_entities = _normalize_span_entities(
                parsed or {},
                chunk=repair_chunk,
                schema=schema,
                target_types=request.target_types,
                warnings=warnings,
            )
            return {"entities": normalized_entities, "relations": []}

        if not repair_catalog:
            _append_warning(
                warnings,
                "REPAIR_ENDPOINT_CONTEXT_EMPTY",
                "关系修复上下文中没有可引用实体，已跳过本轮修复",
                f"repair[{payload['round']}]",
            )
            return {"entities": [], "relations": []}
        output_schema = _output_schema_for_types(
            plan,
            "relation_candidates",
            {str(entry.get("type") or "") for entry in repair_catalog},
        )
        repair_scope = relation_repair_scope(plan, repair_catalog, issue_rows, output_schema)
        for skipped in repair_scope.skipped:
            _append_warning(
                warnings,
                "REPAIR_ISSUE_OUT_OF_SCOPE",
                "关系修复问题缺少当前目录中的合法端点/签名，保留未修复状态并跳过模型调用",
                f"repair[{payload['round']}]",
                skipped,
            )
        issue_rows = repair_scope.issues
        output_schema = repair_scope.schema
        if not issue_rows:
            return {"entities": [], "relations": []}

        def build_repair_request(issue_batch):
            messages = build_chat_messages(
                task="repair",
                plan=plan,
                route=route,
                chunk=repair_chunk,
                known_entities=repair_catalog,
                output_schema=output_schema,
                repair_issues=issue_batch,
            )
            return messages, output_schema
        parsed = generate_batched(
            issue_rows, build_repair_request, _generate_structured, llm,
            batch_size=output_batch_size(llm, tokens_per_item=2048, ceiling=4),
            use_scheduler=use_scheduler,
            attempts=attempts,
            pass_name="repair_relation",
            chunk_id=f"repair-{payload['round']}",
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
            call_metrics=call_metrics,
        )
        return parsed or {"entities": [], "relations": []}

    resolved_repairer = repairer or builtin_repair

    def request_repair(current_pool: CandidatePool, issues, round_number: int):
        model = build_intermediate_model(current_pool, plan, run_id=resolved_run_id)
        current_raw = GraphAssembler().compat_raw(model["objects"], model["relations"])
        return resolved_repairer(
            {
                "round": round_number,
                "issues": [issue.to_dict() for issue in issues],
                "entities": current_pool.entity_catalog(),
                "relations": current_raw["relations"],
                "chunks": [chunk.to_dict() for chunk in manager],
                "schemas": deepcopy(plan.output_schemas),
            }
        )

    repair_result = TargetedRepairOrchestrator(max_rounds=max_repair_rounds).run(
        pool,
        verify=verify_pool,
        repair=request_repair,
        apply=lambda current_pool, proposal: (
            current_pool,
            _apply_repair_proposal(current_pool, proposal, manager, warnings),
        ),
    )
    repair_rounds = [item.to_dict() for item in repair_result.rounds]
    repair_stop_reason = repair_result.stop_reason

    # Repair proposals pass through the same acceptance boundary.  This also
    # cascades any repaired relation whose endpoint was previously rejected.
    selector.pre_relation(pool.entity_candidates())
    selector.post_relation(pool.entity_candidates(), pool.relation_candidates())

    # §17-§19：Mapping Scorer（默认注释模式；apply_mapping_threshold 开启后按阈值过滤）
    scorer = MappingScorer(
        weights=mapping_weights,
        accepted_threshold=mapping_accepted_threshold,
        low_confidence_threshold=mapping_low_confidence_threshold,
    )
    all_entities = pool.accepted_entities()
    all_relations = pool.accepted_relations()
    entity_scores = [
        (entity, scorer.score_entity(entity, plan, all_relations)) for entity in all_entities
    ]
    relation_scores = [
        (relation, scorer.score_relation(relation, plan)) for relation in all_relations
    ]
    for entity, score in entity_scores:
        entity.mapping_score = score.score
        if apply_mapping_threshold:
            entity.mapping_status = score.status
            if score.status == "rejected":
                _append_warning(
                    warnings,
                    "MAPPING_REJECTED",
                    f"实体 {entity.name} 映射分数 {score.score} 低于阈值",
                    f"entities.{entity.candidate_id}",
                )
            elif score.status == "low_confidence":
                _append_warning(
                    warnings,
                    "MAPPING_LOW_CONFIDENCE",
                    f"实体 {entity.name} 映射分数 {score.score} 处于低置信区间",
                    f"entities.{entity.candidate_id}",
                )
    for relation, score in relation_scores:
        relation.mapping_score = score.score
        if apply_mapping_threshold:
            relation.mapping_status = score.status
            if score.status == "rejected":
                _append_warning(
                    warnings,
                    "MAPPING_REJECTED",
                    f"关系 {relation.canonical_relation} 映射分数 {score.score} 低于阈值",
                    f"relations.{relation.candidate_id}",
                )

    accepted_entities = pool.accepted_entities()
    accepted_entity_ids = {entity.candidate_id for entity in accepted_entities}
    accepted_relations = [
        relation
        for relation in pool.accepted_relations()
        if relation.from_candidate_id in accepted_entity_ids
        and relation.to_candidate_id in accepted_entity_ids
    ]
    mapping_summary = {
        "entities": [
            {
                "candidate_id": entity.candidate_id,
                "type": entity.canonical_type,
                "name": entity.name,
                **score.to_dict(),
            }
            for entity, score in entity_scores
        ],
        "relations": [
            {
                "candidate_id": relation.candidate_id,
                "type": relation.canonical_relation,
                **score.to_dict(),
            }
            for relation, score in relation_scores
        ],
        "average_entity_score": (
            round(
                sum(score.score for _, score in entity_scores) / len(entity_scores),
                4,
            )
            if entity_scores
            else None
        ),
        "low_confidence": sum(
            1 for _, score in [*entity_scores, *relation_scores] if score.status == "low_confidence"
        ),
        "rejected": sum(
            1 for _, score in [*entity_scores, *relation_scores] if score.status == "rejected"
        ),
        "weights": scorer.weights,
        "accepted_threshold": scorer.accepted_threshold,
        "low_confidence_threshold": scorer.low_confidence_threshold,
    }

    # O1 中间对象模型 -> O2 graph / triples + compat raw（§25-§28）
    intermediate_model = build_intermediate_model(pool, plan, run_id=resolved_run_id)
    assembler = GraphAssembler()
    raw = assembler.compat_raw(intermediate_model["objects"], intermediate_model["relations"])
    graph = assembler.build_graph(intermediate_model["objects"], intermediate_model["relations"])
    triples = assembler.build_triples(graph)
    # §23 / AC-8：通用 Coverage Auditor（只发现 gap）
    coverage_gaps = CoverageAuditor().audit(
        plan,
        manager,
        request,
        raw["entities"],
        raw["relations"],
    )
    intermediate_model["coverage_gaps"] = [gap.to_dict() for gap in coverage_gaps]

    # Evidence 验证（L1/L2）与覆盖（AC-4）
    verdicts = verify_evidence(raw, manager)
    for verdict in verdicts:
        if verdict.status == "fail":
            _append_warning(
                warnings,
                "EVIDENCE_L1_FAIL" if verdict.level == "L1" else "EVIDENCE_L2_FAIL",
                verdict.message,
                verdict.ref,
            )
    coverage = evidence_coverage(raw)

    verification_report = ExtractionVerifier().verify(
        plan,
        schema,
        manager,
        request,
        raw["entities"],
        raw["relations"],
        rejected_warnings=pool.unresolved_relation_rejections(),
    )
    logger.info(
        "[步骤][抽取] 校验输出: valid=%s hard_issues=%d repairable_issues=%d L1/L2覆盖=%s",
        verification_report.valid,
        len(verification_report.hard_issues),
        len(verification_report.repairable_issues),
        preview(coverage, 120),
    )
    if resolved_validation_mode == "strict" and verification_report.hard_issues:
        attempts.append(
            {
                "attempt": 1,
                "type": "schema_validation",
                "pass": "verification",
                "chunk": "final",
                "message": "; ".join(
                    f"{issue.code}: {issue.message}"
                    for issue in verification_report.hard_issues[:5]
                ),
                "terminal": True,
            }
        )

    terminal_failures = {
        (str(attempt.get("pass") or ""), str(attempt.get("chunk") or ""))
        for attempt in attempts
        if attempt.get("terminal") is True
    }
    for chunk in manager:
        run.mark_chunk_scan(
            chunk.chunk_id,
            "entity",
            completed=("entity", chunk.chunk_id) not in terminal_failures,
        )
        run.mark_chunk_scan(
            chunk.chunk_id,
            "relation",
            completed=("relation", chunk.chunk_id) not in terminal_failures,
        )

    run.absorb_attempts(attempts)
    run.finish(
        accepted_entities=len(accepted_entities),
        accepted_relations=len(accepted_relations),
        stop_reason=repair_stop_reason,
    )
    run_trace = run.to_trace()
    run_trace["calls"] = sorted(
        call_metrics,
        key=lambda call: (
            str(call.get("pass") or ""),
            str(call.get("chunk") or ""),
            int(call.get("attempt") or 0),
        ),
    )
    run_trace["repair_rounds"] = repair_rounds
    run_trace["verification"] = {
        "valid": verification_report.valid,
        "hard_issue_count": len(verification_report.hard_issues),
        "repairable_issue_count": len(verification_report.repairable_issues),
    }
    logger.info(
        "[步骤][抽取] 映射评分输出: accepted=%s low_confidence=%s rejected=%s",
        mapping_summary.get("accepted"),
        mapping_summary.get("low_confidence"),
        mapping_summary.get("rejected"),
    )
    logger.info(
        "[步骤][抽取] 输出: nodes=%d relationships=%d triples=%d accepted_entities=%d accepted_relations=%d warnings=%d attempts=%d repair=%s 耗时=%.2fs",
        len(graph.get("nodes", [])),
        len(graph.get("relationships", [])),
        len(triples),
        len(accepted_entities),
        len(accepted_relations),
        len(warnings),
        len(attempts),
        repair_stop_reason,
        time.monotonic() - started,
    )
    result = {
        "raw": raw,
        "graph": graph,
        "triples": triples,
        "intermediate_model": intermediate_model,
        "warnings": warnings,
        "attempts": attempts,
        "evidence_coverage": coverage,
        "coverage_gaps": [gap.to_dict() for gap in coverage_gaps],
        "verification": verification_report.to_dict(),
        "mapping": mapping_summary,
        "profile": {
            "document_route": route.to_dict(),
            "pipeline": "two_pass",
            "chunks": len(extraction_chunks),
            "entity_candidates": pool.entity_candidate_count,
            "relation_candidates": pool.relation_candidate_count,
            "accepted_entities": len(accepted_entities),
            "accepted_relations": len(accepted_relations),
            "numbered_hierarchy_relations": numbered_hierarchy_relations,
            "document_entities": document_entities,
            "standard_semantics": standard_semantics,
            "property_enrichment": property_enrichment,
            "document_relations": document_relations,
            "standard_semantic_relations": standard_semantic_relations,
            "standard_technical_relations": standard_technical_relations,
            "composite_auto_memberships": sum(
                1 for issue in composite_issues if issue.get("code") == "AUTO_MEMBERSHIP_ESTABLISHED"
            ),
            "mapping_mode": "enforce" if apply_mapping_threshold else "annotate",
            "candidate_selection": {
                "rejected_entities": sum(
                    1
                    for entity in pool.entity_candidates()
                    if entity.mapping_status == "rejected"
                ),
                "rejected_relations": sum(
                    1
                    for relation in pool.relation_candidates()
                    if relation.mapping_status == "rejected"
                ),
            },
            "repair_rounds": repair_rounds,
            "repair_stop_reason": repair_stop_reason,
            "llm_calls": len(call_metrics),
        },
        "status": run.status.value,
        "run_trace": run_trace,
        "error_code": run.error_code,
    }
    if run.error:
        result["_error"] = run.error
    return result


__all__ = [
    "CandidatePool",
    "EntityCandidate",
    "RelationCandidate",
    "build_intermediate_model",
    "extract_split",
]
