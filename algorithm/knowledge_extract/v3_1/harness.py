"""Bounded semantic workflow: discover, decide, enrich, relate, repair.

Code controls references, schemas, evidence locations and budgets. The models
decide meaning, identity equivalence, type ambiguity and factual support.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from copy import deepcopy
import hashlib
import json
import os
import re
import time
from typing import Any

from ..v2.chunk_context import ChunkContextManager, ExtractionChunk
from ..v2.config import get_llm_config
from ..v2.graph_assembler import GraphAssembler
from ..v2.graph_utils import entity_uid
from ..v2.ontology_schema import OntologySchema
from ..v2.scheduler import get_llm_scheduler
from .citations import render_sources
from .controller import CallFailure, Controller
from .decisions import ClefDecider, DecisionTask
from .evidence import locate, source_ids, typed, unique
from .llm_client import create_client, thinking_disabled
from .schema_context import build_schema_context


SUPPORT = {"supported": "原文支持完整候选，包括类型、属性、方向、条件和否定。",
           "contradicted": "原文明示反驳该候选，或候选混淆了对象/方向/条件。",
           "insufficient": "提供的原文不足以判定，候选陈述不是证据；共现不等于关系。"}
IDENTITY = {"same_entity": "原文支持这些名称指同一实体，身份和适用条件一致。",
            "different_entity": "原文支持是不同实体或不同事项。",
            "insufficient": "没有足够原文证明同一或不同。"}


def _same_json(left: Any, right: Any) -> bool:
    """JSON value equality must not conflate Boolean and numeric assertions."""
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(right, sort_keys=True, ensure_ascii=False)


def _positive_number(value: float | None, env: str, default: float) -> float:
    resolved = float(os.getenv(env, str(default)) if value is None else value)
    if not 0 < resolved < float("inf"):
        raise ValueError(f"{env} must be a finite positive number")
    return resolved


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _boolean(value: bool | None, env: str, default: bool) -> bool:
    if value is not None:
        return bool(value)
    raw = os.getenv(env, "1" if default else "0").strip().lower()
    if raw not in _TRUE | _FALSE:
        raise ValueError(f"{env} must be boolean")
    return raw in _TRUE


def _unit_interval(value: float | None, env: str, default: float) -> float:
    resolved = float(os.getenv(env, str(default)) if value is None else value)
    if not 0 <= resolved <= 1:
        raise ValueError(f"{env} must be within [0, 1]")
    return resolved


#: 已发布图谱本身是否可信，只由这些"图谱完整性"问题决定。它们描述的对象
#: 已经进入图谱（条件未连接、重数冲突、属性冲突、身份未重键），因此可能
#: 让已发布事实自相矛盾。候选被拒、任务未被裁决、调用超时、模型声明"某条
#: 事实无法表达"都只说明召回/执行有缺口，记入 coverage_gaps 而不否决整张图
#: （与 v2 的 coverage 语义一致：覆盖缺口是 warning，不是 hard error）。
INTEGRITY_ISSUE_CODES = frozenset({
    "CONDITION_NOT_LINKED",
    "CARDINALITY_CONFLICT",
    "PROPERTY_CONFLICT",
    "REPAIR_CONTEXT_NOT_READ",
    "IDENTITY_CHANGE_REQUIRES_REPAIR",
})

#: 模型显式声明的未完成事实：不改图谱可信度，但要在告警里保持 error 级别，
#: 不能被当作普通提示淹没。
DECLARED_GAP_ISSUE_CODES = frozenset({"SEMANTIC_SCOPE_UNRESOLVED"})

#: 执行类问题：模型对某个切片/阶段没有给出可用响应（超时、请求失败、响应
#: 不完整、调用预算耗尽）。它们说明"已知漏抽"，记入 failed_chunks，让调用方
#: 知道哪一段原文没有被真正读完。
EXECUTION_ISSUE_CODES = frozenset({
    "LLM_TIMEOUT", "LLM_REQUEST_FAILED", "LLM_JSON_PARSE_FAILED",
    "LLM_RESPONSE_INCOMPLETE", "LLM_CALL_BUDGET_EXHAUSTED", "DOCUMENT_BUDGET_EXHAUSTED",
})

#: 候选级问题（候选被拒、从未进入图谱）。它们可以随后由其它切片或修复轮以
#: 合法证据重新提出，因此最终结果必须与已发布图谱对账后再判断。字段级问题
#: （INVALID_PROPERTY_SCHEMA 等）不在此列：实体被发布不代表字段问题已修复，
#: 它们只由实际字段修复（resolve_property_repairs）撤回。
CANDIDATE_RELATION_ISSUE_CODES = frozenset({
    "INVALID_RELATION", "INVALID_RELATION_REFERENCE_OR_EVIDENCE", "INVALID_RELATION_FACT",
    "INVALID_CONDITION_REFERENCES",
})
CANDIDATE_ENTITY_ISSUE_CODES = frozenset({
    "INVALID_ENTITY", "INVALID_ENTITY_TYPE", "INVALID_ENTITY_EVIDENCE_OR_NAME",
    "ENTITY_TYPE_UNRESOLVED",
})


def decision_thresholds(*, support: float | None = None, alternate: float | None = None,
                        margin: float | None = None) -> dict[str, float]:
    """Clef 分流阈值：支持判定与其它判定分开，且都可按部署校准。"""
    return {
        "supported": _unit_interval(support, "KG_EXTRACT_V31_SUPPORT_THRESHOLD", .90),
        "alternate": _unit_interval(alternate, "KG_EXTRACT_V31_ALTERNATE_THRESHOLD", .95),
        "margin": _unit_interval(margin, "KG_EXTRACT_V31_DECISION_MARGIN", .15),
    }


class Harness:
    def __init__(self, *, ontology: str, chunks: list[ExtractionChunk], schema: OntologySchema,
                 allowed: set[str], controller: Controller, decider: Any, mode: str,
                 thresholds: dict[str, float] | None = None, strict_coverage: bool = False,
                 relation_retries: int = 1, decision_group: int = 16):
        self.ontology, self.chunks, self.schema, self.allowed = ontology, chunks, schema, allowed
        self.context = ChunkContextManager(chunks)
        self.control, self.decider, self.mode = controller, decider, mode
        self.thresholds = dict(thresholds or decision_thresholds())
        #: 旧口径：任何未决候选都让整图不可信。保留开关以便发布门禁按需恢复。
        self.strict_coverage = bool(strict_coverage)
        #: 关系阶段失败后允许的有界重试次数（超时/请求失败/响应不完整）。
        self.relation_retries = max(0, int(relation_retries))
        #: 单次 Clef 调用携带的问题数，与服务端 CLEF_MAX_QUESTIONS 对齐。
        self.decision_group = max(1, min(16, int(decision_group)))
        self.objects: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, str] = {}
        self.members: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.relations: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.clef_trace: list[dict[str, Any]] = []
        self.search: dict[str, dict[str, Any]] = {}
        self.resolve_calls: Counter = Counter()
        self.counts: Counter = Counter()
        self.revision = 0
        self.recoveries: list[dict[str, Any]] = []
        self._relation_rerun_chunks: set[str] = set()
        self._relation_rechecks: dict[str, dict[str, Any]] = {}
        self._repair_resolves = 0
        self.contract = {
            "classes": [{"type": cls.identifier, "name": cls.display_name, "abstract": cls.is_abstract,
                         "parents": list(schema.parents.get(cls.identifier, ())),
                         "identity_fallbacks": list(schema.identity_properties_for(cls.identifier)),
                         "properties": [{"id": p.identifier, "name": p.display_name, "type": p.data_type, "description": p.comment}
                                        for p in schema._properties_for(cls.identifier)]}
                        for cls in schema.classes if cls.identifier in allowed],
            "relations": [{"from_type": rel.source, "rel_type": rel.identifier, "to_type": rel.target}
                          for rel in schema.relations],
        }

    def issue(self, code: str, chunk: str, stage: str, *, claim: Any = None) -> dict[str, Any]:
        issue = {"id": f"issue-{len(self.issues) + 1}", "code": code, "chunk": chunk, "stage": stage,
                 "status": "unresolved", "claim": deepcopy(claim)}
        self.issues.append(issue)
        return issue

    def sources(self, chunk: str, *, expanded: bool = False, requested: Iterable[str] = ()) -> list[dict[str, Any]]:
        current = self.context.get_chunk(chunk)
        selected = self.context.local_context(chunk, previous=1 if expanded else 0, next=1 if expanded else 0, same_section=False)
        # Do not mix unrelated documents merely because they share a request.
        selected = [item for item in selected if item.document_id == current.document_id]
        for key in requested:
            if isinstance(key, str) and self.context.has_chunk(key):
                item = self.context.get_chunk(key)
                if item.document_id == current.document_id and item not in selected:
                    selected.append(item)
        return [{"chunk_id": item.chunk_id, "text": item.text, "section_title": item.section_title} for item in selected]

    def context_property_ids(self, obj: dict[str, Any], declared: Any = None) -> list[str]:
        """LLM-selected semantic context plus declared identity; no domain heuristics."""
        if declared is None:
            declared = obj.get("context_property_ids", list(obj["properties"]))
        if not isinstance(declared, list) or any(not isinstance(key, str) for key in declared):
            declared = list(obj["properties"])
        normalized = []
        for key in declared:
            prop = self.schema.resolve_property(obj["type"], key)
            if prop is None or prop.identifier not in obj["properties"]:
                normalized = list(obj["properties"])
                break
            normalized.append(prop.identifier)
        return list(dict.fromkeys([*self.schema.identity_properties_for(obj["type"]),
                                   *normalized]))

    def catalog(self, *, stage: str = "entities", candidate_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        selected = set(candidate_ids) if candidate_ids is not None else None
        catalog = []
        for obj in self.objects.values():
            if selected is not None and obj["id"] not in selected:
                continue
            keys = self.context_property_ids(obj)
            catalog.append({"candidate_id": obj["id"], "type": obj["type"], "name": obj["display_name"],
                            "properties": {key: deepcopy(obj["properties"][key]) for key in keys if key in obj["properties"]},
                            "available_property_ids": list(obj["properties"]),
                            "evidence_refs": [{"chunk_id": source, "start_char": evidence["start_char"], "end_char": evidence["end_char"]}
                                              for evidence in obj["evidence"] for source in evidence["source_block_ids"]]})
        return catalog

    def payload(self, chunk: str, *, stage: str = "entities", expanded: bool = False,
                requested: Iterable[str] = (), candidate_ids: Iterable[str] | None = None) -> dict[str, Any]:
        # Keep the original ontology once: its free-form notes may carry meaning
        # not represented by the parser's normalized class/property contract.
        catalog = self.catalog(stage=stage, candidate_ids=candidate_ids)
        return {"ontology": self.ontology, "allowed_entity_types": sorted(self.allowed),
                "schema_context": build_schema_context(self.schema, allowed_entity_types=self.allowed,
                    candidates=catalog, include_properties=stage in {"entities", "attributes", "repair"},
                    include_relations=stage != "entities"),
                "sources": render_sources(self.sources(chunk, expanded=expanded, requested=requested)),
                "entities": catalog, "anchor_chunk_id": chunk,
                "available_chunk_ids": [item.chunk_id for item in self.chunks if item.document_id == self.context.get_chunk(chunk).document_id]}

    def referenced_entity_ids(self, values: Any, *, chunk: str = "") -> list[str]:
        found: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, str):
                key = self.endpoint(value, chunk)
                if key is not None:
                    found.add(key)

        visit(values)
        return sorted(found)

    def evidence_source_ids(self, value: Any, chunk: str) -> list[str]:
        """Collect only mechanically located evidence dependencies, recursively."""
        found: set[str] = set()

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                if "evidence" in item:
                    found.update(source_ids(locate(item["evidence"], self.context, chunk)))
                for nested in item.values():
                    visit(nested)
            elif isinstance(item, list):
                for nested in item:
                    visit(nested)

        visit(value)
        return sorted(found)

    def prompt_record(self, value: Any, chunk: str, *, available_sources: set[str] | None = None) -> Any:
        """Replace already anchored evidence copies with exact source references.

        Invalid evidence is kept intact for repair. No property meaning is inferred
        or discarded; this only removes a second copy of available original text.
        """
        if isinstance(value, list):
            return [self.prompt_record(item, chunk, available_sources=available_sources) for item in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        result = {}
        for key, item in value.items():
            if key == "evidence":
                evidence = locate(item, self.context, chunk)
                if evidence and (available_sources is None or set(source_ids(evidence)) <= available_sources):
                    result["evidence_refs"] = [{"chunk_id": source, "start_char": part["start_char"], "end_char": part["end_char"]}
                                               for part in evidence for source in part["source_block_ids"]]
                    continue
            result[key] = self.prompt_record(item, chunk, available_sources=available_sources)
        return result

    def call(self, stage: str, chunk: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return self.control.call(stage, chunk, payload)
        except CallFailure as exc:
            self.issue(exc.code, chunk, stage, claim={"task_ids": [r["task_id"] for r in payload.get("tasks", []) if isinstance(r, dict) and "task_id" in r]})
            return None

    def call_stage(self, stage: str, chunk: str, payload: dict[str, Any], *, retries: int = 0) -> dict[str, Any] | None:
        """Bounded retry for a failed stage; a later success retires the transient failures.

        每次显式阶段仍然一次 HTTP 请求；重试只覆盖"没有拿到可用响应"的情形，
        并在拿到响应后把上一次失败留下的未决问题标为已解决，避免历史失败
        永久否决整张图。
        """
        issue_start = len(self.issues)
        for attempt in range(max(0, int(retries)) + 1):
            if attempt and self.control.calls >= self.control.max_calls:
                break
            response = self.call(stage, chunk, payload)
            if response is not None:
                if attempt:
                    for issue in self.issues[issue_start:]:
                        if issue["status"] == "unresolved":
                            issue["status"] = "resolved"
                            issue["resolution"] = {"method": "stage_retry_succeeded", "attempt": attempt + 1}
                return response
        return None

    def array(self, payload: dict[str, Any], key: str, chunk: str, stage: str, *, required: bool = False) -> list[Any]:
        value = payload.get(key, [] if not required else None)
        if not isinstance(value, list):
            self.issue("LLM_SCHEMA_INVALID", chunk, stage, claim={"field": key})
            return []
        return value

    def model_issues(self, payload: dict[str, Any], chunk: str, stage: str) -> None:
        for value in self.array(payload, "issues", chunk, stage):
            # This is an explicit model judgement, not a keyword classifier.
            # Unknown/legacy formats remain blocking; operational and mechanical
            # failures never enter this informational-note path.
            evidence = locate(value.get("evidence"), self.context, chunk) if isinstance(value, dict) else []
            informational = (isinstance(value, dict) and value.get("kind") == "source_observation"
                             and value.get("affects_completeness") is False
                             and isinstance(value.get("message"), str) and bool(value["message"].strip())
                             and bool(evidence))
            issue = self.issue("SOURCE_OBSERVATION" if informational else "SEMANTIC_SCOPE_UNRESOLVED",
                               chunk, stage, claim=value)
            if informational:
                issue.update(status="observed", evidence=evidence)

    def properties(self, type_id: str, values: Any, chunk: str, stage: str,
                   *, candidate_id: str | None = None) -> dict[str, Any] | None:
        if not isinstance(values, dict):
            self.issue("INVALID_PROPERTIES", chunk, stage)
            return None
        result = {}
        invalid_identity = False
        for key, value in values.items():
            if value in (None, "", []):
                continue
            prop = self.schema.resolve_property(type_id, key)
            if prop is None or not typed(value, prop.data_type):
                self.issue("INVALID_PROPERTY_SCHEMA", chunk, stage, claim={"candidate_id": candidate_id, "type": type_id, "property": key, "value": value})
                invalid_identity |= bool(prop and prop.identifier in self.schema.identity_properties_for(type_id))
                continue
            result[prop.identifier] = deepcopy(value)
        return None if invalid_identity else result

    def identity_for(self, type_id: str, properties: dict[str, Any], name: str) -> str:
        return next((str(properties[key]) for key in self.schema.identity_properties_for(type_id)
                     if properties.get(key) not in (None, "", [])), name.strip())

    def entity(self, item: Any, chunk: str, stage: str) -> dict[str, Any] | None:
        self.counts["entity_candidates"] += 1
        if not isinstance(item, dict):
            self.issue("INVALID_ENTITY", chunk, stage)
            return None
        cls = self.schema.resolve_class(item.get("type"))
        if cls is None or cls.is_abstract or cls.identifier not in self.allowed:
            self.issue("INVALID_ENTITY_TYPE", chunk, stage, claim=item)
            return None
        name = item.get("name")
        cid = str(item.get("candidate_id") or f"candidate-{self.counts['entity_candidates']}")
        properties = self.properties(cls.identifier, item.get("properties", {}), chunk, stage,
                                     candidate_id=f"{chunk}:{cid}")
        evidence = locate(item.get("evidence"), self.context, chunk)
        if not isinstance(name, str) or not name.strip() or not evidence:
            self.issue("INVALID_ENTITY_EVIDENCE_OR_NAME", chunk, stage, claim=item)
            return None
        if properties is None:
            return None
        # Exact identity declaration determines graph identity; semantic correctness
        # of the value remains part of the model decision, never a substring rule.
        identity = self.identity_for(cls.identifier, properties, name)
        alternatives = item.get("alternative_types", [])
        if not isinstance(alternatives, list):
            self.issue("INVALID_ALTERNATIVE_TYPES", chunk, stage, claim=item)
            return None
        return {"id": entity_uid(cls.identifier, identity, None), "type": cls.identifier, "identity": identity,
                "display_name": name.strip(), "properties": properties, "evidence": evidence,
                "proposal_id": f"{chunk}:{cid}", "source_chunk": chunk,
                "alternative_types": alternatives,
                "context_property_ids": self.context_property_ids({"type": cls.identifier, "properties": properties}, item.get("context_property_ids"))}

    def task(self, kind: str, claim: dict[str, Any], evidence: list[dict[str, Any]], chunk: str,
             criteria: dict[str, str] | None = None, *, positive: bool = True) -> dict[str, Any]:
        record = {"task_id": f"r{self.revision}-{kind}-{len(self.tasks) + 1}", "kind": kind,
                  "claim": deepcopy(claim), "evidence": deepcopy(evidence), "chunk": chunk,
                  "source_chunk_ids": list(source_ids(evidence)) or [chunk], "criteria": criteria or SUPPORT,
                  "status": "pending", "positive_proposal": positive, "revision": self.revision}
        # Main-model resolution can replace evidence with a wider verified quote.
        # Keep the input identity separate from that outcome and from task IDs.
        record["original_evidence"] = deepcopy(evidence)
        record["decision_fingerprint"] = self.decision_fingerprint(record)
        self.tasks.append(record)
        return record

    def decision_fingerprint(self, record: dict[str, Any]) -> str:
        identity = {key: record[key] for key in ("kind", "claim", "source_chunk_ids", "positive_proposal", "chunk")}
        identity.update(evidence=record["original_evidence"], criteria=list(record["criteria"].items()),
                        schema=self.task_contract([record]), ontology=self.ontology,
                        document_id=self.context.get_chunk(record["chunk"]).document_id)
        return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def decision_is_current(self, record: dict[str, Any]) -> bool:
        """Reuse an immutable judgement only while its bound referents still hold."""
        if (record["status"] not in {"decided", "accepted", "rejected"}
                or record.get("selected") not in record["criteria"]
                or record.get("selected") in {"insufficient", "UNKNOWN"}):
            return False
        claim, kind, chunk = record["claim"], record["kind"], record["chunk"]
        if kind == "relation_support":
            return self.relation_task_is_current(record)
        if kind in {"entity_support", "attribute_support", "attribute_conflict"}:
            reference = claim.get("proposal_id") if kind == "entity_support" else claim.get("candidate_id")
            key = self.endpoint(reference, chunk)
            if key is None:
                return kind == "entity_support" and record.get("selected") == "contradicted"
            obj = self.objects[key]
            if (obj["type"], obj["display_name"]) != (claim["type"], claim["name"]):
                return False
            if kind == "entity_support":
                if obj["identity"] != self.identity_for(claim["type"], claim["properties"], claim["name"]):
                    return False
            elif key != reference:
                return False
            if kind == "attribute_conflict":
                options = {f"value_{index}": value for index, value in enumerate(claim["values"])}
                return (record.get("selected") in options
                        and _same_json(obj["properties"].get(claim["property"]), options[record["selected"]]))
            if record.get("selected") == "supported":
                return all(prop in obj["properties"] and _same_json(obj["properties"][prop], value)
                           for prop, value in claim["properties"].items())
            # Negative attribute judgements must not survive a later accepted
            # correction to their target; compare the original referent snapshot.
            return record.get("decision_bindings") == self.decision_bindings(record)
        # Type/alias tasks carry independent candidate and merge-group state. A
        # changed canonical referent requires a fresh semantic judgement.
        return record.get("decision_bindings") == self.decision_bindings(record)

    def decision_bindings(self, record: dict[str, Any]) -> Any:
        claim, chunk = record["claim"], record["chunk"]
        references = []
        if record["kind"] == "entity_identity":
            group = record.get("merge_group", {})
            references = [group.get("left_id"), group.get("right_id")]
        elif record["kind"] == "entity_type":
            references = [record.get("candidate", {}).get("proposal_id")]
        else:
            references = [claim.get("candidate_id", claim.get("proposal_id"))]
        return [(reference, self.endpoint(reference, chunk),
                 deepcopy(self.object_semantics(self.objects.get(self.endpoint(reference, chunk)))))
                for reference in references]

    def resolve_records(self, pending: list[dict[str, Any]], chunk: str, *, purpose: str | None = None) -> None:
        payload = self.payload(chunk, stage="resolve", expanded=True,
                               requested=[c for r in pending for c in r["source_chunk_ids"]]
                                         + self.evidence_source_ids(pending, chunk), candidate_ids=[])
        payload["tasks"] = [self.prompt_record({key: r[key] for key in ("task_id", "kind", "claim", "criteria", "source_chunk_ids", "evidence")}, chunk,
                                               available_sources={source["chunk_id"] for source in payload["sources"]}) for r in pending]
        if purpose:
            payload["purpose"] = purpose
        response = self.call("resolve", chunk, payload)
        if response is not None:
            self.apply_decisions(response.get("decisions"), pending, chunk,
                                 available_sources={source["chunk_id"] for source in payload["sources"]})
            for record in pending:
                if record["status"] == "decided" and record.get("provider") == "llm":
                    record["resolution_sources_sha256"] = self.sources_fingerprint(payload["sources"])
                    record["resolution_source_chunk_ids"] = [source["chunk_id"] for source in payload["sources"]]
            self.model_issues(response, chunk, "resolve")

    @staticmethod
    def sources_fingerprint(sources: list[dict[str, Any]]) -> str:
        return hashlib.sha256(json.dumps(sources, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def same_resolution_context(self, previous: dict[str, Any], record: dict[str, Any],
                                batch: list[dict[str, Any]]) -> bool:
        if previous.get("provider") != "llm":
            return True
        # A wider LLM read is part of its semantic input, even when its final
        # quote is short. A repair-stage answer without this resolve snapshot
        # cannot substitute for the distinct resolve context.
        group = [item for item in batch if item["chunk"] == record["chunk"]]
        sources = render_sources(self.sources(record["chunk"], expanded=True,
                                 requested=[source for item in group for source in item["source_chunk_ids"]]
                                           + self.evidence_source_ids(group, record["chunk"])))
        return previous.get("resolution_sources_sha256") == self.sources_fingerprint(sources)

    def decide(self, records: list[dict[str, Any]], *, allow_resolve: bool = True) -> None:
        if not records:
            return
        current_ids = {record["task_id"] for record in records}
        # Freeze the pool before this batch. An unreviewed duplicate in the same
        # batch must never borrow an answer that is still being obtained.
        pool = {record["decision_fingerprint"]: record for record in self.tasks
                if record["task_id"] not in current_ids and self.decision_is_current(record)}
        pending_records = []
        for record in records:
            record["decision_bindings"] = self.decision_bindings(record)
            previous = pool.get(record["decision_fingerprint"])
            if (previous is None or not self.same_resolution_context(previous, record, records)
                    or (record["kind"] == "relation_support" and not self.relation_task_is_current(record))):
                pending_records.append(record)
                continue
            record.update(status="decided", selected=previous["selected"], provider=previous["provider"],
                          evidence=deepcopy(previous["evidence"]), reused_from=previous["task_id"])
            for field in ("clef", "context_expanded", "resolution_sources_sha256", "resolution_source_chunk_ids"):
                if field in previous:
                    record[field] = deepcopy(previous[field])
            self.clef_trace.append({"provider": "harness", "type": "decision_reuse", "task_ids": [record["task_id"]],
                                    "reused_from": previous["task_id"], "terminal": False})
            self.counts["reused_decisions"] += 1
        records = pending_records
        if not records:
            return
        tasks = []
        # 同组共享源状态，每个问题仍绑定自己的不可变 claim；组大小与 Clef 单次
        # 调用可带的问题数一致，避免把同一批任务拆成多次串行 HTTP。
        group_size = max(1, int(self.decision_group))
        for offset in range(0, len(records), group_size):
            group = records[offset:offset + group_size]
            state = {"claims": {r["task_id"]: r["claim"] for r in group},
                     "evidence_sets": {r["task_id"]: r["evidence"] for r in group},
                     "ontology_context": self.task_contract(group),
                     "__clef_task_projection__": {"version": 1, "tasks": {
                         r["task_id"]: {"claim_ids": [r["task_id"]], "evidence_set_ids": [r["task_id"]]} for r in group}}}
            for record in group:
                tasks.append(DecisionTask(task_id=record["task_id"], kind=record["kind"], state=deepcopy(state),
                                          instructions=f"仅根据 evidence_sets[{record['task_id']}] 判断 claims[{record['task_id']}]。候选不是证据。忠实保持身份、类型、条件、否定与方向。",
                                          criteria=record["criteria"], source_chunk_ids=tuple(record["source_chunk_ids"])))
        try:
            results = self.decider.decide(tasks, deadline=self.control.deadline, trace=self.clef_trace)
        except Exception as exc:
            # Custom clients/deciders may raise; this is an attempt failure, not yet
            # a logical-task failure because the main model can recover the task.
            self.clef_trace.append({"provider": "clef", "type": "request", "task_ids": [r["task_id"] for r in records],
                                    "source_chunk_ids": sorted({c for r in records for c in r["source_chunk_ids"]}),
                                    "exception_class": type(exc).__name__, "terminal": False})
            results = {}
        for record in records:
            result = results.get(record["task_id"])
            if result is None or result.status != "decided":
                record["decision_reason"] = getattr(result, "reason", "unavailable")
                continue
            selected = result.selected
            probability = result.probabilities.get(selected, 0)
            threshold = self.thresholds["supported"] if selected == "supported" else self.thresholds["alternate"]
            record["clef"] = {"selected": selected, "probabilities": deepcopy(result.probabilities), "margin": result.margin,
                              "details": deepcopy(result.details), "threshold": threshold,
                              "margin_threshold": self.thresholds["margin"]}
            if (selected not in record["criteria"] or selected in {"insufficient", "UNKNOWN"}
                    or probability < threshold or result.margin < self.thresholds["margin"]):
                continue
            record.update(selected=selected, provider="clef", status="decided")
        if allow_resolve:
            by_chunk = defaultdict(list)
            for record in records:
                if record["status"] == "pending":
                    by_chunk[record["chunk"]].append(record)
            for chunk, pending in by_chunk.items():
                if self.resolve_calls[chunk] >= 2:
                    continue
                self.resolve_calls[chunk] += 1
                self.resolve_records(pending, chunk)
        for record in records:
            if record["status"] == "pending":
                record["status"] = "unresolved"

    def task_contract(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        # Include actual class/property meanings and signatures, not only opaque IDs.
        type_ids: set[str] = set()
        relation_ids: set[str] = set()
        property_ids: dict[str, set[str]] = defaultdict(set)
        type_choice_ids: set[str] = set()

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                owner_types = ([value["type"]] if isinstance(value.get("type"), str) else [])
                if isinstance(value.get("types"), list):
                    choices = [item for item in value["types"] if isinstance(item, str)]
                    owner_types.extend(choices)
                    type_choice_ids.update(choices)
                used = set(value["properties"]) if isinstance(value.get("properties"), dict) else set()
                if isinstance(value.get("property"), str):
                    used.add(value["property"])
                for owner in owner_types:
                    property_ids[owner].update(used)
                for key, item in value.items():
                    if key == "type" and isinstance(item, str):
                        type_ids.add(item)
                    elif key == "types" and isinstance(item, list):
                        type_ids.update(name for name in item if isinstance(name, str))
                    elif key == "relation" and isinstance(item, str):
                        relation_ids.add(item)
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        for record in records:
            collect(record["claim"])
        classes = []
        for cls in self.contract["classes"]:
            if cls["type"] not in type_ids:
                continue
            selected = deepcopy(cls)
            if cls["type"] not in type_choice_ids:
                # Describe the fields actually being judged; do not spend the
                # verifier's context on unrelated, absent property definitions.
                used = property_ids[cls["type"]] | set(cls["identity_fallbacks"])
                selected["properties"] = [prop for prop in selected["properties"] if prop["id"] in used]
            classes.append(selected)
        relations = [rel for rel in self.contract["relations"] if rel["rel_type"] in relation_ids]
        return {"classes": classes, "relations": relations}

    def apply_decisions(self, values: Any, pending: list[dict[str, Any]], chunk: str,
                        *, available_sources: set[str] | None = None) -> None:
        if not isinstance(values, list):
            self.issue("LLM_SCHEMA_INVALID", chunk, "resolve")
            return
        by_id = {record["task_id"]: record for record in pending if record["status"] in {"pending", "unresolved"}}
        for item in values:
            if not isinstance(item, dict) or not isinstance(item.get("task_id"), str) or item.get("task_id") not in by_id:
                continue
            record = by_id[item["task_id"]]
            if record["status"] not in {"pending", "unresolved"}:
                continue
            if record["kind"] == "relation_support" and not self.relation_task_is_current(record):
                record["status"] = "superseded"
                self._relation_rerun_chunks.add(record["chunk"])
                continue
            selected = item.get("selected")
            evidence = locate(item.get("evidence"), self.context, chunk)
            if not isinstance(selected, str) or selected not in record["criteria"] or selected == "UNKNOWN" or not evidence:
                continue
            if available_sources is not None and not set(source_ids(evidence)) <= available_sources:
                self.issue("DECISION_EVIDENCE_NOT_READ", chunk, "resolve", claim={
                    "task_ids": [record["task_id"]], "chunk_ids": sorted(set(source_ids(evidence)) - available_sources)})
                continue
            if selected == "insufficient":
                if not record["positive_proposal"] and item.get("context_complete") is True:
                    record.update(status="resolved_no_support", selected=selected, provider="llm", evidence=evidence)
                continue
            if item.get("context_complete") is not True:
                continue
            record.update(selected=selected, status="decided", provider="llm", evidence=evidence, context_expanded=True)
            self.counts["llm_resolved_tasks"] += 1

    def selected(self, record: dict[str, Any], option: str) -> bool:
        return record["status"] in {"decided", "accepted"} and record.get("selected") == option

    def accept(self, obj: dict[str, Any]) -> str:
        key = obj["id"]
        previous = self.objects.get(key)
        if previous is None:
            self.objects[key] = deepcopy(obj)
        else:
            # Equal declared identities or canonical names are mechanical duplicate
            # keys. Property disagreements are separate semantic tasks, never overwritten.
            previous["evidence"] = unique(previous["evidence"] + obj["evidence"])
            previous["context_property_ids"] = list(dict.fromkeys(self.context_property_ids(previous) + self.context_property_ids(obj)))
            for prop, value in obj["properties"].items():
                if prop not in previous["properties"]:
                    previous["properties"][prop] = deepcopy(value)
                elif not _same_json(previous["properties"][prop], value):
                    self.issue("PROPERTY_CONFLICT", obj["source_chunk"], "attributes", claim={"candidate_id": key, "property": prop, "values": [previous["properties"][prop], value]})
        self.aliases[obj["proposal_id"]] = key
        self.aliases[key] = key
        self.members[key].append(deepcopy(obj))
        self.resolve_property_repairs(key, obj["properties"])
        return key

    def resolve_property_repairs(self, key: str, properties: dict[str, Any]) -> None:
        """Only an accepted, typed correction closes that candidate's field issue."""
        obj = self.objects[key]
        for issue in self.issues:
            if issue["status"] != "unresolved" or issue["code"] != "INVALID_PROPERTY_SCHEMA":
                continue
            claim = issue.get("claim") or {}
            if self.endpoint(claim.get("candidate_id"), issue["chunk"]) != key:
                continue
            prop = self.schema.resolve_property(obj["type"], claim.get("property"))
            if (prop and prop.identifier in properties and typed(properties[prop.identifier], prop.data_type)
                    and _same_json(obj["properties"].get(prop.identifier), properties[prop.identifier])):
                issue["status"] = "resolved"
                issue["resolution"] = {"candidate_id": key, "property": prop.identifier,
                                       "method": "accepted_semantic_property_correction"}

    def entities(self, values: list[Any], chunk: str, stage: str) -> list[str]:
        candidates = [obj for item in values if (obj := self.entity(item, chunk, stage)) is not None]
        type_tasks = []
        for obj in candidates:
            alternatives = []
            for name in [obj["type"], *obj["alternative_types"]]:
                cls = self.schema.resolve_class(name)
                if cls and not cls.is_abstract and cls.identifier in self.allowed and cls.identifier not in alternatives:
                    alternatives.append(cls.identifier)
            if len(alternatives) > 1:
                criteria = {name: self.schema.classes_by_id[name].display_name for name in alternatives}
                criteria["UNKNOWN"] = "这些类型均无充分依据，或候选集不足以决定。"
                task = self.task("entity_type", {"name": obj["display_name"], "properties": obj["properties"], "types": alternatives}, obj["evidence"], chunk, criteria)
                task["candidate"] = deepcopy(obj)
                type_tasks.append((obj, task))
        self.decide([task for _, task in type_tasks])
        blocked = set()
        for obj, task in type_tasks:
            if task["status"] != "decided":
                # The undecided type excludes this candidate from every later
                # catalog. Record that exclusion where it happens, instead of
                # leaving only the unanswered type question behind. Naming the
                # task in ``task_ids`` lets the existing recovery rule retire
                # this exclusion as soon as the type is actually decided.
                blocked.add(obj["proposal_id"])
                self.issue("ENTITY_TYPE_UNRESOLVED", chunk, stage, claim={
                    "proposal_id": obj["proposal_id"], "name": obj["display_name"],
                    "types": deepcopy(task["claim"]["types"]), "task_ids": [task["task_id"]]})
                continue
            if task["selected"] != obj["type"]:
                target = task["selected"]
                properties = self.properties(target, obj["properties"], chunk, stage, candidate_id=obj["proposal_id"])
                if properties is None:
                    blocked.add(obj["proposal_id"])
                    continue
                identity = self.identity_for(target, properties, obj["display_name"])
                obj.update(type=target, properties=properties, identity=identity, id=entity_uid(target, identity, None))
                obj["context_property_ids"] = self.context_property_ids(obj)
        pairs = [(obj, self.task("entity_support", {"type": obj["type"], "name": obj["display_name"], "properties": obj["properties"], "proposal_id": obj["proposal_id"]}, obj["evidence"], chunk))
                 for obj in candidates if obj["proposal_id"] not in blocked]
        self.decide([task for _, task in pairs])
        accepted = []
        for obj, task in pairs:
            if self.selected(task, "supported"):
                obj["evidence"] = unique(task["evidence"] + obj["evidence"])
                accepted.append(self.accept(obj))
                task["status"] = "accepted"
            elif self.selected(task, "contradicted"):
                task["status"] = "rejected"
                self.counts["rejected_entities"] += 1
        return accepted

    def endpoint(self, value: Any, chunk: str) -> str | None:
        if not isinstance(value, str):
            return None
        key = self.aliases.get(f"{chunk}:{value}", self.aliases.get(value, value))
        seen = set()
        while key in self.aliases and self.aliases[key] != key and key not in seen:
            seen.add(key)
            key = self.aliases[key]
        return key if key in self.objects else None

    def identity_conflict(self, left: dict[str, Any], right: dict[str, Any]) -> bool:
        if left["type"] != right["type"]:
            return True
        for prop in self.schema.identity_properties_for(left["type"]):
            a, b = left["properties"].get(prop), right["properties"].get(prop)
            if a not in (None, "", []) and b not in (None, "", []) and not _same_json(a, b):
                return True
        return False

    def merge_aliases(self, values: list[Any], chunk: str) -> bool:
        changed = False
        for value in values:
            if not isinstance(value, dict):
                self.issue("INVALID_ALIAS_PAIR", chunk, "aliases")
                continue
            left_id, right_id = self.endpoint(value.get("left"), chunk), self.endpoint(value.get("right"), chunk)
            if not left_id or not right_id:
                self.issue("UNKNOWN_ALIAS_ENDPOINT", chunk, "aliases", claim=value)
                continue
            if left_id == right_id:
                continue
            left, right = self.objects[left_id], self.objects[right_id]
            # All existing members participate, preventing an unverified transitive
            # A~B, B~C chain from silently merging A with C.
            left_members = self.members[left_id] or [left]
            right_members = self.members[right_id] or [right]
            if any(self.identity_conflict(a, b) for a in left_members for b in right_members):
                self.counts["identity_conflicts"] += 1
                continue
            records = [self.task("entity_identity", {"left": {k: a[k] for k in ("type", "display_name", "properties")},
                                                          "right": {k: b[k] for k in ("type", "display_name", "properties")}},
                                 unique(a["evidence"] + b["evidence"]), chunk, IDENTITY)
                       for a in left_members for b in right_members]
            for record in records:
                record["merge_group"] = {"left_id": left_id, "right_id": right_id, "task_ids": [r["task_id"] for r in records]}
            self.decide(records)
            if not all(self.selected(task, "same_entity") for task in records):
                continue
            self.merge_objects(left_id, right_id, chunk)
            for record in records:
                record["status"] = "accepted"
            changed = True
        return changed

    def merge_objects(self, left_id: str, right_id: str, chunk: str) -> bool:
        left_id, right_id = self.endpoint(left_id, chunk), self.endpoint(right_id, chunk)
        if not left_id or not right_id or left_id == right_id:
            return False
        left, right = self.objects[left_id], self.objects[right_id]
        if any(self.identity_conflict(a, b) for a in self.members[left_id] or [left] for b in self.members[right_id] or [right]):
            return False
        left["evidence"] = unique(left["evidence"] + right["evidence"])
        left["context_property_ids"] = list(dict.fromkeys(self.context_property_ids(left) + self.context_property_ids(right)))
        for prop, value in right["properties"].items():
            if prop not in left["properties"]:
                left["properties"][prop] = deepcopy(value)
            elif not _same_json(left["properties"][prop], value):
                self.issue("PROPERTY_CONFLICT", chunk, "attributes", claim={"candidate_id": left_id, "property": prop, "values": [left["properties"][prop], value]})
        self.members[left_id].extend(self.members.pop(right_id, [right]))
        del self.objects[right_id]
        self.aliases[right_id] = left_id
        for relation in self.relations:
            relation["from"] = left_id if relation["from"] == right_id else relation["from"]
            relation["to"] = left_id if relation["to"] == right_id else relation["to"]
            relation["condition_entity_ids"] = [left_id if key == right_id else key for key in relation.get("condition_entity_ids", [])]
        return True

    def attributes(self, values: list[Any], chunk: str, *, stage: str = "attributes") -> bool:
        records = []
        for value in values:
            if not isinstance(value, dict):
                self.issue("INVALID_ATTRIBUTE_UPDATE", chunk, stage)
                continue
            key = self.endpoint(value.get("candidate_id"), chunk)
            if key is None:
                self.issue("UNKNOWN_ATTRIBUTE_ENTITY", chunk, stage, claim=value)
                continue
            obj = self.objects[key]
            properties = self.properties(obj["type"], value.get("properties", {}), chunk, stage, candidate_id=key)
            if properties is not None:
                properties = {prop: val for prop, val in properties.items()
                              if prop not in obj["properties"] or not _same_json(obj["properties"][prop], val)}
            if not properties:
                # This asserts no new value. Do not spend another judgement or
                # attach a newly supplied, unreviewed quote to the accepted fact.
                continue
            evidence = locate(value.get("evidence"), self.context, chunk)
            if not evidence:
                self.issue("INVALID_ATTRIBUTE_EVIDENCE", chunk, stage, claim=value)
                continue
            # Ordinary attribute enrichment cannot silently change stable identity.
            if any(prop in self.schema.identity_properties_for(obj["type"]) and not _same_json(obj["properties"].get(prop), val)
                   for prop, val in properties.items()):
                self.issue("IDENTITY_CHANGE_REQUIRES_REPAIR", chunk, stage, claim=value)
                continue
            record = self.task("attribute_support", {"candidate_id": key, "type": obj["type"], "name": obj["display_name"], "properties": properties}, evidence, chunk)
            record["context_property_ids"] = self.context_property_ids({"type": obj["type"], "properties": properties}, value.get("context_property_ids"))
            records.append((key, properties, record))
        self.decide([record for _, _, record in records])
        changed = False
        for key, properties, record in records:
            if self.selected(record, "supported"):
                changed |= self.commit_attribute(record, stage=stage)
            elif self.selected(record, "contradicted"):
                record["status"] = "rejected"
        return changed

    def commit_attribute(self, record: dict[str, Any], *, stage: str = "attributes") -> bool:
        """Apply immediate and delayed support through the same typed/conflict boundary."""
        claim, chunk = record["claim"], record["chunk"]
        key = self.endpoint(claim.get("candidate_id"), chunk)
        if key is None:
            record["status"] = "unresolved"
            self.issue("UNKNOWN_ATTRIBUTE_ENTITY", chunk, stage, claim=claim)
            return False
        obj = self.objects[key]
        if claim.get("type") != obj["type"]:
            record["status"] = "superseded"
            self.issue("STALE_ATTRIBUTE_TASK", chunk, "attributes", claim=claim)
            return False
        properties = self.properties(obj["type"], claim.get("properties", {}), chunk, stage, candidate_id=key)
        if properties is None:
            record["status"] = "unresolved"
            return False
        if any(prop in self.schema.identity_properties_for(obj["type"]) and not _same_json(obj["properties"].get(prop), val)
               for prop, val in properties.items()):
            self.issue("IDENTITY_CHANGE_REQUIRES_REPAIR", chunk, stage, claim=claim)
            record["status"] = "unresolved"
            return False
        changed = False
        for prop, value in properties.items():
            if prop in obj["properties"] and not _same_json(obj["properties"][prop], value) and stage != "repair":
                self.issue("PROPERTY_CONFLICT", chunk, stage, claim={"candidate_id": key, "property": prop,
                           "values": [obj["properties"][prop], value]})
                continue
            changed |= not _same_json(obj["properties"].get(prop), value)
            obj["properties"][prop] = deepcopy(value)
        obj["evidence"] = unique(obj["evidence"] + record["evidence"])
        obj["context_property_ids"] = list(dict.fromkeys(
            self.context_property_ids(obj) + record.get("context_property_ids", list(properties))))
        self.resolve_property_repairs(key, properties)
        record["status"] = "accepted"
        return changed

    @staticmethod
    def object_semantics(obj: dict[str, Any] | None) -> Any:
        if obj is None:
            return None
        return (obj["type"], obj["display_name"], json.dumps(obj["properties"], sort_keys=True, ensure_ascii=False))

    def relation_task_is_current(self, record: dict[str, Any]) -> bool:
        """Decisions bind the endpoint and condition meanings supplied to the model."""
        claim, chunk = record["claim"], record["chunk"]
        keys = []
        for role in ("source", "target"):
            snapshot = claim[role]
            key = self.endpoint(snapshot["candidate_id"], chunk)
            if key != snapshot["candidate_id"] or key not in self.objects:
                return False
            obj = self.objects[key]
            if (snapshot["type"], snapshot["name"], json.dumps(snapshot["properties"], sort_keys=True, ensure_ascii=False)) != self.object_semantics(obj):
                return False
            keys.append(key)
        for snapshot in claim.get("condition_entities", []):
            key = self.endpoint(snapshot["id"], chunk)
            if key != snapshot["id"] or self.object_semantics(self.objects.get(key)) != self.object_semantics(snapshot):
                return False
        return any(self.schema.is_assignable(self.objects[keys[0]]["type"], rel.source)
                   and self.schema.is_assignable(self.objects[keys[1]]["type"], rel.target)
                   for rel in self.schema.resolve_relation(claim["relation"]))

    def retire_stale_relations(self) -> None:
        stale = set()
        for record in self.tasks:
            if (record["kind"] == "relation_support" and record["status"] != "superseded"
                    and not self.relation_task_is_current(record)):
                if record["status"] == "accepted" and record.get("selected") == "supported":
                    self._relation_rechecks[record["task_id"]] = deepcopy(record)
                record["status"] = "superseded"
                stale.add(record["task_id"])
                self._relation_rerun_chunks.add(record["chunk"])
        if stale:
            self.relations = [relation for relation in self.relations if relation.get("decision_task_id") not in stale]
            for issue in self.issues:
                if (issue["code"] == "CONDITION_NOT_LINKED" and isinstance(issue.get("claim"), dict)
                        and issue["claim"].get("decision_task_id") in stale):
                    issue["status"] = "superseded"

    def recheck_relations(self) -> None:
        """Rejudge prior supported facts against complete current referents first."""
        self.retire_stale_relations()
        previous = list(self._relation_rechecks.values())
        self._relation_rechecks.clear()
        records = []
        for old in previous:
            claim, chunk = deepcopy(old["claim"]), old["chunk"]
            keys = [self.endpoint(claim[role]["candidate_id"], chunk) for role in ("source", "target")]
            conditions = [self.endpoint(obj["id"], chunk) for obj in claim.get("condition_entities", [])]
            if None in keys or None in conditions:
                continue
            for role, key in zip(("source", "target"), keys):
                obj = self.objects[key]
                claim[role] = {"candidate_id": key, "type": obj["type"], "name": obj["display_name"],
                               "properties": deepcopy(obj["properties"])}
            claim["condition_entities"] = [deepcopy(self.objects[key]) for key in conditions]
            if not self.relation_task_is_current({"claim": claim, "chunk": chunk}):
                continue
            # Both the original located assertion and any wider, independently
            # verified evidence remain available; neither is fabricated anew.
            evidence = unique(old["original_evidence"] + old["evidence"])
            record = self.task("relation_support", claim, evidence, chunk, old["criteria"])
            record["revalidated_from"] = old["task_id"]
            records.append(record)
        self.decide(records, allow_resolve=False)
        pending = [record for record in records if record["status"] == "unresolved"]
        if pending and self._repair_resolves < 1:
            # One explicit repair resolve per round, independent of the ordinary
            # per-chunk quota but charged to the same Controller call budget.
            chunk = pending[0]["chunk"]
            document = self.context.get_chunk(chunk).document_id
            pending = [record for record in pending if self.context.get_chunk(record["chunk"]).document_id == document]
            self._repair_resolves += 1
            self.resolve_records(pending, chunk, purpose="repair_relation_reverification")
        self.materialize_resolved(records)
        for record in records:
            if self.selected(record, "contradicted"):
                record["status"] = "rejected"
                self.counts["rejected_relations"] += 1

    def relation_refresh_chunks(self, before: dict[str, dict[str, Any]]) -> set[str]:
        self.retire_stale_relations()
        affected = set(self._relation_rerun_chunks)
        self._relation_rerun_chunks.clear()
        mapped_previous = {self.endpoint(key, "") for key in before}
        for key in before.keys() | self.objects.keys():
            old, new = before.get(key), self.objects.get(key)
            if self.object_semantics(old) == self.object_semantics(new):
                continue
            related = any(self.schema.is_assignable(obj["type"], endpoint)
                          for obj in (old, new) if obj is not None
                          for rel in self.schema.relations for endpoint in (rel.source, rel.target))
            if not related:
                continue
            if new is not None and key not in before and key not in mapped_previous:
                # A newly admitted endpoint can unlock candidates in any previous
                # search scope. Keep existing supported edges while searching anew.
                affected.update(item.chunk_id for item in self.chunks if item.text.strip())
            else:
                for obj in (old, new):
                    if obj is not None:
                        affected.update(source_ids(obj["evidence"]))
        return affected

    def relation_items(self, values: list[Any], chunk: str, *, allow_resolve: bool = True) -> None:
        pairs = []
        for value in values:
            self.counts["relation_candidates"] += 1
            if not isinstance(value, dict):
                self.issue("INVALID_RELATION", chunk, "relations")
                continue
            left, right = self.endpoint(value.get("from_id"), chunk), self.endpoint(value.get("to_id"), chunk)
            signature = next((rel for rel in self.schema.resolve_relation(value.get("rel_type"))
                              if left and right and self.schema.is_assignable(self.objects[left]["type"], rel.source)
                              and self.schema.is_assignable(self.objects[right]["type"], rel.target)), None)
            evidence = locate(value.get("evidence"), self.context, chunk)
            if not left or not right or signature is None or not evidence:
                self.issue("INVALID_RELATION_REFERENCE_OR_EVIDENCE", chunk, "relations", claim=value)
                continue
            condition = value.get("condition_status", "unconditional")
            declared_conditions = value.get("condition_entity_ids", [])
            if not isinstance(declared_conditions, list):
                self.issue("INVALID_CONDITION_REFERENCES", chunk, "relations", claim=value)
                continue
            condition_ids = [self.endpoint(key, chunk) for key in declared_conditions]
            if None in condition_ids or (condition == "unconditional" and condition_ids):
                self.issue("INVALID_CONDITION_REFERENCES", chunk, "relations", claim=value)
                continue
            if not isinstance(condition, str) or condition not in {"unconditional", "represented"} or (condition == "represented" and (not condition_ids or None in condition_ids)):
                self.issue("CONDITION_NOT_REPRESENTED", chunk, "relations", claim=value)
                continue
            claim = {"source": {"candidate_id": left, "type": self.objects[left]["type"], "name": self.objects[left]["display_name"], "properties": self.objects[left]["properties"]},
                     "relation": signature.identifier,
                     "target": {"candidate_id": right, "type": self.objects[right]["type"], "name": self.objects[right]["display_name"], "properties": self.objects[right]["properties"]},
                     "condition_status": condition, "condition_entities": [self.objects[key] for key in condition_ids]}
            if "fact" in value:
                if not isinstance(value["fact"], str) or not value["fact"].strip():
                    self.issue("INVALID_RELATION_FACT", chunk, "relations", claim=value)
                    continue
                # A model's self-contained proposition is a claim, never evidence.
                claim["fact"] = value["fact"].strip()
            record = self.task("relation_support", claim, evidence, chunk)
            pairs.append(({"from": left, "to": right, "type": signature.identifier, "evidence": evidence, "source_chunk": chunk,
                           "condition_entity_ids": condition_ids, "decision_task_id": record["task_id"]}, record))
        self.decide([record for _, record in pairs], allow_resolve=allow_resolve)
        for relation, record in pairs:
            if self.selected(record, "supported"):
                relation["evidence"] = unique(record["evidence"] + relation["evidence"])
                self.relations.append(relation)
                record["status"] = "accepted"
            elif self.selected(record, "contradicted"):
                record["status"] = "rejected"
                self.counts["rejected_relations"] += 1

    def discovery(self, chunks: Iterable[str] | None = None, *, requested: Iterable[str] = ()) -> None:
        selected = set(chunks) if chunks is not None else {item.chunk_id for item in self.chunks}
        for chunk in self.chunks:
            if chunk.chunk_id not in selected or not chunk.text.strip():
                continue
            response = self.call("entities", chunk.chunk_id, self.payload(chunk.chunk_id, expanded=True, requested=requested))
            if response is None:
                continue
            self.entities(self.array(response, "entities", chunk.chunk_id, "entities", required=True), chunk.chunk_id, "entities")
            self.merge_aliases(self.array(response, "alias_pairs", chunk.chunk_id, "entities"), chunk.chunk_id)
            self.model_issues(response, chunk.chunk_id, "entities")
            requested_ids = self.array(response, "context_requests", chunk.chunk_id, "entities")
            if requested_ids:
                self.issue("ADDITIONAL_CONTEXT_REQUESTED", chunk.chunk_id, "entities", claim={"chunk_ids": requested_ids})

    def enrich(self, chunks: Iterable[str] | None = None, *, requested: Iterable[str] = ()) -> None:
        selected = set(chunks) if chunks is not None else {item.chunk_id for item in self.chunks}
        for chunk in self.chunks:
            if chunk.chunk_id not in selected or not chunk.text.strip():
                continue
            requested_properties = []
            for obj in self.objects.values():
                if chunk.chunk_id not in source_ids(obj["evidence"]):
                    continue
                missing = [p.identifier for p in self.schema._properties_for(obj["type"]) if p.identifier not in obj["properties"]
                           and p.identifier not in self.schema.identity_properties_for(obj["type"])]
                if missing:
                    requested_properties.append({"candidate_id": obj["id"], "requested_properties": missing})
            if not requested_properties:
                continue
            payload = self.payload(chunk.chunk_id, stage="attributes", expanded=True,
                                   requested=requested, candidate_ids=[item["candidate_id"] for item in requested_properties])
            payload["requested_properties"] = requested_properties
            response = self.call("attributes", chunk.chunk_id, payload)
            if response is not None:
                self.attributes(self.array(response, "updates", chunk.chunk_id, "attributes", required=True), chunk.chunk_id)
                self.model_issues(response, chunk.chunk_id, "attributes")

    def relate(self, chunks: Iterable[str] | None = None, *, requested: Iterable[str] = ()) -> None:
        selected = set(chunks) if chunks is not None else {item.chunk_id for item in self.chunks}
        for chunk in self.chunks:
            if chunk.chunk_id not in selected or not chunk.text.strip():
                continue
            payload = self.payload(chunk.chunk_id, stage="relations", expanded=True, requested=requested)
            plan = {"anchor_chunk_id": chunk.chunk_id, "source_chunk_ids": [s["chunk_id"] for s in payload["sources"]],
                    "relation_signatures": self.contract["relations"], "revision": self.revision,
                    "candidate_method": "llm_semantic_search", "status": "pending",
                    "outside_scope": "Non-adjacent evidence is supplied only when explicitly requested; semantic recall is not proven by execution completeness."}
            self.search[chunk.chunk_id] = plan
            if not self.schema.relations or not self.objects:
                plan["status"] = "completed_no_endpoints" if self.schema.relations else "completed_no_signatures"
                continue
            response = self.call_stage("relations", chunk.chunk_id, payload, retries=self.relation_retries)
            if response is None:
                plan["status"] = "unresolved"
                continue
            relations = self.array(response, "relations", chunk.chunk_id, "relations", required=True)
            self.relation_items(relations, chunk.chunk_id)
            self.model_issues(response, chunk.chunk_id, "relations")
            requested_ids = self.array(response, "context_requests", chunk.chunk_id, "relations")
            if requested_ids:
                self.issue("ADDITIONAL_CONTEXT_REQUESTED", chunk.chunk_id, "relations", claim={"chunk_ids": requested_ids})
            plan.update(status="scanned", proposed_candidates=len(relations), context_requests=requested_ids)

    def unresolved(self) -> list[dict[str, Any]]:
        return [issue for issue in self.issues if issue["status"] == "unresolved"] + [record for record in self.tasks if record["status"] == "unresolved"]

    def resolve_property_conflicts(self) -> None:
        pairs = []
        for issue in self.issues:
            if issue["code"] != "PROPERTY_CONFLICT" or issue["status"] != "unresolved" or issue.get("decision_task_id"):
                continue
            claim = issue["claim"]
            key = self.endpoint(claim.get("candidate_id"), issue["chunk"])
            if key is None:
                continue
            obj = self.objects[key]
            values = claim["values"]
            criteria = {f"value_{index}": "该实体在原文作用域中的正确完整属性值为 " + json.dumps(value, ensure_ascii=False)
                        for index, value in enumerate(values)}
            criteria["UNKNOWN"] = "这些值不足以表达原文，或条件不同、证据矛盾，不能唯一选择。"
            task = self.task("attribute_conflict", {"candidate_id": key, "type": obj["type"], "name": obj["display_name"],
                                                   "property": claim["property"], "values": deepcopy(values)}, obj["evidence"], issue["chunk"], criteria)
            task["issue_id"] = issue["id"]
            issue["decision_task_id"] = task["task_id"]
            pairs.append(task)
        self.decide(pairs)
        self.materialize_resolved(pairs)

    def validate_graph(self) -> None:
        """Only public-reference and explicitly declared multiplicity invariants."""
        conditional_pending = [deepcopy(issue["claim"]) for issue in self.issues
                               if issue["code"] == "CONDITION_NOT_LINKED" and issue["status"] == "unresolved"]
        for issue in self.issues:
            if issue["code"] in {"CONDITION_NOT_LINKED", "CARDINALITY_CONFLICT"}:
                issue["status"] = "superseded"
        accepted = []
        edges = {(edge["from"], edge["to"]) for edge in self.relations}
        for relation in self.relations + conditional_pending:
            conditions = relation.get("condition_entity_ids", [])
            linked = all(key in {relation["from"], relation["to"]} or any(
                (key, endpoint) in edges or (endpoint, key) in edges for endpoint in (relation["from"], relation["to"])
            ) for key in conditions)
            if not linked:
                self.issue("CONDITION_NOT_LINKED", relation["source_chunk"], "relations", claim=deepcopy(relation))
            else:
                accepted.append(relation)
        self.relations = [value for value in unique(accepted) if value["from"] in self.objects and value["to"] in self.objects]
        for signature in self.schema.relations:
            matching = [edge for edge in self.relations if edge["type"] == signature.identifier
                        and self.schema.is_assignable(self.objects[edge["from"]]["type"], signature.source)
                        and self.schema.is_assignable(self.objects[edge["to"]]["type"], signature.target)]
            for cardinality, group_key, value_key in ((signature.target_cardinality, "from", "to"),
                                                       (signature.source_cardinality, "to", "from")):
                match = re.fullmatch(r"\s*(?:\d+\s*\.\.\s*)?(\d+)\s*", cardinality or "")
                if match is None:
                    continue
                maximum = int(match.group(1))
                grouped = defaultdict(list)
                for edge in matching:
                    grouped[edge[group_key]].append(edge)
                for key, group in grouped.items():
                    if len({edge[value_key] for edge in group}) > maximum:
                        self.issue("CARDINALITY_CONFLICT", group[0]["source_chunk"], "relations", claim={
                            "candidate_id": key, "relation": signature.identifier, "maximum": maximum,
                            "relations": deepcopy(group)})

    def rerun_stage(self, stage: str, chunk: str, *, requested: Iterable[str] = ()) -> bool:
        """A claimed repair closes an old failure only after its actual stage reruns."""
        normalized = {"aliases": "entities", "repair": "entities"}.get(stage, stage)
        if normalized not in {"entities", "attributes", "relations"}:
            return False
        prior_issues = [issue for issue in self.issues if issue["status"] == "unresolved" and issue["chunk"] == chunk
                        and issue["code"] not in {"PROPERTY_CONFLICT", "INVALID_PROPERTY_SCHEMA", "CONDITION_NOT_LINKED", "CARDINALITY_CONFLICT"}
                        and {"aliases": "entities", "repair": "entities"}.get(issue["stage"], issue["stage"]) == normalized]
        issue_start, task_start, call_start = len(self.issues), len(self.tasks), len(self.control.attempts)
        if normalized == "entities":
            self.discovery([chunk], requested=requested)
        elif normalized == "attributes":
            self.enrich([chunk], requested=requested)
        else:
            self.relate([chunk], requested=requested)
            self.validate_graph()
        self.resolve_property_conflicts()
        new_unresolved = [issue for issue in self.issues[issue_start:] if issue["status"] == "unresolved"]
        new_unresolved += [record for record in self.tasks[task_start:] if record["status"] == "unresolved"]
        calls = self.control.attempts[call_start:]
        complete = not new_unresolved and (normalized == "attributes" or any(a["pass"] == normalized and a["type"] == "success" for a in calls))
        if complete:
            for issue in prior_issues:
                issue["status"] = "resolved"
            stage_kinds = {"entities": {"entity_support", "entity_type", "entity_identity"},
                           "attributes": {"attribute_support"}, "relations": {"relation_support"}}[normalized]
            for record in self.tasks[:task_start]:
                if (record["chunk"] == chunk and record["kind"] in stage_kinds and record["status"] == "unresolved"
                        and not record.get("revalidated_from")):
                    record["status"] = "superseded"
        self.recoveries.append({"stage": normalized, "chunk": chunk, "status": "completed" if complete else "unresolved",
                                "resolved_issue_ids": [issue["id"] for issue in prior_issues] if complete else [],
                                "source_chunk_ids": [source["chunk_id"] for source in self.sources(
                                    chunk, expanded=normalized != "entities" or bool(requested), requested=requested)]
                                    if any(a["pass"] == normalized and a["type"] == "success" for a in calls) else [],
                                "call_indices": [a["call_index"] for a in calls]})
        return complete

    def repair(self) -> None:
        self._repair_resolves = 0
        pending = self.unresolved()
        by_chunk = defaultdict(list)
        for value in pending:
            by_chunk[value["chunk"]].append(value)
        # One document round, at most two requests, followed by dependency reruns.
        for chunk, issues in list(by_chunk.items())[:2]:
            # Earlier chunk repairs may already retire tasks in this snapshot.
            issues = [issue for issue in issues if issue["status"] == "unresolved"]
            if not issues:
                continue
            requested = [key for issue in issues if isinstance(issue.get("claim"), dict)
                         and isinstance(issue["claim"].get("chunk_ids"), list)
                         for key in issue["claim"]["chunk_ids"] if isinstance(key, str)]
            requested.extend(source for issue in issues for source in issue.get("source_chunk_ids", []) if isinstance(source, str))
            requested.extend(self.evidence_source_ids(issues, chunk))
            payload = self.payload(chunk, stage="repair", expanded=True, requested=requested,
                                   candidate_ids=self.referenced_entity_ids(issues, chunk=chunk))
            available_sources = {source["chunk_id"] for source in payload["sources"]}
            payload["issues"] = [self.prompt_record({key: issue[key] for key in ("id", "code", "stage", "claim")}, chunk, available_sources=available_sources)
                                 for issue in issues if "id" in issue]
            payload["tasks"] = [self.prompt_record({key: record[key] for key in ("task_id", "kind", "claim", "criteria", "source_chunk_ids")}, chunk, available_sources=available_sources)
                                for record in issues if "task_id" in record]
            response = self.call("repair", chunk, payload)
            if response is None:
                continue
            recovery_start = len(self.recoveries)
            request_issue = None
            new_requests = self.array(response, "context_requests", chunk, "repair")
            available = {source["chunk_id"] for source in payload["sources"]}
            unread = [source for source in new_requests if isinstance(source, str) and source not in available]
            if unread or any(not isinstance(source, str) for source in new_requests):
                stage_by_kind = {"entity_support": "entities", "entity_type": "entities", "entity_identity": "entities",
                                 "attribute_support": "attributes", "attribute_conflict": "attributes", "relation_support": "relations"}
                required_stages = {stage_by_kind.get(issue.get("kind"), issue.get("stage")) for issue in issues}
                required_stages = sorted(stage for stage in required_stages if isinstance(stage, str))
                request_issue = self.issue("REPAIR_CONTEXT_NOT_READ", chunk, "context", claim={
                    "chunk_ids": deepcopy(new_requests), "required_stages": required_stages})
                requested.extend(unread)
            before = deepcopy(self.objects)
            new_entities = self.array(response, "entities", chunk, "repair")
            self.entities(new_entities, chunk, "repair")
            for value in self.array(response, "updates", chunk, "repair"):
                if not isinstance(value, dict):
                    continue
                old_key = self.endpoint(value.get("candidate_id"), chunk)
                if old_key is None:
                    self.issue("UNKNOWN_REPAIR_ENTITY", chunk, "repair", claim=value)
                    continue
                old = self.objects[old_key]
                proposed_properties = value.get("properties") or {}
                identity_update = isinstance(proposed_properties, dict) and any(
                    key in self.schema.identity_properties_for(old["type"]) and not _same_json(old["properties"].get(key), val)
                    for key, val in proposed_properties.items())
                if any(key in value for key in ("type", "name")) or identity_update:
                    proposed_type = value.get("type", old["type"])
                    proposed_class = self.schema.resolve_class(proposed_type)
                    same_type = proposed_class is not None and proposed_class.identifier == old["type"]
                    proposed_values = value.get("properties", {})
                    # Updates are patches. A compact catalog deliberately omits
                    # unrelated accepted values, so omission cannot delete them.
                    replacement_properties = ({**old["properties"], **proposed_values}
                                              if same_type and isinstance(proposed_values, dict)
                                              else value.get("properties", old["properties"]))
                    replacement_context = self.context_property_ids(old)
                    replacement_context.extend(self.context_property_ids({"type": proposed_type, "properties": proposed_values},
                                                                         value.get("context_property_ids"))
                                               if isinstance(proposed_values, dict) else [])
                    replacement_evidence = locate(value.get("evidence", old["evidence"]), self.context, chunk)
                    if not replacement_evidence:
                        self.issue("INVALID_ENTITY_EVIDENCE_OR_NAME", chunk, "repair", claim=value)
                        continue
                    replacement = {"candidate_id": value["candidate_id"], "type": value.get("type", old["type"]),
                                   "name": value.get("name", old["display_name"]), "properties": replacement_properties,
                                   "context_property_ids": list(dict.fromkeys(replacement_context)),
                                   "evidence": unique(old["evidence"] + replacement_evidence)}
                    accepted = self.entities([replacement], chunk, "repair")
                    if accepted:
                        new_key = accepted[0]
                        # A supported explicit rename is a patch even when the
                        # declared identity keeps the same graph key. Ordinary
                        # duplicate discovery still retains its first display name.
                        if new_key == old_key and "name" in value:
                            self.objects[new_key]["display_name"] = replacement["name"].strip()
                        if new_key != old_key:
                            del self.objects[old_key]
                            self.aliases[old_key] = new_key
                            self.members.pop(old_key, None)
                        self.resolve_property_repairs(new_key, self.objects[new_key]["properties"])
                else:
                    self.attributes([value], chunk, stage="repair")
            self.merge_aliases(self.array(response, "alias_pairs", chunk, "repair"), chunk)
            self.retire_stale_relations()
            unresolved_tasks = [record for record in issues if "task_id" in record]
            self.apply_decisions(response.get("decisions", []), unresolved_tasks, chunk,
                                 available_sources={source["chunk_id"] for source in payload["sources"]})
            # Materialize independently resolved assertions without treating a bare
            # resolved_issue_ids list as evidence of successful repair.
            self.materialize_resolved(unresolved_tasks)
            self.resolve_property_conflicts()
            original_issue_ids = {value for value in self.array(response, "resolved_issue_ids", chunk, "repair") if isinstance(value, str)}
            for stage in dict.fromkeys(issue["stage"] for issue in issues if issue.get("id") in original_issue_ids):
                self.rerun_stage(stage, chunk, requested=requested)
            # Recovered logical tasks retire their failed generation attempts. A
            # transport failure with no task binding still requires a stage rerun.
            by_task_id = {record["task_id"]: record for record in self.tasks}
            for issue in self.issues:
                task_ids = issue.get("claim", {}).get("task_ids", []) if isinstance(issue.get("claim"), dict) else []
                if issue["status"] == "unresolved" and task_ids and all(by_task_id.get(key, {}).get("status") in {"accepted", "rejected", "decided", "resolved_no_support", "superseded"} for key in task_ids):
                    issue["status"] = "resolved"
            changed = not _same_json(before, self.objects)
            if changed:
                self.revision += 1
                attribute_chunks = set()
                for key in before.keys() | self.objects.keys():
                    old, new = before.get(key), self.objects.get(key)
                    if old is None or new is None or any(old[field] != new[field] for field in ("type", "identity", "display_name")):
                        for obj in (old, new):
                            if obj is not None:
                                attribute_chunks.update(source_ids(obj["evidence"]))
                for affected_chunk in attribute_chunks:
                    self.rerun_stage("attributes", affected_chunk, requested=requested)
                relation_chunks = self.relation_refresh_chunks(before)
                # Existing supported facts get the scarce semantic resolve
                # before a fresh relation search can consume that same budget.
                self.recheck_relations()
                for original_chunk in self.chunks:
                    if original_chunk.chunk_id in relation_chunks and original_chunk.text.strip():
                        self.rerun_stage("relations", original_chunk.chunk_id, requested=requested)
            self.relation_items(self.array(response, "relations", chunk, "repair"), chunk, allow_resolve=False)
            self.model_issues(response, chunk, "repair")
            self.validate_graph()
            if request_issue and unread and all(isinstance(source, str) for source in new_requests):
                stages = request_issue["claim"]["required_stages"]
                # A request for new context is complete only when every affected
                # stage actually received those originals and completed its rerun.
                if stages and all(any(recovery["chunk"] == chunk and recovery["stage"] == stage
                                      and recovery["status"] == "completed"
                                      and set(unread) <= set(recovery["source_chunk_ids"])
                                      for recovery in self.recoveries[recovery_start:]) for stage in stages):
                    request_issue["status"] = "resolved"

    def materialize_resolved(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            if record["kind"] == "attribute_conflict" and record["status"] == "decided":
                claim = record["claim"]
                key = self.endpoint(claim["candidate_id"], record["chunk"])
                selected = record.get("selected", "")
                options = {f"value_{index}": value for index, value in enumerate(claim["values"])}
                if key and selected in options:
                    self.objects[key]["properties"][claim["property"]] = deepcopy(options[selected])
                    record["status"] = "accepted"
                    for issue in self.issues:
                        if issue["id"] == record["issue_id"]:
                            issue["status"] = "resolved"
                else:
                    record["status"] = "unresolved"
                continue
            if record["kind"] == "entity_type" and record["status"] == "decided":
                candidate = record.get("candidate")
                if candidate:
                    item = {"candidate_id": candidate["proposal_id"].removeprefix(record["chunk"] + ":"),
                            "type": record["selected"], "name": candidate["display_name"],
                            "properties": candidate["properties"], "evidence": record["evidence"]}
                    if self.entities([item], record["chunk"], "repair"):
                        record["status"] = "accepted"
                    else:
                        record["status"] = "unresolved"
                continue
            if record["kind"] == "entity_identity" and record["status"] == "decided":
                group = record.get("merge_group", {})
                linked = [task for task in self.tasks if task["task_id"] in group.get("task_ids", [])]
                if linked and all(self.selected(task, "same_entity") for task in linked):
                    if self.merge_objects(group["left_id"], group["right_id"], record["chunk"]):
                        for task in linked:
                            task["status"] = "accepted"
                    else:
                        record["status"] = "unresolved"
                continue
            if not self.selected(record, "supported"):
                continue
            claim, chunk = record["claim"], record["chunk"]
            if record["kind"] == "entity_support":
                item = {"candidate_id": claim.get("proposal_id", record["task_id"]).removeprefix(chunk + ":"), "type": claim["type"], "name": claim["name"],
                        "properties": claim["properties"], "evidence": record["evidence"]}
                obj = self.entity(item, chunk, "repair")
                if obj:
                    self.accept(obj)
                    record["status"] = "accepted"
            elif record["kind"] == "attribute_support":
                self.commit_attribute(record)
            elif record["kind"] == "relation_support":
                if not self.relation_task_is_current(record):
                    record["status"] = "superseded"
                    self._relation_rerun_chunks.add(chunk)
                    continue
                left, right = self.endpoint(claim["source"]["candidate_id"], chunk), self.endpoint(claim["target"]["candidate_id"], chunk)
                if left and right:
                    self.relations.append({"from": left, "to": right, "type": claim["relation"], "evidence": record["evidence"], "source_chunk": chunk,
                                           "condition_entity_ids": [obj["id"] for obj in claim.get("condition_entities", [])],
                                           "decision_task_id": record["task_id"]})
                    record["status"] = "accepted"

    def is_blocking(self, value: dict[str, Any]) -> bool:
        """图谱完整性口径：只有它决定已发布图谱是否可信。

        ``strict_coverage`` 恢复旧口径（任何未决候选都算阻断），供发布门禁按需启用。
        """
        if self.strict_coverage:
            return True
        code = value.get("code")
        if code is None:
            # 未决的逻辑任务：候选没有进入图谱，属于召回缺口。
            return False
        return code in INTEGRITY_ISSUE_CODES

    def reconcile_issues(self) -> None:
        """撤回"主体随后已被发布"的旧拒绝记录。

        被拒候选可能由其它切片或修复轮以合法证据重新提出。候选一旦以同样的
        三元组（或同样的实体身份）进入最终图谱，旧的就地拒绝就不再是未决事项；
        否则历史失败会永久否决一张已经自洽的图。
        """
        published_relations = {(relation["from"], relation["type"], relation["to"]) for relation in self.relations}
        for issue in self.issues:
            if issue["status"] != "unresolved":
                continue
            relation_issue = issue["code"] in CANDIDATE_RELATION_ISSUE_CODES
            if not relation_issue and issue["code"] not in CANDIDATE_ENTITY_ISSUE_CODES:
                continue
            claim, chunk = issue.get("claim"), issue["chunk"]
            if not isinstance(claim, dict):
                continue
            published = False
            if relation_issue:
                left = self.endpoint(claim.get("from_id"), chunk)
                right = self.endpoint(claim.get("to_id"), chunk)
                names = {relation.identifier for relation in self.schema.resolve_relation(claim.get("rel_type"))}
                published = bool(left and right and any((left, name, right) in published_relations for name in names))
            else:
                reference = claim.get("candidate_id") or claim.get("proposal_id")
                if isinstance(reference, str) and reference:
                    published = self.endpoint(reference, chunk) in self.objects
            if published:
                issue["status"] = "superseded"
                issue["resolution"] = {"method": "subject_published_before_result"}

    def result(self, *, best_effort: bool, started: float, repairs: int) -> dict[str, Any]:
        self.reconcile_issues()
        unresolved = self.unresolved()
        blocking = [value for value in unresolved if self.is_blocking(value)]
        coverage = [value for value in unresolved if value not in blocking]
        # 只有执行类问题才说明"这段原文没有被真正读完"；候选被拒是正常的语义
        # 筛选，不应把整个切片记成失败。
        failed_chunks = sorted({value["chunk"] for value in unresolved
                                if value.get("code") in EXECUTION_ISSUE_CODES})
        objects = list(self.objects.values())
        relation_map = {}
        for relation in self.relations:
            left, right = self.endpoint(relation["from"], relation["source_chunk"]), self.endpoint(relation["to"], relation["source_chunk"])
            if left not in self.objects or right not in self.objects:
                continue
            key = (left, relation["type"], right)
            if key not in relation_map:
                relation_map[key] = {**deepcopy(relation), "from": left, "to": right}
            else:
                relation_map[key]["evidence"] = unique(relation_map[key]["evidence"] + relation["evidence"])
        relations = list(relation_map.values())
        assembler = GraphAssembler()
        graph = assembler.build_graph(objects, relations)
        for edge in graph["relationships"]:
            edge["evidences"] = deepcopy(relation_map[(edge["from_uid"], edge["type"], edge["to_uid"])]["evidence"])
        attempts = self.control.attempts + [{**item, "terminal": False} for item in self.clef_trace]
        for chunk in failed_chunks:
            has_entities = any(chunk in source_ids(obj["evidence"]) for obj in objects)
            stage = "relation" if has_entities else "entity"
            attempts.append({"provider": "harness", "chunk": chunk, "pass": stage, "type": "schema_validation",
                             "terminal": True, "message": "V3.1 logical tasks remain unresolved"})
        # 先算图谱，再按"图谱是否可用/可信"定状态：空图不是成功，完整性未过
        # 才是 partial；召回与执行缺口只降覆盖率，不否决已经发布的事实。
        usable = bool(objects) or bool(relations)
        if not usable:
            status = "failed"
        elif (blocking or failed_chunks) and best_effort:
            status = "partial"
        elif blocking or failed_chunks:
            status = "failed"
        else:
            status = "succeeded"
        diagnostic_code = "LLM_SCHEMA_INVALID" if unresolved else None
        budget_failure = next((value.get("code") for value in unresolved if value.get("code") in {"DOCUMENT_BUDGET_EXHAUSTED", "LLM_TIMEOUT"}), None)
        if budget_failure:
            diagnostic_code = "LLM_TIMEOUT"
        request_failure = next((value.get("code") for value in unresolved if value.get("code") == "LLM_REQUEST_FAILED"), None)
        if request_failure and not budget_failure:
            diagnostic_code = "LLM_REQUEST_FAILED"
        if not usable:
            # 空结果必须显式失败：模型返回空数组时 harness 不产生任何未决项，
            # 旧实现会把它当成一次成功抽取。有执行/校验失败原因时优先报原因。
            error_code = diagnostic_code or "EMPTY_EXTRACTION"
        elif blocking and not diagnostic_code:
            error_code = "PARTIAL_EXTRACTION_FAILED"
        else:
            # 执行类失败即使没有阻断整图也保留错误码：公开 extract() 契约要求
            # 不能把"某个切片没读成"悄悄当作成功。
            error_code = diagnostic_code
        profile = {"algorithm": "v3.1", "llm_calls": self.control.calls,
                   "llm_http_calls": sum(bool(a.get("request_sent")) for a in self.control.attempts),
                   "clef_calls": len({item["call_index"] for item in self.clef_trace if "call_index" in item}),
                   "chunk_count": len(self.chunks), "nonempty_chunks": sum(bool(c.text.strip()) for c in self.chunks),
                   "skipped_empty_chunks": sum(not c.text.strip() for c in self.chunks), "failed_chunks": failed_chunks,
                   "entity_count": len(objects), "relation_count": len(relations), "accepted_entities": len(objects),
                   "accepted_relations": len(relations), "entity_candidates": self.counts["entity_candidates"],
                   "relation_candidates": self.counts["relation_candidates"], "candidate_selection": dict(self.counts),
                   "repair_rounds": repairs, "validation_mode": self.mode, "elapsed_seconds": time.monotonic() - started,
                   "disable_thinking": getattr(self.control.llm, "disable_thinking", None),
                   "model": getattr(self.control.llm, "model", None), "late_responses_observed": self.control.late_responses,
                   "late_responses_ignored": sum(bool(a.get("late_response_ignored")) for a in self.control.attempts),
                   "logical_task_counts": dict(Counter(record["status"] for record in self.tasks)),
                   "source_observation_count": sum(issue["status"] == "observed" for issue in self.issues),
                   "strict_coverage": self.strict_coverage, "relation_retries": self.relation_retries,
                   "decision_thresholds": dict(self.thresholds),
                   "blocking_issue_count": len(blocking), "coverage_issue_count": len(coverage),
                   "degraded_error_code": None if status != "succeeded" else diagnostic_code,
                   "search_plan": deepcopy(list(self.search.values()))}
        warnings = [{"code": value.get("code", "SEMANTIC_TASK_UNRESOLVED"), "chunk": value["chunk"],
                     "severity": ("error" if value in blocking or value.get("code") in DECLARED_GAP_ISSUE_CODES
                                  else "warning"),
                     "message": "未完成的语义任务，详见 run_trace", "path": value.get("id", value.get("task_id"))}
                    for value in unresolved]
        warnings.extend({"code": "SOURCE_OBSERVATION", "chunk": issue["chunk"], "severity": "info",
                         "message": issue["claim"]["message"], "path": issue["id"]}
                        for issue in self.issues if issue["status"] == "observed")
        verification = {"valid": usable and not blocking, "coverage_complete": not unresolved, "failed_chunks": failed_chunks,
                        "hard_issues": deepcopy(blocking), "hard_issue_count": len(blocking),
                        "coverage_issues": deepcopy(coverage), "coverage_issue_count": len(coverage),
                        "strict_coverage": self.strict_coverage,
                        "scope": "executed_semantic_search_plan_not_exhaustive_recall"}
        trace = {"status": status, "ontology_sha256": hashlib.sha256(self.ontology.encode()).hexdigest(),
                 "attempts": attempts, "tasks": deepcopy(self.tasks), "issues": deepcopy(self.issues),
                 "alias_mapping": deepcopy(self.aliases), "search_plan": deepcopy(list(self.search.values())),
                 "recoveries": deepcopy(self.recoveries), "profile": profile}
        return {"raw": assembler.compat_raw(objects, relations), "graph": graph, "triples": assembler.build_triples(graph),
                "intermediate_model": {"objects": objects, "relations": relations}, "warnings": warnings, "attempts": attempts,
                "profile": profile, "verification": verification, "status": status, "error_code": error_code,
                "_error": ("V3.1 logical tasks remain unresolved" if not usable and unresolved
                           else "V3.1 extracted nothing from nonempty chunks" if not usable
                           else "V3.1 graph integrity issues remain unresolved" if blocking else None),
                "extract_version": "v3.1", "coverage_gaps": deepcopy(unresolved), "run_trace": trace}


def extract_harness(
    ontology: str, chunks: Iterable[ExtractionChunk], *, target_types: Iterable[str] | None = None,
    include_subclasses: bool = True, validation_mode: str = "strict", best_effort: bool = False,
    model: str | None = None, base_url: str | None = None, api_key: str | None = None,
    max_tokens: int | None = None, request_timeout: float | None = None, document_timeout: float | None = None,
    max_workers: int | None = None, llm: Any = None, clef_client: Any = None, decider: Any = None,
    disable_thinking: bool | None = None, max_llm_calls: int | None = None, clef_timeout: float | None = None,
    clef_max_calls: int | None = None, max_repair_rounds: int = 1, use_scheduler: bool = True,
    repairer: Any = None, entity_adjudicator: Any = None, prompt_variant: str | None = None,
    max_attempts: int = 1, retry_base_seconds: float = 0,
    support_threshold: float | None = None, alternate_threshold: float | None = None,
    decision_margin: float | None = None, strict_coverage: bool | None = None,
    relation_retries: int | None = None, clef_max_questions: int | None = None,
) -> dict[str, Any]:
    """V2-compatible contract, with independent V3.1 semantics and bounded repair.

    Legacy callbacks/worker knobs are accepted for caller compatibility. Semantic
    decisions use the explicit ``decider`` and primary ``llm`` interfaces instead.
    """
    started = time.monotonic()
    if not isinstance(ontology, str) or not ontology.strip():
        raise ValueError("ontology must be nonempty")
    if isinstance(chunks, (str, bytes, dict, ExtractionChunk)):
        raise TypeError("chunks must be an iterable of V2 ExtractionChunk")
    chunk_list = list(chunks)
    if any(not isinstance(item, ExtractionChunk) for item in chunk_list):
        raise TypeError("chunks must contain V2 ExtractionChunk")
    for item in chunk_list:
        if not isinstance(item.text, str) or not isinstance(item.chunk_id, str) or item.chunk_id != item.chunk_id.strip():
            raise ValueError("chunk id/text must be strings and chunk id cannot have surrounding spaces")
    ChunkContextManager(chunk_list)
    if validation_mode not in {"strict", "permissive"}:
        raise ValueError("validation_mode must be strict or permissive")
    if max_repair_rounds not in {0, 1, 2}:
        raise ValueError("max_repair_rounds must be 0, 1 or legacy 2 (capped at one)")
    if max_attempts != 1:
        raise ValueError("V3.1 uses one primary HTTP attempt per explicit stage; max_attempts must be 1")
    if isinstance(target_types, (str, bytes)):
        raise TypeError("target_types must be an iterable of type names")
    schema = OntologySchema.parse(ontology)
    if not schema.classes or schema.unresolved:
        raise ValueError("ontology must contain resolvable classes, inheritance and relations")
    allowed = {cls.identifier for cls in schema.classes}
    targets = list(target_types or [])
    if targets:
        allowed = set()
        for target in targets:
            cls = schema.resolve_class(target)
            if cls is None:
                raise ValueError(f"unknown target type: {target!r}")
            allowed.add(cls.identifier)
            if include_subclasses:
                allowed.update(schema.descendants(cls.identifier))
    budget = _positive_number(document_timeout, "KG_EXTRACT_V31_DOCUMENT_TIMEOUT", 900)
    timeout = _positive_number(request_timeout, "KG_EXTRACT_V31_REQUEST_TIMEOUT", 120)
    clef_seconds = _positive_number(clef_timeout, "KG_EXTRACT_V31_CLEF_TIMEOUT", 10)
    thresholds = decision_thresholds(support=support_threshold, alternate=alternate_threshold, margin=decision_margin)
    nonempty = sum(bool(item.text.strip()) for item in chunk_list)
    call_limit = 6 * nonempty + 2 if max_llm_calls is None else int(max_llm_calls)
    clef_limit = int(os.getenv("KG_EXTRACT_V31_CLEF_MAX_CALLS", "512") if clef_max_calls is None else clef_max_calls)
    clef_questions = int(os.getenv("KG_EXTRACT_V31_CLEF_MAX_QUESTIONS", "16")
                         if clef_max_questions is None else clef_max_questions)
    if call_limit < 1 or clef_limit < 1 or not 1 <= clef_questions <= 16:
        raise ValueError("call limits must be positive and Clef questions within 1..16")
    resolved_llm = llm
    if nonempty and resolved_llm is None:
        config = get_llm_config()
        resolved_llm = create_client(model=model or config["model"], base_url=base_url or config["base_url"],
                                     api_key=api_key or config["api_key"], max_tokens=max_tokens or config["max_tokens"],
                                     request_timeout=timeout, disable_thinking=thinking_disabled(disable_thinking))
    control = Controller(resolved_llm, deadline=started + budget, timeout=timeout, max_calls=call_limit,
                         scheduler=get_llm_scheduler() if use_scheduler and nonempty else None)
    harness = Harness(ontology=ontology, chunks=chunk_list, schema=schema, allowed=allowed,
                      controller=control, decider=decider or ClefDecider(client=clef_client, timeout=clef_seconds,
                                                                        max_calls=clef_limit, max_questions=clef_questions),
                      mode=validation_mode, thresholds=thresholds,
                      strict_coverage=_boolean(strict_coverage, "KG_EXTRACT_V31_STRICT_COVERAGE", False),
                      relation_retries=(int(os.getenv("KG_EXTRACT_V31_RELATION_RETRIES", "1"))
                                        if relation_retries is None else relation_retries),
                      decision_group=clef_questions)
    harness.discovery()
    harness.resolve_property_conflicts()
    harness.enrich()
    harness.resolve_property_conflicts()
    harness.relate()
    harness.validate_graph()
    repairs = 0
    if max_repair_rounds and harness.unresolved() and time.monotonic() < control.deadline and control.calls < control.max_calls:
        repairs = 1
        harness.repair()
    return harness.result(best_effort=best_effort, started=started, repairs=repairs)
