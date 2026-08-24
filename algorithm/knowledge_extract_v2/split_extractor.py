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

LLM 通过注入的 ``llm(prompt) -> str`` callable 调用，本模块不依赖 openai；
live 环境可用 ``llm_client.create_llm_client()`` 生成适配器。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .chunk_context import ChunkContextManager, ExtractionChunk, ExtractionRequest
from .composite import CompositeElementHandler
from .coverage import CoverageAuditor
from .evidence import Evidence, bind_evidence, evidence_coverage, locate_quote, verify_evidence
from .graph_assembler import GraphAssembler
from .graph_utils import (
    canonical_name,
    merge_node_properties,
)
from .mapping_scorer import MappingScorer
from .official_document_strategy import OfficialDocumentExtractionStrategy  # noqa: F401 - 注册到 STRATEGY_REGISTRY
from .ontology_compiler import ExtractionPlan, compile_plan
from .ontology_schema import OntologySchema, _clean_relation_identifier
from .scheduler import get_llm_scheduler
from .standard_strategy import get_strategy


LLM_CALLABLE = Callable[[str], str]


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


def extract_json_from_response(response: str) -> dict[str, Any]:
    """从 LLM 响应中提取第一个完整 JSON 对象。"""
    response = (response or "").strip()
    if not response:
        return {}
    if response.startswith("```"):
        lines = response.split("\n")
        response = "\n".join(lines[1:]) if len(lines) > 1 else response
        if response.endswith("```"):
            response = response[:-3].rstrip()
    start = response.find("{")
    end = response.rfind("}")
    if start >= 0 and end > start:
        response = response[start : end + 1]
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass
class EntityCandidate:
    """§14.1：实体候选。"""

    candidate_id: str
    proposed_type: str
    canonical_type: str | None
    name: str
    properties: dict[str, Any]
    evidence: list[Evidence]
    source: str
    extraction_pass: str = "entity"
    mapping_score: float | None = None
    mapping_status: str = "accepted"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.canonical_type or self.proposed_type,
            "name": self.name,
            "properties": dict(self.properties),
            "candidate_id": self.candidate_id,
        }
        if self.evidence:
            payload["evidence"] = self.evidence[0].to_dict()
        return payload

    def object_model(self, features: dict[str, Any]) -> dict[str, Any]:
        """§25 O1 object 形态。"""
        return {
            "id": self.candidate_id,
            "type": self.canonical_type or self.proposed_type,
            "identity": self.name,
            "properties": dict(self.properties),
            "features": features,
            "evidence": [evidence.to_dict() for evidence in self.evidence],
            "mapping": {"score": self.mapping_score, "status": self.mapping_status},
        }


@dataclass
class RelationCandidate:
    """§14.2：关系候选（端点引用实体候选 id）。"""

    candidate_id: str
    from_candidate_id: str
    to_candidate_id: str
    proposed_relation: str
    canonical_relation: str | None
    evidence: list[Evidence]
    from_name: str = ""
    to_name: str = ""
    from_type: str = ""
    to_type: str = ""
    mapping_score: float | None = None
    mapping_status: str = "accepted"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "from_id": self.from_candidate_id,
            "from_type": self.from_type,
            "from_name": self.from_name,
            "rel_type": self.canonical_relation or self.proposed_relation,
            "to_type": self.to_type,
            "to_name": self.to_name,
            "to_id": self.to_candidate_id,
        }
        if self.evidence:
            payload["evidence"] = self.evidence[0].to_dict()
        return payload


def _cardinality_is_single(cardinality: str) -> bool:
    normalized = (cardinality or "").strip()
    return normalized in {"1", "0..1", "1..1"}


class CandidatePool:
    """实体解析/去重（§20）与关系候选校验/去重（§19、AC-7）。"""

    def __init__(self, schema: OntologySchema, *, allowed_types: Iterable[str] | None = None):
        self.schema = schema
        self.allowed_types = set(allowed_types or ()) or {
            ontology_class.identifier for ontology_class in schema.classes
        }
        self._entities: list[EntityCandidate] = []
        self._entity_by_id: dict[str, EntityCandidate] = {}
        self._entity_index: dict[tuple[str, str], EntityCandidate] = {}
        self._relations: list[RelationCandidate] = []
        self._relation_by_key: dict[tuple[str, str, str], RelationCandidate] = {}
        self._next_entity_id = 0
        self._next_relation_id = 0

    def add_entity(
        self,
        candidate: EntityCandidate,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> EntityCandidate | None:
        """添加实体候选；类型一致 + 规范化 identity 一致时合并（§20）。"""
        ontology_class = self.schema.resolve_class(candidate.canonical_type or candidate.proposed_type)
        if ontology_class is None:
            _append_warning(
                warnings,
                "UNKNOWN_ENTITY_TYPE",
                f"实体类型 {candidate.proposed_type!r} 不在本体中",
                "entities",
                candidate.proposed_type,
            )
            return None
        if ontology_class.identifier not in self.allowed_types:
            _append_warning(
                warnings,
                "ENTITY_OUT_OF_SCOPE",
                f"实体类型 {ontology_class.identifier} 不在目标类型范围内",
                "entities",
                ontology_class.identifier,
            )
            return None

        candidate.canonical_type = ontology_class.identifier
        key = (ontology_class.identifier, canonical_name(candidate.name))
        existing = self._entity_index.get(key)
        if existing is None:
            candidate.candidate_id = f"entity-{self._next_entity_id}"
            self._next_entity_id += 1
            self._entities.append(candidate)
            self._entity_by_id[candidate.candidate_id] = candidate
            self._entity_index[key] = candidate
            return candidate

        # §20 merge：类型与规范化 identity 已一致，合并属性与证据（不凭 LLM 主观判断）。
        existing.properties = merge_node_properties(existing.properties, candidate.properties)
        for evidence in candidate.evidence:
            if not any(evidence.span_id == item.span_id for item in existing.evidence):
                existing.evidence.append(evidence)
        existing.warnings.extend(candidate.warnings)
        return existing

    def resolve_entity(self, candidate_id: str) -> EntityCandidate | None:
        return self._entity_by_id.get(candidate_id)

    def find_entity(self, type_id: str, name: str) -> EntityCandidate | None:
        """按稳定类型 + 名称（canonical）查找已入池实体。"""
        ontology_class = self.schema.resolve_class(type_id)
        if ontology_class is None:
            return None
        return self._entity_index.get((ontology_class.identifier, canonical_name(name)))

    def accepted_entities(self) -> list[EntityCandidate]:
        return [entity for entity in self._entities if entity.mapping_status == "accepted"]

    def entity_catalog(self) -> list[dict[str, Any]]:
        """§15.2：供第二次 LLM pass 引用的已确认实体目录。"""
        return [
            {
                "id": entity.candidate_id,
                "type": entity.canonical_type,
                "name": entity.name,
                "properties": {key: value for key, value in entity.properties.items() if key != "name"},
            }
            for entity in self.accepted_entities()
        ]

    def add_relation(
        self,
        candidate: RelationCandidate,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> RelationCandidate | None:
        """校验关系端点存在、签名匹配、cardinality 去重；失败则拒绝（AC-7）。"""
        from_entity = self.resolve_entity(candidate.from_candidate_id)
        to_entity = self.resolve_entity(candidate.to_candidate_id)
        if from_entity is None or to_entity is None:
            candidate.mapping_status = "rejected"
            _append_warning(
                warnings,
                "UNKNOWN_RELATION_ENDPOINT_ID",
                (
                    f"关系 {candidate.proposed_relation!r} 引用不存在的候选端点: "
                    f"{candidate.from_candidate_id!r} -> {candidate.to_candidate_id!r}"
                ),
                "relations",
                [candidate.from_candidate_id, candidate.to_candidate_id],
            )
            return None

        proposed_relation = _clean_relation_identifier(candidate.proposed_relation)
        if proposed_relation:
            candidate.proposed_relation = proposed_relation
        matching = self.schema.resolve_relation(proposed_relation)
        if not matching:
            candidate.mapping_status = "rejected"
            _append_warning(
                warnings,
                "UNKNOWN_RELATION_TYPE",
                f"关系类型 {candidate.proposed_relation!r} 未在本体中定义",
                "relations",
                candidate.proposed_relation,
            )
            return None
        signature = next(
            (
                relation
                for relation in matching
                if self.schema.is_assignable(from_entity.canonical_type or "", relation.source)
                and self.schema.is_assignable(to_entity.canonical_type or "", relation.target)
            ),
            None,
        )
        if signature is None:
            candidate.mapping_status = "rejected"
            _append_warning(
                warnings,
                "RELATION_ENDPOINT_TYPE_MISMATCH",
                (
                    f"关系 {candidate.proposed_relation!r} 的端点 "
                    f"{from_entity.canonical_type} -> {to_entity.canonical_type} 不符合本体签名"
                ),
                "relations",
            )
            return None

        candidate.canonical_relation = signature.identifier
        candidate.from_name = from_entity.name
        candidate.to_name = to_entity.name
        candidate.from_type = from_entity.canonical_type or ""
        candidate.to_type = to_entity.canonical_type or ""

        key = (candidate.from_candidate_id, candidate.canonical_relation, candidate.to_candidate_id)
        existing = self._relation_by_key.get(key)
        if existing is not None:
            for evidence in candidate.evidence:
                if not any(evidence.span_id == item.span_id for item in existing.evidence):
                    existing.evidence.append(evidence)
            return existing

        # §19：cardinality 作为结构 validator；max=1 的端出现多个不同对端时告警。
        if _cardinality_is_single(signature.target_cardinality):
            for other in self._relations:
                if (
                    other.from_candidate_id == candidate.from_candidate_id
                    and other.canonical_relation == candidate.canonical_relation
                    and other.to_candidate_id != candidate.to_candidate_id
                ):
                    _append_warning(
                        warnings,
                        "RELATION_CARDINALITY_EXCEEDED",
                        (
                            f"关系 {candidate.canonical_relation} 目标端基数 "
                            f"{signature.target_cardinality} 为单值，但出现多个目标端点"
                        ),
                        "relations",
                        candidate.canonical_relation,
                    )
                    candidate.warnings.append("RELATION_CARDINALITY_EXCEEDED")
        if _cardinality_is_single(signature.source_cardinality):
            for other in self._relations:
                if (
                    other.to_candidate_id == candidate.to_candidate_id
                    and other.canonical_relation == candidate.canonical_relation
                    and other.from_candidate_id != candidate.from_candidate_id
                ):
                    _append_warning(
                        warnings,
                        "RELATION_CARDINALITY_EXCEEDED",
                        (
                            f"关系 {candidate.canonical_relation} 源端基数 "
                            f"{signature.source_cardinality} 为单值，但出现多个源端点"
                        ),
                        "relations",
                        candidate.canonical_relation,
                    )
                    candidate.warnings.append("RELATION_CARDINALITY_EXCEEDED")

        candidate.candidate_id = f"relation-{self._next_relation_id}"
        self._next_relation_id += 1
        self._relations.append(candidate)
        self._relation_by_key[key] = candidate
        return candidate

    def accepted_relations(self) -> list[RelationCandidate]:
        return [relation for relation in self._relations if relation.mapping_status == "accepted"]


def _feature_lines(plan: ExtractionPlan) -> str:
    lines: list[str] = []
    for type_id in sorted(plan.allowed_types):
        specs = plan.feature_specs.get(type_id, ())
        intrinsic = [spec for spec in specs if spec.kind == "intrinsic"]
        if not intrinsic:
            continue
        lines.append(f"- `{type_id}`：")
        for spec in intrinsic:
            lines.append(f"    - `{spec.identifier}` ({spec.datatype})")
    return "\n".join(lines) if lines else "（无特性声明）"


def _signature_lines(plan: ExtractionPlan) -> str:
    lines = sorted(
        {
            f"`{spec.source}` -[`{spec.relation_id}`]-> `{spec.target}`"
            for specs in plan.relation_specs.values()
            for spec in specs
            if spec.source in plan.allowed_types and spec.target in plan.allowed_types
        }
    )
    return "\n".join(lines) if lines else "（目标类型之间没有关系签名）"


def _catalog_lines(catalog: list[dict[str, Any]]) -> str:
    if not catalog:
        return "（无已确认实体）"
    lines = ["| id | type | name | 关键属性 |", "| --- | --- | --- | --- |"]
    for entry in catalog:
        properties = ", ".join(f"{key}={value}" for key, value in entry["properties"].items())
        lines.append(f"| `{entry['id']}` | `{entry['type']}` | {entry['name']} | {properties or '-'} |")
    return "\n".join(lines)


def _catalog_text(plan: ExtractionPlan) -> str:
    """从 ExtractionPlan 生成本体标识目录（与 schema.prompt_catalog 同构）。"""
    lines: list[str] = []
    for type_id in sorted(plan.allowed_types):
        spec = plan.class_specs.get(type_id)
        if spec is None:
            continue
        line = f"- `{spec.display_name}` -> type `{type_id}`"
        if spec.identity_properties:
            line += f"；identity 优先级：{', '.join(spec.identity_properties)}"
        lines.append(line)
        for prop in spec.properties:
            lines.append(f"  - 属性 `{prop.display_name}` -> `{prop.identifier}` ({prop.data_type})")
    return "\n".join(lines) if lines else "目标类型未匹配到本体类。"


def build_entity_prompt(
    plan: ExtractionPlan,
    chunk_text: str,
    *,
    local_context: str = "",
    document_type: str = "auto",
    known_entities: list[dict[str, Any]] | None = None,
    hints: str = "",
) -> str:
    """§15.1：第一次 LLM pass——实体 + 特性候选提议（禁止输出关系）。"""
    targets = (
        "、".join(f"`{identifier}`" for identifier in sorted(plan.allowed_types))
        if plan.allowed_types
        else "本体中全部已声明类型"
    )
    catalog = _catalog_text(plan) if plan.class_specs else "未解析到类声明。"
    known_text = _catalog_lines(known_entities or []) if known_entities else "（无）"
    return f"""你是知识图谱抽取专家。本阶段只做**实体候选提议**，禁止输出关系。

## 文档画像
{document_type}

## 本次目标类型（已含子类展开，AC-2 范围纪律）
{targets}

## 本次允许的本体标识目录（唯一 schema）
{catalog}

## 每类特征（FeatureSpec，intrinsic）
{_feature_lines(plan)}

## 已确认实体（确定性抽取结果，请勿重复输出；可用其 identity 对齐）
{known_text}

{hints}

## 待抽取文本（当前 chunk）
{chunk_text}

## 局部上下文（仅参考，禁止据此编造当前 chunk 没有的事实）
{local_context or "（无）"}

## 输出要求
- 只输出一个 JSON 对象：{{"entities": [...]}}，禁止输出 relations 或其他顶层键。
- 每个实体必须：
  - `type` 只能使用目录中的稳定类型标识；
  - `name` 按 @identity 优先级填写，同一实体复用同一 identity/name；
  - `properties` 键只能使用目录中的稳定属性标识，值符合 datatype；
  - 必须携带 `evidence: {{"quote": "原文片段"}}`，quote 必须是待抽取文本中的原文字符串；
  - 只抽取文本明确表达的事实，不补写常识、不猜测缺失值。
- 没有结果时输出 {{"entities": []}}。"""


def build_relation_prompt(
    plan: ExtractionPlan,
    catalog: list[dict[str, Any]],
    chunk_text: str,
    *,
    local_context: str = "",
    document_type: str = "auto",
) -> str:
    """§15.2：第二次 LLM pass——关系候选提议（端点引用已确认实体目录 id）。"""
    return f"""你是知识图谱抽取专家。本阶段只做**关系候选提议**，禁止输出实体。

## 文档画像
{document_type}

## 已确认实体目录（关系端点只能引用这里的 id，禁止编造）
{_catalog_lines(catalog)}

## 本次允许的关系签名（方向和端点类型必须匹配）
{_signature_lines(plan)}

## 待抽取文本（当前 chunk）
{chunk_text}

## 局部上下文（仅参考）
{local_context or "（无）"}

## 输出要求
- 只输出一个 JSON 对象：{{"relations": [...]}}。
- 每条 relation 必须：
  - `from_id` / `to_id` 引用目录中的 id；
  - `rel_type` 只能使用列出的关系名，方向与端点类型必须匹配签名；
  - 必须携带 `evidence: {{"quote": "原文片段"}}`，quote 必须来自待抽取文本；
  - 不要为输出关系而编造实体。
- 没有结果时输出 {{"relations": []}}。"""


def _call_llm(
    llm: LLM_CALLABLE,
    prompt: str,
    *,
    use_scheduler: bool,
    attempts: list[dict[str, Any]],
    pass_name: str,
    chunk_id: str,
) -> str:
    try:
        if use_scheduler:
            with get_llm_scheduler().request_slot():
                response = llm(prompt)
        else:
            response = llm(prompt)
    except Exception as exc:  # noqa: BLE001 - 请求失败归因，不中断整体抽取
        attempts.append(
            {
                "attempt": 1,
                "type": "request",
                "pass": pass_name,
                "chunk": chunk_id,
                "message": str(exc),
            }
        )
        return ""
    return response or ""


def _normalize_entities(
    parsed: dict[str, Any],
    schema: OntologySchema,
    target_types: Iterable[str] | None,
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sanitized, validation_warnings = schema.validate_extraction(
        parsed,
        target_types=target_types,
        mode="strict",
    )
    warnings.extend(validation_warnings)
    normalized = schema.normalize_extraction(sanitized)
    return normalized.get("entities", [])


def _bind_entity_evidence(
    entities: list[dict[str, Any]],
    manager: ChunkContextManager,
) -> list[dict[str, Any]]:
    bound = bind_evidence({"entities": entities, "relations": []}, manager)
    return bound.get("entities", [])


def _bind_relation_evidence(
    raw_relation: dict[str, Any],
    candidate: RelationCandidate,
    manager: ChunkContextManager,
) -> Evidence | None:
    quote = (raw_relation.get("evidence") or {}).get("quote")
    if quote:
        located = locate_quote(manager, quote)
        if located is not None:
            return located
    for name in (candidate.from_name, candidate.to_name):
        located = locate_quote(manager, name)
        if located is not None:
            return located
    return None


def _entity_pass(
    plan: ExtractionPlan,
    schema: OntologySchema,
    manager: ChunkContextManager,
    request: ExtractionRequest,
    llm: LLM_CALLABLE,
    *,
    max_workers: int,
    use_scheduler: bool,
    attempts: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    local_previous: int,
    local_next: int,
    same_section: bool,
    known_entities: list[dict[str, Any]] | None = None,
    hints: str = "",
) -> list[tuple[str, list[dict[str, Any]]]]:
    chunks = request.extraction_chunks()

    def run(chunk: ExtractionChunk) -> tuple[str, list[dict[str, Any]]]:
        neighbors = [
            item
            for item in manager.local_context(
                chunk.chunk_id,
                previous=local_previous,
                next=local_next,
                same_section=same_section,
            )
            if item.chunk_id != chunk.chunk_id
        ]
        local_context = "\n".join(item.text for item in neighbors)
        prompt = build_entity_prompt(
            plan,
            chunk.text,
            local_context=local_context,
            document_type=request.document_type,
            known_entities=known_entities,
            hints=hints,
        )
        response = _call_llm(
            llm,
            prompt,
            use_scheduler=use_scheduler,
            attempts=attempts,
            pass_name="entity",
            chunk_id=chunk.chunk_id,
        )
        parsed = extract_json_from_response(response)
        if not parsed:
            attempts.append(
                {
                    "attempt": 1,
                    "type": "json",
                    "pass": "entity",
                    "chunk": chunk.chunk_id,
                    "message": "entity pass JSON 解析失败",
                }
            )
            return chunk.chunk_id, []
        entities = _normalize_entities(parsed, schema, request.target_types, warnings)
        entities = _bind_entity_evidence(entities, manager)
        return chunk.chunk_id, entities

    worker_count = max(1, max_workers)
    if worker_count == 1:
        return [run(chunk) for chunk in chunks]
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ke-entity") as executor:
        return list(executor.map(run, chunks))


def _relation_pass(
    plan: ExtractionPlan,
    manager: ChunkContextManager,
    request: ExtractionRequest,
    llm: LLM_CALLABLE,
    pool: CandidatePool,
    *,
    max_workers: int,
    use_scheduler: bool,
    attempts: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    local_previous: int,
    local_next: int,
    same_section: bool,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """第二次 pass：worker 只返回原始关系候选，入池统一在收集后按 chunk 顺序进行。"""
    chunks = request.extraction_chunks()
    catalog = pool.entity_catalog()

    def run(chunk: ExtractionChunk) -> tuple[str, list[dict[str, Any]]]:
        neighbors = [
            item
            for item in manager.local_context(
                chunk.chunk_id,
                previous=local_previous,
                next=local_next,
                same_section=same_section,
            )
            if item.chunk_id != chunk.chunk_id
        ]
        local_context = "\n".join(item.text for item in neighbors)
        prompt = build_relation_prompt(
            plan,
            catalog,
            chunk.text,
            local_context=local_context,
            document_type=request.document_type,
        )
        response = _call_llm(
            llm,
            prompt,
            use_scheduler=use_scheduler,
            attempts=attempts,
            pass_name="relation",
            chunk_id=chunk.chunk_id,
        )
        parsed = extract_json_from_response(response)
        if not parsed:
            attempts.append(
                {
                    "attempt": 1,
                    "type": "json",
                    "pass": "relation",
                    "chunk": chunk.chunk_id,
                    "message": "relation pass JSON 解析失败",
                }
            )
            return chunk.chunk_id, []

        accepted: list[dict[str, Any]] = []
        for index, raw_relation in enumerate(parsed.get("relations", [])):
            if not isinstance(raw_relation, dict):
                _append_warning(warnings, "INVALID_RELATION", "关系必须是对象", f"relations[{index}]")
                continue
            accepted.append(raw_relation)
        return chunk.chunk_id, accepted

    worker_count = max(1, max_workers)
    if worker_count == 1:
        return [run(chunk) for chunk in chunks]
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ke-relation") as executor:
        return list(executor.map(run, chunks))


def _object_features(entity: EntityCandidate, feature_specs: tuple[Any, ...]) -> dict[str, Any]:
    intrinsic: dict[str, Any] = {}
    relation_features: list[str] = []
    for spec in feature_specs:
        if spec.kind == "intrinsic" and spec.identifier in entity.properties:
            intrinsic[spec.identifier] = entity.properties[spec.identifier]
        elif spec.kind == "relation":
            relation_features.append(spec.relation_signature or spec.identifier)
    return {"intrinsic": intrinsic, "relation": relation_features, "context": {}}


def build_intermediate_model(
    pool: CandidatePool,
    plan: ExtractionPlan,
    *,
    run_id: str = "",
) -> dict[str, Any]:
    """§25：O1 中间对象模型（只含 accepted candidates）。"""
    objects: list[dict[str, Any]] = []
    for entity in pool.accepted_entities():
        features = _object_features(entity, plan.feature_specs.get(entity.canonical_type or "", ()))
        objects.append(entity.object_model(features))
    relations = [
        {
            "id": relation.candidate_id,
            "type": relation.canonical_relation,
            "from": relation.from_candidate_id,
            "to": relation.to_candidate_id,
            "evidence": [evidence.to_dict() for evidence in relation.evidence],
            "mapping": {"score": relation.mapping_score, "status": relation.mapping_status},
        }
        for relation in pool.accepted_relations()
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


def _build_graph(
    entities: list[EntityCandidate],
    relations: list[RelationCandidate],
) -> dict[str, list[dict[str, Any]]]:
    """§26/§27：O2 graph（委托 GraphAssembler，UID 只由稳定类型 + identity 生成）。"""
    objects = [
        {
            "id": entity.candidate_id,
            "type": entity.canonical_type or entity.proposed_type,
            "identity": entity.name,
            "properties": entity.properties,
        }
        for entity in entities
    ]
    relation_payloads = [
        {
            "id": relation.candidate_id,
            "type": relation.canonical_relation or relation.proposed_relation,
            "from": relation.from_candidate_id,
            "to": relation.to_candidate_id,
        }
        for relation in relations
    ]
    return GraphAssembler().build_graph(objects, relation_payloads)


def _triples(graph: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return GraphAssembler().build_triples(graph)


def _empty_result(
    plan: ExtractionPlan,
    *,
    warnings: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    run_id: str,
) -> dict[str, Any]:
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
        "profile": {"document_type": "auto", "pipeline": "two_pass", "chunks": 0},
        "error_code": None,
    }


def extract_split(
    ontology: str,
    text: str | None = None,
    chunks: Iterable[ExtractionChunk] | None = None,
    *,
    target_types: Iterable[str] | None = None,
    include_subclasses: bool = True,
    document_type: str = "auto",
    validation_mode: str = "strict",
    llm: LLM_CALLABLE,
    mode: str = "strict",
    unknown_content_policy: str = "ignore",
    local_previous: int = 1,
    local_next: int = 1,
    same_section: bool = True,
    max_workers: int = 1,
    use_scheduler: bool = False,
    run_id: str = "",
    strategy: str | None = None,
    mapping_weights: dict[str, float] | None = None,
    mapping_accepted_threshold: float = 0.85,
    mapping_low_confidence_threshold: float = 0.70,
    apply_mapping_threshold: bool = False,
) -> dict[str, Any]:
    """§37 Step 4：entity candidate -> resolve -> relation candidate 两段式抽取。

    ``llm`` 是 ``prompt -> response_text`` 的 callable；返回契约兼容现有
    extractor（raw / graph / warnings / attempts / profile / error_code），
    并附带 triples / intermediate_model / evidence_coverage。
    """
    plan = compile_plan(
        ontology,
        target_types=target_types,
        include_subclasses=include_subclasses,
        mode=mode,
        unknown_content_policy=unknown_content_policy,
    )
    schema = OntologySchema.parse(ontology or "")
    request = ExtractionRequest(
        ontology=ontology,
        text=text,
        chunks=list(chunks) if chunks is not None else None,
        target_types=list(target_types) if target_types is not None else None,
        include_subclasses=include_subclasses,
        document_type=document_type,
        validation_mode=validation_mode,
    )
    manager = ChunkContextManager.from_request(request)
    warnings: list[dict[str, Any]] = [warning.to_dict() for warning in plan.schema_warnings]
    attempts: list[dict[str, Any]] = []
    resolved_run_id = run_id or f"ke-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    resolved_strategy = get_strategy(request.document_type, strategy)
    strategy_hints = (
        resolved_strategy.prompt_hints() if hasattr(resolved_strategy, "prompt_hints") else ""
    )

    if not request.extraction_chunks():
        _append_warning(warnings, "EMPTY_INPUT", "请求没有 text 或 chunks", "text/chunks")
        return _empty_result(plan, warnings=warnings, attempts=attempts, run_id=resolved_run_id)

    # 策略确定性抽取（§11.1）：先入池并注入实体 pass，防止 LLM 重复抽取
    pool = CandidatePool(schema, allowed_types=plan.allowed_types)
    deterministic = resolved_strategy.deterministic_extraction(plan, manager, request)
    deterministic_entities = _bind_entity_evidence(
        deterministic.get("entities", []),
        manager,
    )
    for entity in deterministic_entities:
        evidence = [item for item in [Evidence.from_dict(entity.get("evidence"))] if item is not None]
        pool.add_entity(
            EntityCandidate(
                candidate_id="",
                proposed_type=entity.get("type", ""),
                canonical_type=entity.get("type"),
                name=entity.get("name", ""),
                properties=entity.get("properties") or {},
                evidence=evidence,
                source="deterministic",
            ),
            warnings=warnings,
        )
    known_entities = pool.entity_catalog()

    # Pass 1：实体候选（LLM）
    entity_results = _entity_pass(
        plan,
        schema,
        manager,
        request,
        llm,
        max_workers=max_workers,
        use_scheduler=use_scheduler,
        attempts=attempts,
        warnings=warnings,
        local_previous=local_previous,
        local_next=local_next,
        same_section=same_section,
        known_entities=known_entities,
        hints=strategy_hints,
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

    # 策略确定性关系（§11.1：hasStructuralElement / hasSubClause）
    for relation in deterministic.get("relations", []):
        from_entity = pool.find_entity(relation.get("from_type", ""), relation.get("from_name", ""))
        to_entity = pool.find_entity(relation.get("to_type", ""), relation.get("to_name", ""))
        if from_entity is None or to_entity is None:
            continue
        candidate = RelationCandidate(
            candidate_id="",
            from_candidate_id=from_entity.candidate_id,
            to_candidate_id=to_entity.candidate_id,
            proposed_relation=relation.get("rel_type", ""),
            canonical_relation=None,
            evidence=[],
        )
        resolved = pool.add_relation(candidate, warnings=warnings)
        if resolved is None:
            continue
        evidence = _bind_relation_evidence(relation, resolved, manager)
        if evidence is not None:
            resolved.evidence = [evidence]

    # Pass 2：关系候选（LLM，基于 accepted/resolved 实体目录）
    relation_results = _relation_pass(
        plan,
        manager,
        request,
        llm,
        pool,
        max_workers=max_workers,
        use_scheduler=use_scheduler,
        attempts=attempts,
        warnings=warnings,
        local_previous=local_previous,
        local_next=local_next,
        same_section=same_section,
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
            evidence = _bind_relation_evidence(raw_relation, resolved, manager)
            if evidence is not None:
                resolved.evidence = [evidence]

    # §21/AC-6：复合元素确定性组装与校验（补建 hasSubRule 成员关系）
    composite_handler = CompositeElementHandler.from_plan(plan)
    composite_issues = composite_handler.assemble(pool, plan, manager, warnings=warnings)

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
    # §23 / AC-8：通用 Coverage Auditor（只发现 gap）+ 策略专项审计
    coverage_gaps = CoverageAuditor().audit(
        plan,
        manager,
        request,
        raw["entities"],
        raw["relations"],
    )
    coverage_gaps.extend(resolved_strategy.coverage_audit(
        plan,
        manager,
        request,
        raw["entities"],
        raw["relations"],
    ))
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

    error_code = None
    if any(attempt.get("type") == "request" for attempt in attempts):
        error_code = "LLM_REQUEST_FAILED"
    elif any(attempt.get("type") == "json" for attempt in attempts):
        error_code = "LLM_JSON_PARSE_FAILED"

    return {
        "raw": raw,
        "graph": graph,
        "triples": triples,
        "intermediate_model": intermediate_model,
        "warnings": warnings,
        "attempts": attempts,
        "evidence_coverage": coverage,
        "coverage_gaps": [gap.to_dict() for gap in coverage_gaps],
        "mapping": mapping_summary,
        "profile": {
            "document_type": request.document_type,
            "pipeline": "two_pass",
            "chunks": len(request.extraction_chunks()),
            "entity_candidates": len(pool._entities),
            "relation_candidates": len(pool._relations),
            "accepted_entities": len(accepted_entities),
            "accepted_relations": len(accepted_relations),
            "strategy": resolved_strategy.document_type,
            "composite_auto_memberships": sum(
                1 for issue in composite_issues if issue.get("code") == "AUTO_MEMBERSHIP_ESTABLISHED"
            ),
            "mapping_mode": "enforce" if apply_mapping_threshold else "annotate",
        },
        "error_code": error_code,
    }


__all__ = [
    "CandidatePool",
    "EntityCandidate",
    "RelationCandidate",
    "build_entity_prompt",
    "build_intermediate_model",
    "build_relation_prompt",
    "extract_json_from_response",
    "extract_split",
]
