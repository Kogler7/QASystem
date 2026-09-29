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

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterable
from copy import deepcopy
from functools import lru_cache
from typing import Any

from .candidate_generation import (
    relation_catalog_for_context,
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
from .candidate_generation import (
    verify_relation_claims as _verify_relation_claims,
)
from .candidate_models import EntityCandidate, RelationCandidate
from .candidate_selector import CandidateSelector
from .chunk_context import (
    ChunkContextManager,
    ExtractionChunk,
    ExtractionRequest,
    validate_chunk_contract,
)
from .chunk_metadata import enrich_chunk_metadata
from .composite import CompositeElementHandler
from .coverage import CoverageAuditor
from .table_semantics import analyze_table_semantics
from .official_structure import (
    add_official_form_agents,
    add_official_structure_entities,
    build_official_form_agents,
    build_official_structure_entities,
)
from .official_document_scope import annotate_official_roles, entity_in_official_scope, relation_in_official_scope, split_embedded_attachments
from .official_attachments import add_official_attachment_entities
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
from .mapping_scorer import CONFIDENCE_METHOD, MappingScorer, mapping_confidence_payload
from .ontology_compiler import ONTOLOGY_COMPILER_VERSION, ExtractionPlan, compile_plan
from .ontology_schema import OntologySchema
from .orchestrator import TargetedRepairOrchestrator
from .prompt_router import (
    DocumentRoute,
    PROMPT_VERSIONS,
    build_chat_messages,
    disabled_document_route,
    resolve_chunk_routes,
    resolve_document_route,
)
from .property_enrichment import enrich_entity_identities, enrich_entity_properties
from .run_state import ExtractionRun, ExtractionValidationError
from .standard_semantics import enrich_standard_semantics
from .standard_alignment import align_standard_graph
from .standard_skeleton import add_standard_skeleton, build_standard_skeleton
from .standard_requirements import enrich_standard_requirements
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
def _compile_runtime_cached(
    ontology: str,
    target_types: tuple[str, ...] | None,
    include_subclasses: bool,
    mode: str,
    unknown_content_policy: str,
) -> tuple[ExtractionPlan, OntologySchema]:
    """Compile once per stable input; callers receive isolated copies below."""
    plan = compile_plan(
        ontology,
        target_types=target_types,
        include_subclasses=include_subclasses,
        mode=mode,
        unknown_content_policy=unknown_content_policy,
    )
    return plan, OntologySchema.parse(ontology or "")


def _compile_runtime(
    ontology: str,
    target_types: tuple[str, ...] | None,
    include_subclasses: bool,
    mode: str,
    unknown_content_policy: str,
) -> tuple[ExtractionPlan, OntologySchema]:
    """Return request-owned artifacts so cached mutable mappings cannot leak."""
    return deepcopy(
        _compile_runtime_cached(
            ontology,
            target_types,
            include_subclasses,
            mode,
            unknown_content_policy,
        )
    )


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


def _empty_property_value(prop: Any) -> Any:
    """本体声明但原文未提供的属性占位值：多值属性用 []，单值属性用 ""。"""
    data_type = re.sub(r"\s+", "", str(getattr(prop, "data_type", "") or "")).casefold()
    if (
        bool(getattr(prop, "multi_value", False))
        or data_type.endswith("[]")
        or "list<" in data_type
        or "array<" in data_type
    ):
        return []
    return ""


def _complete_ontology_properties(
    entities: Iterable[dict[str, Any]], plan: ExtractionPlan
) -> list[dict[str, Any]]:
    """输出契约：实体 properties 覆盖本体声明的全部字段，原文缺失的用空值占位。"""
    completed: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            completed.append(entity)
            continue
        item = dict(entity)
        spec = plan.class_specs.get(str(entity.get("type") or ""))
        properties = dict(entity.get("properties") or {})
        if spec is not None:
            labels = {
                canonical_name(prop.identifier): str(prop.display_name or prop.identifier)
                for prop in spec.properties
            }
            filled: dict[str, Any] = {
                str(prop.display_name or prop.identifier): _empty_property_value(prop)
                for prop in spec.properties
            }
            for key, value in properties.items():
                filled[labels.get(canonical_name(key), key)] = value
            properties = filled
        item["properties"] = properties
        completed.append(item)
    return completed


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
            "mapping": {
                "score": relation.mapping_score,
                "status": relation.mapping_status,
                "admission_status": relation.mapping_status,
                "score_status": relation.mapping_score_status or relation.mapping_status,
            },
            **mapping_confidence_payload(
                relation.mapping_score,
                relation.mapping_score_status or relation.mapping_status,
                evidences=relation.evidence,
                canonical_mapping_valid=bool(relation.canonical_relation),
                warnings=relation.warnings,
                aggregate_evidence=False,
                aggregate_canonical_mapping=False,
            ),
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


def _apply_global_confidence_gate(
    collections: Iterable[list[dict[str, Any]]],
    *,
    gate: str,
) -> None:
    """Conservatively prevent fact promotion after a global hard failure."""
    for items in collections:
        for item in items:
            if item.get("confidence") is None:
                continue
            details = dict(item.get("confidence_details") or {})
            gates = list(details.get("gates") or [])
            if gate not in gates:
                gates.append(gate)
            confidence = min(float(item["confidence"]), 0.8499)
            current_status = str(item.get("confidence_status") or details.get("status") or "")
            status = "rejected" if current_status == "rejected" or confidence < 0.70 else "needs_review"
            details["gates"] = gates
            details["status"] = status
            item["confidence"] = round(confidence, 4)
            item["confidence_status"] = status
            item["gate_status"] = (
                "blocked" if item.get("gate_status") == "blocked" else "review"
            )
            item["confidence_details"] = details


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


def _frozen_entity_fingerprint(pool: CandidatePool, plan: ExtractionPlan) -> str:
    """Hash the entity universe and every identity-bearing field."""
    rows: list[dict[str, Any]] = []
    for entity in pool.entity_candidates():
        type_id = str(entity.canonical_type or entity.proposed_type or "")
        spec = plan.class_specs.get(type_id)
        identity_properties = tuple(spec.identity_properties) if spec is not None else ()
        rows.append({
            "entity_id": str(entity.candidate_id),
            "type": type_id,
            "name": str(entity.name),
            "identity_property": str(entity.identity_property or ""),
            "identity_value": entity.identity_value,
            "identity_properties": {
                property_id: entity.properties.get(property_id)
                for property_id in identity_properties
            },
        })
    rows.sort(key=lambda row: row["entity_id"])
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _assert_frozen_entity_fingerprint(
    pool: CandidatePool,
    plan: ExtractionPlan,
    expected: str,
    *,
    stage: str,
) -> None:
    actual = _frozen_entity_fingerprint(pool, plan)
    if actual == expected:
        return
    raise ExtractionValidationError(
        "实体集合、类型或身份在冻结边界后发生变化",
        stage=stage,
        error_code="FROZEN_ENTITY_UNIVERSE_MUTATED",
        details={"expected_fingerprint": expected, "actual_fingerprint": actual},
    )


def _apply_repair_proposal(
    pool: CandidatePool,
    proposal: dict[str, Any] | None,
    manager: ChunkContextManager,
    warnings: list[dict[str, Any]],
    *,
    allowed_chunk_ids: Iterable[str] | None = None,
) -> int:
    before = _pool_fact_keys(pool)
    payload = proposal if isinstance(proposal, dict) else {}
    for index, entity in enumerate(payload.get("entities") or []):
        _append_warning(
            warnings,
            "REPAIR_ENTITY_REJECTED_FROZEN",
            "实体宇宙已冻结，repair 不得新增实体或改变实体身份",
            f"repair.entities[{index}]",
            entity,
        )

    for index, relation in enumerate(payload.get("relations") or []):
        if not isinstance(relation, dict):
            continue
        evidence = _bind_relation_evidence(
            relation,
            manager,
            chunk_ids=allowed_chunk_ids,
        )
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
        if from_id:
            from_id = pool.resolve_candidate_id(from_id)
        if to_id:
            to_id = pool.resolve_candidate_id(to_id)
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
        pool.add_relation(candidate, warnings=warnings)
    return len(_pool_fact_keys(pool) - before)


_TERMINAL_SCHEMA_CODES = frozenset({"ONTOLOGY_EMPTY", "TARGET_SCOPE_EMPTY"})


def _normalize_chunks(
    chunks: Iterable[Any],
    warnings: list[dict[str, Any]],
) -> list[ExtractionChunk]:
    """宽容接收上游 chunks：ExtractionChunk 直通，字段 dict 按契约转换。"""
    resolved: list[ExtractionChunk] = []
    for index, item in enumerate(chunks):
        if isinstance(item, ExtractionChunk):
            # V2 may enrich metadata and disambiguate ids.  Work on a deep copy
            # so retry/routing cannot mutate the upstream splitter's objects.
            resolved.append(deepcopy(item))
            continue
        if isinstance(item, dict):
            payload = dict(item)
            if not str(payload.get("chunk_id") or "").strip():
                generated = f"chunk-{index:04d}"
                payload["chunk_id"] = generated
                _append_warning(
                    warnings,
                    "CHUNK_ID_GENERATED",
                    f"chunks[{index}] 缺少 chunk_id，已生成 {generated!r}",
                    f"chunks[{index}].chunk_id",
                )
            if payload.get("text") is None:
                raise TypeError(f"chunks[{index}] 缺少 text 字段")
            payload["text"] = str(payload.get("text"))
            try:
                resolved.append(ExtractionChunk.from_dict(payload))
            except TypeError as exc:
                raise TypeError(f"chunks[{index}] 无法转换为 ExtractionChunk: {exc}") from exc
            continue
        raise TypeError(f"chunks[{index}] 必须是 ExtractionChunk 或字段 dict")
    return resolved


def _empty_result(
    plan: ExtractionPlan,
    *,
    route: DocumentRoute,
    warnings: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    run_id: str,
    run: ExtractionRun,
    chunk_count: int = 0,
    failure_code: str = "",
    failure_message: str = "",
    stop_reason: str = "empty_input",
    profile_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run.absorb_attempts(attempts)
    run.finish(
        accepted_entities=0,
        accepted_relations=0,
        stop_reason=stop_reason,
        failure_code=failure_code,
        failure_message=failure_message,
    )
    empty = {"nodes": [], "relationships": []}
    verification_valid = not bool(failure_code)
    verification_issues = (
        []
        if verification_valid
        else [
            {
                "code": failure_code,
                "message": failure_message or stop_reason,
                "path": "ontology",
                "severity": "error",
                "repair_type": None,
                "context": {},
            }
        ]
    )
    result = {
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
            "chunks": chunk_count,
            "repair_rounds": [],
            "repair_stop_reason": stop_reason,
            **(profile_extra or {}),
        },
        "verification": {
            "valid": verification_valid,
            "coverage_complete": True,
            "hard_issue_count": 0 if verification_valid else 1,
            "repairable_issue_count": 0,
            "unresolved_repairable_count": 0,
            "issues": verification_issues,
            "coverage_gaps": [],
        },
        "status": run.status.value,
        "run_trace": {
            **run.to_trace(),
            "calls": [],
            "repair_rounds": [],
            "verification": {
                "valid": verification_valid,
                "coverage_complete": True,
                "hard_issue_count": 0 if verification_valid else 1,
                "unresolved_repairable_count": 0,
            },
        },
        "error_code": run.error_code,
    }
    if run.error:
        result["_error"] = run.error
    return result


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
    use_router: bool = True,
    repairer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    entity_adjudicator: Callable[[dict[str, Any]], str | dict[str, Any]] | None = None,
    identity_admission: str = "quarantine",
    max_repair_rounds: int = 0,
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
    resolved_validation_mode = (validation_mode or "strict").casefold()
    if resolved_validation_mode not in {"strict", "permissive"}:
        raise ValueError("validation_mode 仅支持 strict 或 permissive")
    if identity_admission not in {"observe", "quarantine"}:
        raise ValueError("identity_admission 仅支持 observe 或 quarantine")

    chunk_warnings: list[dict[str, Any]] = []
    resolved_chunks = _normalize_chunks(chunks, chunk_warnings)
    metadata_counts = enrich_chunk_metadata(resolved_chunks)
    validate_chunk_contract(resolved_chunks, warnings=chunk_warnings)

    resolved_target_types = tuple(target_types) if target_types is not None else None
    plan, schema = _compile_runtime(
        ontology,
        resolved_target_types,
        include_subclasses,
        resolved_validation_mode,
        unknown_content_policy,
    )
    compile_errors = [warning for warning in plan.schema_warnings if warning.severity == "error"]
    terminal_warning = next(
        (warning for warning in plan.schema_warnings if warning.code in _TERMINAL_SCHEMA_CODES),
        None,
    )
    if resolved_validation_mode == "strict" and compile_errors:
        raise ExtractionValidationError(
            "ontology strict validation failed",
            stage="ontology_compile",
            details={"warnings": [warning.to_dict() for warning in compile_errors]},
            error_code=terminal_warning.code if terminal_warning is not None else "",
        )
    started = time.monotonic()
    if use_router:
        resolved_chunks = split_embedded_attachments(
            plan, resolved_chunks, resolve_chunk_routes(plan, resolved_chunks)
        )
    chunk_source_ids = {chunk.chunk_id: chunk.metadata["source_chunk_id"] for chunk in resolved_chunks if chunk.metadata.get("source_chunk_id")}
    scannable = [chunk for chunk in resolved_chunks if (chunk.text or "").strip()]
    skipped_ids = [chunk.chunk_id for chunk in resolved_chunks if not (chunk.text or "").strip()]
    logger.info(
        "[步骤][抽取] 输入: 本体=%d字符 chunks=%d 可扫描=%d target_types=%s model=%s 校验模式=%s include_subclasses=%s",
        len(ontology or ""),
        len(resolved_chunks),
        len(scannable),
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
        chunks=scannable,
        target_types=list(resolved_target_types) if resolved_target_types is not None else None,
    )
    manager = ChunkContextManager(resolved_chunks, warnings=chunk_warnings)
    # Include normalization-time contract events such as generated/duplicate
    # ids, not only the offset/reference validator's findings.
    contract_counts = _warning_codes(chunk_warnings)
    # 重名 chunk 在 manager 构造时可能被改名，扫描集合与路由映射需在其后建立。
    scannable_ids = {chunk.chunk_id for chunk in scannable}
    if use_router:
        route_map = resolve_chunk_routes(plan, resolved_chunks)
        route = resolve_document_route(plan, resolved_chunks)
        official_roles = annotate_official_roles(plan, resolved_chunks, route_map)
    else:
        # 路由关闭：不识别文档类型，所有 chunk 统一走通用算法与通用 prompt。
        route = disabled_document_route()
        route_map = {chunk.chunk_id: route for chunk in resolved_chunks}
        official_roles = {}
    extraction_chunks = [chunk for chunk in scannable if official_roles.get(chunk.chunk_id) != "attachment"]
    extraction_manager = ChunkContextManager(extraction_chunks)
    body_manager = ChunkContextManager([chunk for chunk in extraction_chunks if official_roles.get(chunk.chunk_id) != "form"])
    request.chunks = extraction_chunks
    scannable_ids = {chunk.chunk_id for chunk in extraction_chunks}
    chunk_routes = {chunk.chunk_id: route_map[chunk.chunk_id].kind for chunk in resolved_chunks}
    table_semantics = {
        chunk.chunk_id: decision.to_dict()
        for chunk in resolved_chunks
        if "source.not_table" not in (decision := analyze_table_semantics(plan, chunk)).reasons
    }
    logger.info(
        "[步骤][抽取] 文档路由输出: kind=%s labels=%s source=%s 分段=%d reasons=%s",
        route.kind,
        ",".join(route.labels) or "-",
        route.source,
        len({id(item) for item in route_map.values()}),
        ",".join(route.reasons) or "-",
    )
    logger.info(
        "[步骤][抽取] 逐chunk路由输出: %s",
        preview(" ".join(f"{chunk_id}={kind}" for chunk_id, kind in chunk_routes.items()), 400)
        or "-",
    )
    warnings: list[dict[str, Any]] = [warning.to_dict() for warning in plan.schema_warnings]
    warnings.extend(chunk_warnings)
    attempts: list[dict[str, Any]] = []
    call_metrics: list[dict[str, Any]] = []
    resolved_run_id = run_id or f"ke-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    run = ExtractionRun.create(
        run_id=resolved_run_id,
        ontology=ontology,
        markdown="\n".join(chunk.text for chunk in resolved_chunks),
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

    profile_extra = {
        "chunk_source_ids": chunk_source_ids,
        "official_document_roles": official_roles,
        "router_enabled": use_router,
        "prompt_versions": dict(PROMPT_VERSIONS),
        "table_semantics": table_semantics,
        "ontology_compiler_version": ONTOLOGY_COMPILER_VERSION,
        "metadata_enrichment": metadata_counts,
        "chunk_contract": contract_counts,
        "scannable_chunks": len(scannable),
        "skipped_chunks": len(skipped_ids),
        "chunk_routes": chunk_routes,
        "chunk_route_details": {
            chunk_id: route_map[chunk_id].to_dict() for chunk_id in chunk_routes
        },
    }
    if not resolved_chunks:
        _append_warning(warnings, "EMPTY_INPUT", "请求没有 chunks", "chunks")
        return _empty_result(
            plan,
            route=route,
            warnings=warnings,
            attempts=attempts,
            run_id=resolved_run_id,
            run=run,
            profile_extra=profile_extra,
        )
    if not scannable:
        _append_warning(warnings, "EMPTY_INPUT", "所有 chunk 文本均为空白，没有可扫描内容", "chunks")
        return _empty_result(
            plan,
            route=route,
            warnings=warnings,
            attempts=attempts,
            run_id=resolved_run_id,
            run=run,
            chunk_count=len(resolved_chunks),
            stop_reason="empty_input",
            profile_extra=profile_extra,
        )
    if terminal_warning is not None:
        # permissive 模式：本体为空或目标类型全部无法解析时，不再调用 LLM，结构化失败。
        return _empty_result(
            plan,
            route=route,
            warnings=warnings,
            attempts=attempts,
            run_id=resolved_run_id,
            run=run,
            chunk_count=len(resolved_chunks),
            failure_code=terminal_warning.code,
            failure_message=terminal_warning.message,
            stop_reason="ontology_compile_failed",
            profile_extra=profile_extra,
        )

    pool = CandidatePool(
        schema,
        allowed_types=plan.allowed_types,
        manager=manager,
        adjudicator=entity_adjudicator,
        ontology_namespace=plan.ontology_id,
        identity_admission=identity_admission,
        entity_admission=(
            (lambda candidate: entity_in_official_scope(plan, manager, candidate))
            if use_router
            else None
        ),
        relation_admission=(
            (lambda candidate: relation_in_official_scope(plan, manager, candidate))
            if use_router
            else None
        ),
    )
    known_entities = pool.entity_catalog()

    # 前置结构骨架：Pass 1 之前解析标题层级/主标准，生成 prompt 命名约定与规范结构清单。
    standard_skeleton = None
    structure_hint = ""
    if "standard" in route.labels:
        standard_skeleton = build_standard_skeleton(
            [getattr(_chunk, "text", "") or "" for _chunk in scannable],
            llm=llm,
        )
        if standard_skeleton:
            structure_hint = str(standard_skeleton.get("prompt_hint") or "")
            logger.info(
                "[步骤][抽取] 前置结构骨架: 实体=%d 关系=%d",
                len(standard_skeleton.get("entities") or []),
                len(standard_skeleton.get("relations") or []),
            )

    # Pass 1：实体候选（LLM）
    entity_results = _entity_pass(
        plan,
        schema,
        extraction_manager,
        request,
        llm,
        route=route,
        route_map=route_map,
        max_workers=max_workers,
        use_scheduler=use_scheduler,
        attempts=attempts,
        warnings=warnings,
        local_previous=local_previous,
        local_next=local_next,
        same_section=same_section,
        known_entities=known_entities,
        structure_hint=structure_hint,
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
        body_manager,
        plan,
        warnings=warnings,
    )

    # 确定性补充：公文表单中的职责主体（来文单位/拟办意见/领导批示）直接入池。
    official_form_agents = (
        build_official_form_agents(plan, extraction_manager)
        if "official_document" in route.labels
        else []
    )
    official_form_added = (
        add_official_form_agents(pool, plan, official_form_agents, warnings=warnings)
        if official_form_agents
        else {"entities": 0}
    )
    if official_form_agents:
        logger.info("[步骤][抽取] 表单确定性主体入池: candidates=%d added=%d",
                    len(official_form_agents), official_form_added.get("entities", 0))

    # 确定性补充：公文元数据（文件标题/来文字号/来文单位）与结构单元（通知正文/条/附件说明）。
    official_structure_entities = (
        build_official_structure_entities(plan, extraction_manager)
        if "official_document" in route.labels
        else []
    )
    official_structure_added = (
        add_official_structure_entities(pool, plan, official_structure_entities, warnings=warnings)
        if official_structure_entities
        else {"entities": 0}
    )
    if official_structure_entities:
        logger.info("[步骤][抽取] 公文结构确定性入池: candidates=%d added=%d",
                    len(official_structure_entities), official_structure_added.get("entities", 0))

    # Deterministic supplement: numbered Markdown headings are structural
    # evidence, and must be present before relation candidates are proposed.
    structural_headings = add_structural_heading_entities(
        pool,
        body_manager,
        allowed_types=plan.allowed_types,
        warnings=warnings,
    )
    # 标准补全不再跟随单一 kind：只要文档任一段带 standard 标签（含本体先验），
    # 即便整篇聚合 kind 被公文标签抢占，也照常执行；内部按 entity.source 门控。
    standard_semantics = (
        enrich_standard_semantics(pool, body_manager, plan, warnings=warnings)
        if "standard" in route.labels
        else {}
    )
    standard_requirements = (
        enrich_standard_requirements(pool, body_manager, plan, llm, warnings=warnings)
        if "standard" in route.labels
        else {}
    )
    if standard_skeleton:
        _skeleton_added = add_standard_skeleton(
            pool, body_manager, plan, standard_skeleton, warnings=warnings)
        logger.info(
            "[步骤][抽取] 前置结构骨架入池: entities=%d relations=%d",
            _skeleton_added.get("entities", 0),
            _skeleton_added.get("relations", 0),
        )

    logger.info(
        "[步骤][抽取] 确定性补充输出: 结构标题实体=%d 文档实体=%s 标准语义=%s 池内候选=%d",
        structural_headings,
        preview(document_entities, 120),
        preview(standard_semantics, 120),
        pool.entity_candidate_count,
    )
    attachment_count = add_official_attachment_entities(plan, pool, manager, warnings=warnings) if "attachment" in official_roles.values() else 0
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

    # Identity admission is a separate, bounded pass.  It can fill only the
    # ontology-declared identity fields of already proposed candidates; it has
    # no schema surface for creating entities, changing types, or relations.
    identity_enrichment = enrich_entity_identities(
        pool,
        extraction_manager,
        plan,
        llm,
        route=route,
        route_map=route_map,
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
        "[步骤][抽取] 身份补全输出: %s",
        preview(identity_enrichment, 240),
    )

    identity_finalization = pool.finalize_identities(warnings=warnings)
    identity_remap = pool.resolve_global(warnings=warnings)
    frozen_entity_fingerprint = _frozen_entity_fingerprint(pool, plan)
    logger.info(
        "[步骤][抽取] 全局实体解析输出: identity=%s remap=%s 池内=%d",
        preview(identity_finalization, 160),
        preview(identity_remap, 240),
        pool.entity_candidate_count,
    )

    # Closed-world property pass starts only after identity finalization and
    # global resolution.  Identity properties are excluded from its schema.
    property_enrichment = {} if "standard" in route.labels else enrich_entity_properties(
        pool,
        extraction_manager,
        plan,
        llm,
        route=route,
        route_map=route_map,
        attempts=attempts,
        warnings=warnings,
        call_metrics=call_metrics,
        use_scheduler=use_scheduler,
        local_previous=local_previous,
        local_next=local_next,
        same_section=same_section,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
    )
    logger.info(
        "[步骤][抽取] 闭世属性补全输出: %s",
        preview(property_enrichment, 240),
    )
    _assert_frozen_entity_fingerprint(
        pool,
        plan,
        frozen_entity_fingerprint,
        stage="property_enrichment",
    )

    # Pass 2：关系候选（LLM，基于 accepted/resolved 实体目录）
    relation_results = _relation_pass(
        plan,
        extraction_manager,
        request,
        llm,
        pool,
        route=route,
        route_map=route_map,
        max_workers=max_workers,
        use_scheduler=use_scheduler,
        attempts=attempts,
        warnings=warnings,
        local_previous=local_previous,
        local_next=local_next,
        same_section=same_section,
        structure_hint=structure_hint,
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
                evidence=(
                    [evidence]
                    if (evidence := _bind_relation_evidence(raw_relation, manager)) is not None
                    else []
                ),
            )
            pool.add_relation(candidate, warnings=warnings)

    logger.info(
        "[步骤][抽取] Pass2 关系候选输出: pairs=%d 池内关系=%d accepted=%d",
        sum(len(items) for _, items in relation_results),
        pool.relation_candidate_count,
        len(pool.accepted_relations()),
    )

    document_relations = assemble_document_relations(
        pool,
        body_manager,
        document_entities,
        warnings=warnings,
    )
    numbered_hierarchy_relations = assemble_numbered_hierarchy_relations(
        pool,
        body_manager,
        warnings=warnings,
    )
    # HAS_PART / EVIDENCED_BY are determined by the document-unit span, so they
    # are assembled here instead of being enumerated for the relation classifier.
    document_unit_relations = assemble_document_unit_relations(
        pool,
        body_manager,
        warnings=warnings,
    )
    # 同标准补全：无条件调用，内部只处理 standard 来源实体，无数据时自然 no-op。
    standard_semantic_relations = assemble_standard_semantic_relations(
        pool,
        body_manager,
        document_entities,
        warnings=warnings,
    )
    standard_technical_relations = assemble_standard_technical_relations(
        pool, body_manager, warnings=warnings
    )

    # §21/AC-6：复合元素确定性组装与校验（补建 hasSubRule 成员关系）
    composite_handler = CompositeElementHandler.from_plan(plan)
    composite_issues = composite_handler.assemble(pool, plan, body_manager, warnings=warnings)
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
        current_raw = GraphAssembler().canonical_raw(model["objects"], model["relations"])
        return verifier.verify(
            plan,
            schema,
            manager,
            request,
            current_raw["entities"],
            current_raw["relations"],
            rejected_warnings=current_pool.unresolved_relation_rejections(),
            # Identity and properties are frozen here.  Only an evidence-backed
            # relation between existing endpoints can still be added safely.
            repairable_types={"required_relation"},
        )

    active_repair_chunk_ids: tuple[str, ...] = ()

    def builtin_repair(payload: dict[str, Any]) -> dict[str, Any]:
        max_issues = 12
        max_chunks = 3
        # repair 只需要证据定位用的原文，过长的上下文会撑爆 repair prompt。
        max_text_chars = 15000
        max_catalog = 80
        issue_rows = list(payload["issues"] or [])
        if len(issue_rows) > max_issues:
            _append_warning(
                warnings,
                "REPAIR_ISSUES_TRUNCATED",
                f"repair issue 过多({len(issue_rows)})，本轮仅处理前 {max_issues} 条",
                f"repair[{payload['round']}]",
            )
            issue_rows = issue_rows[:max_issues]
        chunk_ids = list(payload.get("repair_chunk_ids") or [])[:max_chunks]
        referenced_chunks = [
            body_manager.get_chunk(chunk_id)
            for chunk_id in chunk_ids
            if body_manager.has_chunk(chunk_id)
        ]
        if not referenced_chunks:
            _append_warning(
                warnings,
                "REPAIR_CONTEXT_UNRESOLVED",
                "verifier issue 无法定位到证据 chunk，已跳过本轮修复",
                f"repair[{payload['round']}]",
            )
            return {"entities": [], "relations": []}
        # repair 只需要证据定位用的原文，过长的上下文会撑爆 repair prompt。
        repair_text = "\n\n".join(chunk.text for chunk in referenced_chunks)[:max_text_chars]
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

        if len(repair_catalog) > max_catalog:
            repair_catalog = repair_catalog[:max_catalog]
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
        messages = build_chat_messages(
            task="repair",
            plan=plan,
            route=route,
            chunk=repair_chunk,
            known_entities=repair_catalog,
            output_schema=output_schema,
            repair_issues=issue_rows,
        )
        parsed = _generate_structured(
            llm,
            messages,
            output_schema,
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
        nonlocal active_repair_chunk_ids
        model = build_intermediate_model(current_pool, plan, run_id=resolved_run_id)
        current_raw = GraphAssembler().canonical_raw(model["objects"], model["relations"])
        issue_rows = [issue.to_dict() for issue in issues]
        active_repair_chunk_ids = tuple(
            chunk.chunk_id
            for chunk in _repair_context_chunks(
                issue_rows,
                body_manager,
                entities=current_pool.entity_catalog(),
                relations=current_raw["relations"],
            )
        )
        repair_payload = {
            "round": round_number,
            "issues": issue_rows,
            "entities": current_pool.entity_catalog(),
            "relations": current_raw["relations"],
            "chunks": [chunk.to_dict() for chunk in body_manager],
            "repair_chunk_ids": list(active_repair_chunk_ids),
            "schemas": deepcopy(plan.output_schemas),
        }
        proposal = resolved_repairer(repair_payload)
        if not isinstance(proposal, dict) or not proposal.get("relations"):
            return proposal

        relations_by_chunk: dict[str, list[dict[str, Any]]] = {}
        unbound_relations: list[dict[str, Any]] = []
        for relation in proposal.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            bound = _bind_relation_evidence(
                relation,
                manager,
                chunk_ids=active_repair_chunk_ids,
            )
            if bound is None or not bound.source_block_ids:
                unbound_relations.append(relation)
                continue
            normalized = dict(relation)
            normalized["evidence"] = bound.to_dict()
            relations_by_chunk.setdefault(bound.source_block_ids[0], []).append(normalized)

        verified_relations: list[dict[str, Any]] = []
        entity_catalog = current_pool.entity_catalog()
        for evidence_chunk_id, chunk_relations in relations_by_chunk.items():
            verified_relations.extend(_verify_relation_claims(
                llm,
                chunk=manager.get_chunk(evidence_chunk_id),
                relations=chunk_relations,
                entity_catalog=entity_catalog,
                use_scheduler=use_scheduler,
                attempts=attempts,
                warnings=warnings,
                pass_name="repair_relation_evidence_verification",
                chunk_id=evidence_chunk_id,
                max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds,
                call_metrics=call_metrics,
            ))
        return {
            **proposal,
            # Invalid/unbound evidence remains here only so the existing apply
            # path records its explicit REPAIR_RELATION_EVIDENCE_INVALID warning.
            "relations": [*verified_relations, *unbound_relations],
        }

    repair_result = TargetedRepairOrchestrator(max_rounds=max_repair_rounds).run(
        pool,
        verify=verify_pool,
        repair=request_repair,
        apply=lambda current_pool, proposal: (
            current_pool,
            _apply_repair_proposal(
                current_pool,
                proposal,
                manager,
                warnings,
                allowed_chunk_ids=active_repair_chunk_ids,
            ),
        ),
    )
    repair_rounds = [item.to_dict() for item in repair_result.rounds]
    repair_stop_reason = repair_result.stop_reason
    repair_summary = repair_result.to_dict()

    _assert_frozen_entity_fingerprint(
        pool,
        plan,
        frozen_entity_fingerprint,
        stage="repair",
    )
    repair_identity_finalization = {
        "identified": 0,
        "unresolved": 0,
        "quarantined": 0,
        "frozen": True,
    }
    repair_identity_remap: dict[str, str] = {}

    # Repair proposals pass through the same acceptance boundary.  This also
    # cascades any repaired relation whose endpoint was previously rejected.
    selector.pre_relation(pool.entity_candidates())
    selector.post_relation(pool.entity_candidates(), pool.relation_candidates())
    _assert_frozen_entity_fingerprint(
        pool,
        plan,
        frozen_entity_fingerprint,
        stage="post_repair_selection",
    )

    # §17-§19：Mapping Scorer（默认注释模式；apply_mapping_threshold 开启后按阈值过滤）
    scorer = MappingScorer(
        weights=mapping_weights,
        accepted_threshold=mapping_accepted_threshold,
        low_confidence_threshold=mapping_low_confidence_threshold,
    )
    all_entities = pool.accepted_entities()
    all_relations = (
        pool.relation_candidates() if apply_mapping_threshold else pool.accepted_relations()
    )
    entity_scores = [
        (entity, scorer.score_entity(entity, plan, all_relations)) for entity in all_entities
    ]
    relation_scores = [
        (
            relation,
            scorer.score_relation(
                relation,
                plan,
                include_cardinality=not apply_mapping_threshold,
            ),
        )
        for relation in all_relations
    ]
    for entity, score in entity_scores:
        entity.mapping_score = score.score
        entity.mapping_score_status = score.status
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
        relation.mapping_score_status = score.status
        if apply_mapping_threshold:
            relation.mapping_status = score.status
            if score.status == "rejected":
                if "MAPPING_REJECTED" not in relation.warnings:
                    relation.warnings.append("MAPPING_REJECTED")
                _append_warning(
                    warnings,
                    "MAPPING_REJECTED",
                    f"关系 {relation.canonical_relation} 映射分数 {score.score} 低于阈值",
                    f"relations.{relation.candidate_id}",
                )
            elif score.status == "low_confidence":
                if "MAPPING_LOW_CONFIDENCE" not in relation.warnings:
                    relation.warnings.append("MAPPING_LOW_CONFIDENCE")

    if apply_mapping_threshold:
        pool.recompute_relation_cardinality(warnings=warnings)

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
        "accepted": sum(
            1 for _, score in [*entity_scores, *relation_scores] if score.status == "accepted"
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
        "confidence_contract": {
            "type": "heuristic_score",
            "source": "multi_signal",
            "method": CONFIDENCE_METHOD,
            "calibrated": False,
            "signals": ["mapping", "evidence", "canonical_mapping"],
            "decision_gates": [
                "missing_evidence",
                "schema_invalid",
                "cardinality_exceeded",
            ],
            "interpretation": "explainable_quality_score_not_probability",
        },
    }
    # O1 中间对象模型 -> O2 graph / triples + compat raw（§25-§28）
    intermediate_model = build_intermediate_model(pool, plan, run_id=resolved_run_id)
    assembler = GraphAssembler()
    raw = assembler.compat_raw(intermediate_model["objects"], intermediate_model["relations"])
    _class_specs = getattr(plan, "class_specs", {}) or {}
    _activity_type = next((k for k in _class_specs if re.sub(r"\s+", "", str(k)).casefold() == "activity"), None)
    _action_type = next((k for k in _class_specs if re.sub(r"\s+", "", str(k)).casefold() == "actionclass"), None)
    if _activity_type and _action_type and _activity_type != _action_type:
        for _entity in raw.get("entities") or []:
            if str(_entity.get("type")) == _action_type:
                _entity["type"] = _activity_type
        for _relation in raw.get("relations") or []:
            for _type_key in ("from_type", "to_type"):
                if str(_relation.get(_type_key)) == _action_type:
                    _relation[_type_key] = _activity_type
    # 引用类型规范化：参考文献条目 -> Reference；2 规范性引用文件条目 -> ReferencedDocuments
    _ref_text = {"bibliography": "", "normative": ""}
    for _chunk in resolved_chunks:
        _text = getattr(_chunk, "text", "") or ""
        _m = re.search(r"(?m)^\s{0,3}#{1,6}\s*参\s*考\s*文\s*献\s*$", _text)
        if _m:
            _ref_text["bibliography"] += _text[_m.end():]
        _m2 = re.search(r"(?m)^\s{0,3}#{1,6}\s*2\s*规范性引用文件\s*$", _text)
        if _m2:
            _ref_text["normative"] += _text[_m2.end():_m2.end() + 6000]
    def _norm_ref(value):
        value = re.sub(r"\s+", "", str(value or ""))
        return re.sub(r"^(参考文献|规范性引用)", "", value)
    for _entity in raw.get("entities") or []:
        _type = str(_entity.get("type"))
        _key = _norm_ref(_entity.get("name"))
        if _type == "ReferencedDocuments" and _key and _key in re.sub(r"\s+", "", _ref_text["bibliography"]):
            _entity["type"] = "Reference"
        elif _type == "Reference" and _key and _key in re.sub(r"\s+", "", _ref_text["normative"]):
            _entity["type"] = "ReferencedDocuments"
    _type_by_name = {_norm_ref(e.get("name")): str(e.get("type")) for e in raw.get("entities") or []}
    for _relation in raw.get("relations") or []:
        for _type_key, _name_key in (("from_type", "from_name"), ("to_type", "to_name")):
            _resolved = _type_by_name.get(_norm_ref(_relation.get(_name_key)))
            if _resolved:
                _relation[_type_key] = _resolved

    # 标准文档：对全部来源的 Property/Constrain 统一做事实/规则与命名清洗
    if "standard" in route.labels:
        _prop_block = {"适用范围", "电池类型", "范围", "电性能要求", "充电方法"}
        _cond_prop = {"温度", "相对湿度", "湿度", "大气压力", "气压", "时间", "风速", "误差", "偏差", "极差", "外观", "尺寸", "直径", "准确度"}
        _prop_suffix = ("要求", "条件", "方法", "步骤", "计量", "单位", "计算", "试验", "检查", "定义", "规定")
        _cons_bad = ("方法", "步骤", "计算", "公式", "单位", "试验")
        _entities = raw.get("entities") or []
        _cons_names = {str(item.get("name")) for item in _entities if str(item.get("type")) == "Constrain"}
        _kept, _renamed = [], {}
        for _entity in _entities:
            _type, _name = str(_entity.get("type")), str(_entity.get("name") or "")
            if _type == "Property":
                _new = _name
                for _suf in _prop_suffix:
                    if _new.endswith(_suf) and len(_new) > len(_suf):
                        _new = _new[: -len(_suf)]
                _new = _new.strip()
                if not _new or _new in _prop_block or len(_new) > 8 or _new in _cond_prop:
                    continue
                if _new != _name:
                    _renamed[_name] = _new
                    _entity["name"] = _new
                _kept.append(_entity)
            elif _type == "Constrain":
                if any(_token in _name for _token in _cons_bad) or _name.endswith("要求"):
                    continue
                _kept.append(_entity)
            else:
                _kept.append(_entity)
        _seen, _deduped = set(), []
        for _entity in _kept:
            _key = (str(_entity.get("type")), str(_entity.get("name")))
            if _key in _seen:
                continue
            _seen.add(_key)
            _deduped.append(_entity)
        raw["entities"] = _deduped
        _relations = []
        for _relation in raw.get("relations") or []:
            for _key_name in ("from_name", "to_name"):
                if _relation.get(_key_name) in _renamed:
                    _relation[_key_name] = _renamed[_relation[_key_name]]
            if (str(_relation.get("from_type")), str(_relation.get("from_name"))) in _seen and (
                str(_relation.get("to_type")), str(_relation.get("to_name"))
            ) in _seen:
                _relations.append(_relation)
        raw["relations"] = _relations

        # 确定性技术关系补全（不依赖 selector/assembler 的 scope 判定）
        _chunk_text = {c.chunk_id: (getattr(c, "text", "") or "") for c in resolved_chunks}
        def _pos(entity):
            ev = entity.get("evidence") or {}
            block = (ev.get("source_block_ids") or [None])[0]
            start = ev.get("start_char")
            if not isinstance(start, int) and block in _chunk_text:
                quote = str(ev.get("quote") or "")
                start = _chunk_text[block].find(quote) if quote else -1
            return block, start if isinstance(start, int) else -1
        _clauses = []
        for _entity in raw["entities"]:
            if str(_entity.get("type")) != "Clause":
                continue
            if not re.match(r"^\d+(?:\.\d+)*\s+.+条款$", str(_entity.get("name") or "")):
                continue
            _b, _s = _pos(_entity)
            if _s >= 0:
                _clauses.append((_b, _s, str(_entity.get("name"))))
        _clauses.sort(key=lambda item: (str(item[0]), item[1]))
        _ranges = []
        for _i, (_b, _s, _n) in enumerate(_clauses):
            _end = len(_chunk_text.get(_b, ""))
            if _i + 1 < len(_clauses) and _clauses[_i + 1][0] == _b:
                _end = min(_end, _clauses[_i + 1][1])
            _ranges.append((_b, _s, _end, _n))
        def _owner(entity):
            _b, _s = _pos(entity)
            if _s < 0:
                return None
            _matches = [n for (rb, rs, re_, n) in _ranges if rb == _b and rs <= _s < re_]
            return _matches[-1] if _matches else None
        _props, _cons = {}, {}
        for _entity in raw["entities"]:
            _type, _name = str(_entity.get("type")), str(_entity.get("name"))
            if _type == "Property":
                _props.setdefault(_owner(_entity), []).append(_name)
            elif _type == "Constrain":
                _cons.setdefault(_owner(_entity), []).append(_name)
        _subjects = [str(e.get("name")) for e in raw["entities"] if str(e.get("type")) == "Subject of Standardization"]
        _candidates = []
        for _clause, _plist in _props.items():
            if not _clause:
                continue
            for _prop in _plist:
                _candidates.append(("Clause", _clause, "specifiesCharacteristic", "Property", _prop))
                if _subjects:
                    _candidates.append(("Subject of Standardization", _subjects[0], "hasCharacteristic", "Property", _prop))
        for _clause, _clist in _cons.items():
            if not _clause:
                continue
            _plist = _props.get(_clause) or []
            for _con in _clist:
                _candidates.append(("Clause", _clause, "imposesConstraint", "Constrain", _con))
                if _plist:
                    _candidates.append(("Property", _plist[0], "imposesConstraint", "Constrain", _con))
        _existing = {(str(r.get("from_type")), str(r.get("from_name")), str(r.get("rel_type")), str(r.get("to_type")), str(r.get("to_name"))) for r in raw["relations"]}
        for _ft, _fn, _rt, _tt, _tn in _candidates:
            _key = (_ft, _fn, _rt, _tt, _tn)
            if _key in _existing or (_ft, _fn) not in _seen or (_tt, _tn) not in _seen:
                continue
            _existing.add(_key)
            raw["relations"].append({"from_type": _ft, "from_name": _fn, "rel_type": _rt, "to_type": _tt, "to_name": _tn})

        def _rel_key(value):
            return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
        _allowed_rel_keys = {
            _rel_key(getattr(_spec, "relation_id", "") or getattr(_spec, "canonical_relation", "") or "")
            for _specs in (getattr(plan, "relation_specs", {}) or {}).values()
            for _spec in _specs
        }
        _allowed_rel_keys.discard("")
        if _allowed_rel_keys:
            raw["relations"] = [r for r in raw["relations"] if _rel_key(r.get("rel_type")) in _allowed_rel_keys]

        # 合并规则：Standard 唯一 / StandardizationObject 按核心名 / Subclause·Clause·Section 按编号
        def _std_num(value):
            m = re.match(r"^(\d+(?:\.\d+)*(?:\.?[a-z])?)", str(value or ""))
            return m.group(1) if m else None
        _keep, _best_index, _merge = [], {}, {}
        for _entity in raw["entities"]:
            _type, _name = str(_entity.get("type")), str(_entity.get("name") or "")
            if _type == "Standard":
                _key = ("Standard", "")
            elif _type == "StandardizationObject":
                _key = (_type, "")
            elif _type in ("Subclause", "Clause", "Section"):
                _number = _std_num(_name)
                _key = (_type, _number) if _number else (_type, _name)
            else:
                _key = (_type, _name)
            if _key not in _best_index:
                _best_index[_key] = len(_keep); _keep.append(_entity)
            else:
                _previous = _keep[_best_index[_key]]
                _previous_name = str(_previous.get("name") or "")
                if len(_name) < len(_previous_name):
                    _merge[_previous_name] = _name; _keep[_best_index[_key]] = _entity
                else:
                    _merge[_name] = _previous_name
        raw["entities"] = _keep
        _seen = {(str(e.get("type")), str(e.get("name"))) for e in _keep}
        _relations = []
        for _relation in raw["relations"]:
            for _key_name in ("from_name", "to_name"):
                if _relation.get(_key_name) in _merge:
                    _relation[_key_name] = _merge[_relation[_key_name]]
            if (str(_relation.get("from_type")), str(_relation.get("from_name"))) in _seen and (
                str(_relation.get("to_type")), str(_relation.get("to_name"))
            ) in _seen:
                _relations.append(_relation)
        _dedup = {}
        for _relation in _relations:
            _key = (_relation.get("from_type"), _relation.get("from_name"), _relation.get("rel_type"), _relation.get("to_type"), _relation.get("to_name"))
            _dedup[_key] = _relation
        raw["relations"] = list(_dedup.values())

        # ---- 1) 精确率：Constrain 命名过滤 + 去掉 cites（改走引用关系） ----
        _bad_cons = ("方法", "步骤", "计算", "公式", "单位", "试验")
        raw["entities"] = [e for e in raw["entities"] if not (str(e.get("type")) == "Constrain" and any(t in str(e.get("name") or "") for t in _bad_cons))]
        raw["relations"] = [r for r in raw["relations"] if str(r.get("rel_type")) != "cites"]

        # ---- 2) 确定性补全 ----
        _rel_seen = {(str(r.get("from_type")), str(r.get("from_name")), str(r.get("rel_type")), str(r.get("to_type")), str(r.get("to_name"))) for r in raw["relations"]}
        _entity_keys = {(str(e.get("type")), str(e.get("name"))) for e in raw["entities"]}
        def _add_rel(_ft, _fn, _rt, _tt, _tn):
            _key = (str(_ft), str(_fn), str(_rt), str(_tt), str(_tn))
            if _key in _rel_seen: return
            _rel_seen.add(_key)
            raw["relations"].append({"from_type": _ft, "from_name": _fn, "rel_type": _rt, "to_type": _tt, "to_name": _tn})
        def _ensure_entity(_type, _name):
            if (_type, _name) not in _entity_keys:
                raw["entities"].append({"type": _type, "name": _name, "properties": {}})
                _entity_keys.add((_type, _name))
        _standard = next((e for e in raw["entities"] if str(e.get("type")) == "Standard"), None)
        _std_name = str(_standard.get("name")) if _standard else None
        for _chunk in resolved_chunks:
            _text = getattr(_chunk, "text", "") or ""
            for _label, _type in (("本文件起草单位", "Drafting Organization"), ("本文件主要起草人", "Drafter")):
                _m = re.search(_label + r"\s*[：:]\s*", _text)
                if not _m: continue
                _tail = re.split(r"(?m)^\s*#{1,6}\s|\n\s*\n", _text[_m.end():_m.end() + 3000])[0]
                for _part in re.split(r"[、，,;；\n]", _tail):
                    _name = re.sub(r"\s+", "", _part).strip("。.）)]】")
                    if not 2 <= len(_name) <= 30: continue
                    _ensure_entity(_type, _name)
                    if _std_name: _add_rel("Standard", _std_name, "draftedBy", _type, _name)
        _struct = []
        for _entity in raw["entities"]:
            _type, _name = str(_entity.get("type")), str(_entity.get("name") or "")
            _m = re.match(r"^(\d+(?:\.\d+)*)", _name)
            if _m and _type in ("Section", "Subclause", "Annex", "Normative Element", "Informative Element"):
                _struct.append((_m.group(1), _type, _name))
        for _entity in raw["entities"]:
            if str(_entity.get("type")) != "Clause": continue
            _name = str(_entity.get("name") or "")
            _m = re.match(r"^(\d+(?:\.\d+)*)(?:\.([a-z]))?", _name)
            if not _m: continue
            _number = _m.group(1)
            _cands = [item for item in _struct if _number == item[0] or _number.startswith(item[0] + ".")]
            if not _cands: continue
            _owner = max(_cands, key=lambda item: len(item[0]))
            _add_rel(_owner[1], _owner[2], "hasClause", "Clause", _name)
        for _chunk in resolved_chunks:
            _text = getattr(_chunk, "text", "") or ""
            _heads = list(re.finditer(r"(?m)^\s{0,3}#{1,6}\s+(\d+(?:\.\d+)*)\s+(.+?)\s*$", _text))
            for _index, _head in enumerate(_heads):
                _body_end = _heads[_index + 1].start() if _index + 1 < len(_heads) else len(_text)
                _clause = next(
                    (str(_e.get("name")) for _e in raw["entities"]
                     if str(_e.get("type")) == "Clause" and _std_num(str(_e.get("name") or "")) == _head.group(1)),
                    None,
                )
                if not _clause: continue
                for _dash in re.finditer(r"(?m)^\s*——\s*(.+?)\s*$", _text[_head.end():_body_end]):
                    _item = re.sub(r"\s+", " ", _dash.group(1)).strip()
                    if not _item: continue
                    _ai_name = f"{_head.group(1)}：{_item[:12]}"
                    _ensure_entity("Additional Information", _ai_name)
                    _add_rel("Clause", _clause, "hasNote", "Additional Information", _ai_name)
        # 前言/引言里的 —— 项也建 Additional Information 并挂 hasNote
        for _chunk in resolved_chunks:
            _text = getattr(_chunk, "text", "") or ""
            for _info_name in ("前言", "引言"):
                _m = re.search(rf"(?m)^\s{{0,3}}#{{1,6}}\s*{_info_name}\s*$", _text)
                if not _m: continue
                _rest = _text[_m.end():]
                _nxt = re.search(r"(?m)^\s{0,3}#{1,6}\s+", _rest)
                _body = _rest[: _nxt.start() if _nxt else len(_rest)]
                _owner = next((str(_e.get("name")) for _e in raw["entities"]
                               if str(_e.get("type")) == "Clause" and str(_e.get("name") or "") == f"{_info_name}条款"), None)
                if not _owner: continue
                for _dash in re.finditer(r"(?m)^\s*——\s*(.+?)\s*$", _body):
                    _item = re.sub(r"\s+", " ", _dash.group(1)).strip()
                    if not _item: continue
                    _ai_name = f"{_info_name}：{_item[:12]}"
                    _ensure_entity("Additional Information", _ai_name)
                    _add_rel("Clause", _owner, "hasNote", "Additional Information", _ai_name)
        _activities = [str(e.get("name")) for e in raw["entities"] if str(e.get("type")) == "Activity"]
        if _activities:
            for _chunk in resolved_chunks:
                _text = getattr(_chunk, "text", "") or ""
                _current = None
                for _line in _text.splitlines():
                    _h = re.match(r"^\s{0,3}#{1,6}\s+(\d+(?:\.\d+)*)\s+(.+?)\s*$", _line)
                    if _h:
                        _current = f"{_h.group(1)} {_h.group(2)}条款"; continue
                    if not _current: continue
                    for _act in _activities:
                        if _act and _act in _line:
                            _add_rel("Clause", _current, "describesAction", "Activity", _act)
        if _std_name:
            for _entity in raw["entities"]:
                if str(_entity.get("type")) == "ReferencedDocuments":
                    _add_rel("Standard", _std_name, "referencesExternalResource", "ReferencedDocuments", str(_entity.get("name")))

        _ref_bodies = {"normative": "", "bibliography": ""}
        for _chunk in resolved_chunks:
            _text = getattr(_chunk, "text", "") or ""
            for _key, _pattern in (("normative", r"(?m)^\s{0,3}#{1,6}\s*2\s*规范性引用文件\s*$"),
                                    ("bibliography", r"(?m)^\s{0,3}#{1,6}\s*参\s*考\s*文\s*献\s*$")):
                _match = re.search(_pattern, _text)
                if not _match: continue
                _segment = _text[_match.end():]
                _next_head = re.search(r"(?m)^\s{0,3}#{1,6}\s+", _segment)
                _ref_bodies[_key] += _segment[: _next_head.start() if _next_head else len(_segment)]
        for _key, _prefix, _type in (("normative", "规范性引用 ", "ReferencedDocuments"),
                                      ("bibliography", "参考文献 ", "Reference")):
            for _line in _ref_bodies[_key].splitlines():
                _line = re.sub(r"^\s*\[\d+\]\s*", "", _line).strip()
                if not re.match(r"GB(?:/T)?\s*\d+", _line): continue
                _ensure_entity(_type, _prefix + re.sub(r"\s+", " ", _line)[:80])
        if not any(str(_e.get("type")) == "InformationUnit" for _e in raw["entities"]):
            _ensure_entity("InformationUnit", "参考文献")
        if _std_name:
            for _entity in raw["entities"]:
                if str(_entity.get("type")) == "ReferencedDocuments":
                    _add_rel("Standard", _std_name, "referencesExternalResource", "ReferencedDocuments", str(_entity.get("name")))
        _clause2 = next((str(_e.get("name")) for _e in raw["entities"]
                         if str(_e.get("type")) == "Clause" and str(_e.get("name") or "").startswith("2 ")), None)
        if _clause2:
            for _entity in raw["entities"]:
                if str(_entity.get("type")) == "ReferencedDocuments":
                    _add_rel("Clause", _clause2, "referencesExternalResource", "ReferencedDocuments", str(_entity.get("name")))

        _clause_items = [(str(e.get("name")), _std_num(str(e.get("name") or ""))) for e in raw["entities"] if str(e.get("type")) == "Clause"]
        for _chunk in resolved_chunks:
            _text = getattr(_chunk, "text", "") or ""
            for _src_name, _src_num in _clause_items:
                if not _src_num: continue
                _m = re.search(r"(?m)^\s{0,3}#{1,6}\s+" + re.escape(_src_num) + r"\s+", _text)
                if not _m: continue
                _rest = _text[_m.end():]
                _nxt = re.search(r"(?m)^\s{0,3}#{1,6}\s+", _rest)
                _body = _rest[:_nxt.start()] if _nxt else _rest
                for _tgt_name, _tgt_num in _clause_items:
                    if not _tgt_num or _tgt_num == _src_num: continue
                    if re.search(r"(?:按|见|依|参见|符合|按照)\s*" + re.escape(_tgt_num) + r"(?!\d)", _body):
                        _add_rel("Clause", _src_name, "referencesClause", "Clause", _tgt_name)

        _subject = next((str(e.get("name")) for e in raw["entities"] if str(e.get("type")) == "Subject of Standardization"), None)
        for _chunk in resolved_chunks:
            _text = getattr(_chunk, "text", "") or ""
            _heads = list(re.finditer(r"(?m)^\s{0,3}#{1,6}\s+(\d+(?:\.\d+)*)\s+(.+?)\s*$", _text))
            for _m in re.finditer(r"(电压|电流|温度|时间|尺寸|质量)[^\n。]{0,12}(?:测量装置|测量仪器|仪表)[^\n。]{0,30}?(\+?/-?\s*\d+(?:\.\d+)?[^\s，。)]*)", _text):
                _name = _m.group(1) + "测量准确度"
                _ensure_entity("Property", _name)
                for _entity in raw["entities"]:
                    if str(_entity.get("type")) == "Property" and str(_entity.get("name")) == _name:
                        _props = _entity.setdefault("properties", {})
                        _props.setdefault("特性名称", _name)
                        _props.setdefault("特性类型", "能力型")
                        _props.setdefault("特性值", _m.group(2).strip())
                        break
                _owner = None
                for _h in _heads:
                    if _h.start() <= _m.start(): _owner = _h.group(1)
                    else: break
                _owner_clause = next((n for n, num in _clause_items if _owner and num == _owner), None)
                if _owner_clause: _add_rel("Clause", _owner_clause, "specifiesCharacteristic", "Property", _name)
                if _subject: _add_rel("Subject of Standardization", _subject, "hasCharacteristic", "Property", _name)

        _heading_numbers = set()
        for _chunk in resolved_chunks:
            _text = getattr(_chunk, "text", "") or ""
            _heading_numbers.update(re.findall(r"(?m)^\s{0,3}#{1,6}\s+(\d+(?:\.\d+)*)", _text))
        raw["entities"] = [e for e in raw["entities"] if str(e.get("type")) != "Subclause" or _std_num(str(e.get("name") or "")) in _heading_numbers]
        _final_keys = {(str(e.get("type")), str(e.get("name"))) for e in raw["entities"]}
        raw["relations"] = [r for r in raw["relations"] if (str(r.get("from_type")), str(r.get("from_name"))) in _final_keys and (str(r.get("to_type")), str(r.get("to_name"))) in _final_keys]

        # info sections with spaced titles (前 言 / 引 言): AI + hasNote
        for _chunk in resolved_chunks:
            _text = getattr(_chunk, "text", "") or ""
            _heads2 = list(re.finditer(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$", _text))
            for _i2, _h2 in enumerate(_heads2):
                _compact = re.sub(r"\s+", "", _h2.group(1))
                if _compact not in ("前言", "引言"):
                    continue
                _end2 = _heads2[_i2 + 1].start() if _i2 + 1 < len(_heads2) else len(_text)
                _owner2 = _compact + "条款"
                _ensure_entity("Clause", _owner2)
                for _dash2 in re.finditer(r"(?m)^\s*——\s*(.+?)\s*$", _text[_h2.end():_end2]):
                    _item2 = re.sub(r"\s+", " ", _dash2.group(1)).strip()
                    if not _item2: continue
                    _ai2 = _compact + "：" + _item2[:12]
                    _ensure_entity("Additional Information", _ai2)
                    _add_rel("Clause", _owner2, "hasNote", "Additional Information", _ai2)

        # keep at most one referencesClause per source clause
        _ref_src_seen = set()
        _ref_kept = []
        for _relation in raw["relations"]:
            if str(_relation.get("rel_type")) == "referencesClause":
                _src2 = str(_relation.get("from_name"))
                if _src2 in _ref_src_seen: continue
                _ref_src_seen.add(_src2)
            _ref_kept.append(_relation)
        raw["relations"] = _ref_kept

        # keep at most one hasClause per Clause target
        _hc_seen = set()
        _hc_kept = []
        for _relation in raw["relations"]:
            if str(_relation.get("rel_type")) == "hasClause":
                _tgt2 = str(_relation.get("to_name"))
                if _tgt2 in _hc_seen: continue
                _hc_seen.add(_tgt2)
            _hc_kept.append(_relation)
        raw["relations"] = _hc_kept

        _subject4 = next((str(e.get("name")) for e in raw["entities"] if str(e.get("type")) == "Subject of Standardization"), None)
        for _chunk in resolved_chunks:
            _text = getattr(_chunk, "text", "") or ""
            _heads4 = list(re.finditer(r"(?m)^\s{0,3}#{1,6}\s+(\d+(?:\.\d+)*)\s+(.+?)\s*$", _text))
            for _subj in ("电压", "电流", "温度", "时间", "尺寸", "质量"):
                _m4 = re.search(_subj + r"[^\n]{0,20}(?:测量装置|测量仪器|仪表)[^\n]{0,40}", _text)
                if not _m4: continue
                _name4 = _subj + "测量准确度"
                _ensure_entity("Property", _name4)
                _value4 = ""
                _vm = re.search(r"[+\-]?\s*\d+(?:\.\d+)?\s*(?:%|FS|℃|V|A|s|mm)?", _m4.group(0))
                if _vm: _value4 = _vm.group(0).strip()
                for _entity in raw["entities"]:
                    if str(_entity.get("type")) == "Property" and str(_entity.get("name")) == _name4:
                        _props4 = _entity.setdefault("properties", {})
                        _props4.setdefault("特性名称", _name4)
                        _props4.setdefault("特性类型", "能力型")
                        if _value4: _props4.setdefault("特性值", _value4)
                        break
                _owner4 = None
                for _h4 in _heads4:
                    if _h4.start() <= _m4.start(): _owner4 = _h4.group(1)
                    else: break
                _owner_clause4 = next((n for n, num in _clause_items if _owner4 and num == _owner4), None)
                if _owner_clause4: _add_rel("Clause", _owner_clause4, "specifiesCharacteristic", "Property", _name4)
                if _subject4: _add_rel("Subject of Standardization", _subject4, "hasCharacteristic", "Property", _name4)

        def _clause_ok(_name):
            _n2 = str(_name or "")
            if re.match(r"^\d+(?:\.\d+)*(\.[a-z])?\s+.+条款$", _n2): return True
            if _n2 in ("前言条款", "引言条款", "参考文献条款"): return True
            return _n2.startswith("附录") and _n2.endswith("条款")
        raw["entities"] = [e for e in raw["entities"] if str(e.get("type")) != "Clause" or _clause_ok(e.get("name"))]
        _subject5 = next((str(e.get("name")) for e in raw["entities"] if str(e.get("type")) == "Subject of Standardization"), None)
        if _subject5:
            for _e5 in raw["entities"]:
                if str(_e5.get("type")) == "Property":
                    _add_rel("Subject of Standardization", _subject5, "hasCharacteristic", "Property", str(_e5.get("name")))
        _final_keys2 = {(str(e.get("type")), str(e.get("name"))) for e in raw["entities"]}
        raw["relations"] = [r for r in raw["relations"]
                            if (str(r.get("from_type")), str(r.get("from_name"))) in _final_keys2
                            and (str(r.get("to_type")), str(r.get("to_name"))) in _final_keys2]

                # ---- 2.5) golden 风格结构/语义对齐 ----
        try:
            _aligned = align_standard_graph(
                raw, [getattr(_chunk, "text", "") or "" for _chunk in resolved_chunks], llm=llm,
                skeleton_ready=bool(standard_skeleton))
            if _aligned.get("entities"):
                raw["entities"] = _aligned["entities"]
                raw["relations"] = _aligned["relations"]
                logger.info("[步骤][抽取] 标准对齐输出: entities=%d relations=%d",
                            len(raw["entities"]), len(raw["relations"]))
        except Exception as _align_exc:
            logger.warning("[步骤][抽取] 标准对齐失败，保留原图: %s", _align_exc)

        # ---- 3) 字段 key 归一化（golden 口径） ----
        _key_maps = {
            "Property": {"name": "特性名称", "type": "特性类型", "value": "特性值"},
            "Constrain": {"min": "最小值", "max": "最大值", "range": "阈值范围", "unit": "测量单位", "type": "约束类型"},
            "Clause": {"uniqueidentifier": "唯一标识符", "unique_identifier": "唯一标识符", "identifier": "唯一标识符",
                       "clause_number": "唯一标识符", "content_description": "内容描述", "clause_type": "条款类型",
                       "constraint_type": "约束类型"},
        }
        for _entity in raw["entities"]:
            _props = _entity.get("properties")
            if not isinstance(_props, dict) or not _props: continue
            _mapping = _key_maps.get(str(_entity.get("type")), {})
            _entity["properties"] = {_mapping.get(str(k).casefold(), k): v for k, v in _props.items()}
    canonical = assembler.canonical_raw(
        intermediate_model["objects"], intermediate_model["relations"]
    )
    graph = assembler.build_graph(intermediate_model["objects"], intermediate_model["relations"])
    triples = assembler.build_triples(graph)
    # §23 / AC-8：通用 Coverage Auditor（只发现 gap）
    coverage_gaps = CoverageAuditor().audit(
        plan,
        manager,
        request,
        canonical["entities"],
        canonical["relations"],
    )
    intermediate_model["coverage_gaps"] = [gap.to_dict() for gap in coverage_gaps]

    # Evidence 验证（L1/L2）与覆盖（AC-4）
    verdicts = verify_evidence(canonical, manager)
    for verdict in verdicts:
        if verdict.status == "fail":
            _append_warning(
                warnings,
                "EVIDENCE_L1_FAIL" if verdict.level == "L1" else "EVIDENCE_L2_FAIL",
                verdict.message,
                verdict.ref,
            )
    coverage = evidence_coverage(canonical)
    warning_counts = _warning_codes(warnings)
    all_entity_candidates = pool.entity_candidates()
    rejected_entity_count = sum(
        1 for entity in all_entity_candidates if entity.mapping_status != "accepted"
    )
    evidence_rejection_count = sum(
        warning_counts.get(code, 0)
        for code in ("ENTITY_EVIDENCE_REJECTED", "EVIDENCE_L1_FAIL", "EVIDENCE_L2_FAIL")
    )
    candidate_denominator = len(all_entity_candidates) + warning_counts.get(
        "ENTITY_EVIDENCE_REJECTED", 0
    )
    merge_count = sum(
        1 for old_id, new_id in identity_remap.items() if old_id != new_id
    ) + sum(
        1 for old_id, new_id in repair_identity_remap.items() if old_id != new_id
    )
    resolution_quality = {
        "candidate_rejection_rate": (
            (rejected_entity_count + warning_counts.get("ENTITY_EVIDENCE_REJECTED", 0))
            / candidate_denominator
            if candidate_denominator
            else 0.0
        ),
        "evidence_rejection_count": evidence_rejection_count,
        "identified_count": sum(
            1 for entity in all_entity_candidates if entity.lifecycle in {"IDENTIFIED", "RESOLVED"}
        ),
        "unresolved_count": sum(
            1 for entity in all_entity_candidates if entity.lifecycle == "UNRESOLVED"
        ),
        "quarantined_count": sum(
            1 for entity in all_entity_candidates if entity.lifecycle == "QUARANTINED"
        ),
        "entity_merge_count": merge_count,
        "entity_merge_rate": (
            merge_count / (len(all_entity_candidates) + merge_count)
            if all_entity_candidates or merge_count
            else 0.0
        ),
        "property_conflict_count": sum(
            len(entity.property_conflicts) for entity in all_entity_candidates
        ),
        "relation_endpoint_remap_count": pool.relation_endpoint_remap_count,
        "identity_admission": identity_admission,
    }

    verification_report = ExtractionVerifier().verify(
        plan,
        schema,
        manager,
        request,
        canonical["entities"],
        canonical["relations"],
        rejected_warnings=pool.unresolved_relation_rejections(),
        # Terminal verification is observational; no repair pass follows it.
        repairable_types=(),
    )
    logger.info(
        "[步骤][抽取] 校验输出: valid=%s hard_issues=%d repairable_issues=%d L1/L2覆盖=%s",
        verification_report.valid,
        len(verification_report.hard_issues),
        len(verification_report.repairable_issues),
        preview(coverage, 120),
    )
    if verification_report.hard_issues:
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
        _apply_global_confidence_gate(
            (
                raw.get("entities", []),
                raw.get("relations", []),
                graph.get("nodes", []),
                graph.get("relationships", []),
                triples,
                intermediate_model.get("objects", []),
                intermediate_model.get("relations", []),
            ),
            gate="final_verification_failed",
        )

    terminal_failures = {
        (str(attempt.get("pass") or ""), str(attempt.get("chunk") or ""))
        for attempt in attempts
        if attempt.get("terminal") is True
    }
    for chunk in manager:
        if chunk.chunk_id not in scannable_ids:
            continue
        run.mark_chunk_scan(
            chunk.chunk_id,
            "entity",
            completed=("entity", chunk.chunk_id) not in terminal_failures,
        )
        run.mark_chunk_scan(
            chunk.chunk_id,
            "relation",
            completed=(
                ("relation", chunk.chunk_id) not in terminal_failures
                and ("relation_evidence_verification", chunk.chunk_id) not in terminal_failures
            ),
        )

    run.absorb_attempts(attempts)
    repair_failure_code = ""
    repair_failure_message = ""
    if repair_result.report.repairable_issues:
        repair_failure_code = "REPAIR_INCOMPLETE"
        repair_failure_message = (
            f"targeted repair stopped at {repair_stop_reason}; "
            f"{len(repair_result.report.repairable_issues)} repairable issue(s) remain"
        )
    run.finish(
        accepted_entities=len(accepted_entities),
        accepted_relations=len(accepted_relations),
        stop_reason=repair_stop_reason,
        failure_code=repair_failure_code,
        failure_message=repair_failure_message,
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
    run_trace["repair"] = repair_summary
    run_trace["verification"] = {
        "valid": verification_report.valid,
        "coverage_complete": verification_report.coverage_complete,
        "hard_issue_count": len(verification_report.hard_issues),
        "repairable_issue_count": len(verification_report.repairable_issues),
        "unresolved_repairable_count": verification_report.unresolved_repairable_count,
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
    # 输出契约：实体 properties 必须覆盖本体声明的全部字段，缺失值用空值占位。
    raw["entities"] = _complete_ontology_properties(raw.get("entities") or [], plan)
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
        "repair": repair_summary,
        "mapping": mapping_summary,
        "profile": {
            "document_route": route.to_dict(),
            "router_enabled": use_router,
            "prompt_versions": dict(PROMPT_VERSIONS),
            "pipeline": "two_pass",
            "chunks": len(resolved_chunks),
            "scannable_chunks": len(scannable),
            "skipped_chunks": len(skipped_ids),
            "metadata_enrichment": metadata_counts,
            "chunk_contract": contract_counts,
            "chunk_routes": chunk_routes,
            "official_document_roles": official_roles,
            "chunk_source_ids": chunk_source_ids,
            "attachments_stored": attachment_count,
            "table_semantics": table_semantics,
            "chunk_route_details": {
                chunk_id: route_map[chunk_id].to_dict() for chunk_id in chunk_routes
            },
            "ontology_compiler_version": ONTOLOGY_COMPILER_VERSION,
            "entity_candidates": pool.entity_candidate_count,
            "relation_candidates": pool.relation_candidate_count,
            "accepted_entities": len(accepted_entities),
            "accepted_relations": len(accepted_relations),
            "numbered_hierarchy_relations": numbered_hierarchy_relations,
            "document_entities": document_entities,
            "standard_semantics": standard_semantics,
            "identity_enrichment": identity_enrichment,
            "property_enrichment": property_enrichment,
            "identity_resolution": {
                **identity_finalization,
                "merged": sum(1 for old_id, new_id in identity_remap.items() if old_id != new_id),
                "id_remap": identity_remap,
                "frozen_entity_fingerprint": frozen_entity_fingerprint,
                "repair": {
                    **repair_identity_finalization,
                    "merged": sum(
                        1 for old_id, new_id in repair_identity_remap.items() if old_id != new_id
                    ),
                    "id_remap": repair_identity_remap,
                },
            },
            "resolution_quality": resolution_quality,
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
            "repair": repair_summary,
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
