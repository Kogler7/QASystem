"""Canonical chat prompt builder and deterministic document routing."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from .prompt_templates import BASE_SYSTEM_PROMPT, build_system_prompt, render_user
from .domain_skills import select_domain_policy
from .output_contracts import build_output_contract
from .chunk_context import ExtractionChunk
from .structured_text import is_structured_payload
from .ontology_compiler import ExtractionPlan
from .graph_utils import canonical_name

RouteKind = Literal["generic", "official_document", "standard"]
RouteSource = Literal["ontology", "chunks", "fallback"]


class ChatMessage(TypedDict):
    role: Literal["system", "user"]
    content: str


ChatMessages = list[ChatMessage]
_DOMAIN_ORDER: tuple[RouteKind, RouteKind] = ("official_document", "standard")
_DOMAIN_THRESHOLD = 6
_DOMAIN_MARGIN = 3
_CHUNK_TEXT_LIMIT = 1200
_MAX_ROUTE_CHUNKS = 12

_STANDARD_NUMBER = re.compile(
    r"\b(?:GB\s*/\s*T|GB|GJB|JB\s*/\s*T|ISO|IEC|EN|Q\s*/\s*CR)\s*[—–-]?\s*\d+(?:[./—–-]\d+)*",
    re.IGNORECASE,
)
_CLAUSE_NUMBER = re.compile(r"^\s*\d+(?:\.\d+){0,3}\s+\S+")
_OFFICIAL_TITLE = re.compile(
    r"(?:^|\n)\s*(?:关于|关于印发|关于开展|关于同意|关于做好|关于进一步|关于切实|关于全面|关于组织)"
    r"[^\n]{2,100}?(?:的通知|的公告|的决定|的意见|的办法|的规定|的批复|的请示|的报告|的函)"
)
_OFFICIAL_NUMBER = re.compile(r"〔\d{4}〕\d+号")


@dataclass(frozen=True)
class DocumentRoute:
    kind: RouteKind
    source: RouteSource
    scores: tuple[tuple[str, int], tuple[str, int]]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "scores": [list(item) for item in self.scores],
            "reasons": list(self.reasons),
        }


def _score_rows(scores: dict[str, int]) -> tuple[tuple[str, int], tuple[str, int]]:
    return tuple((kind, int(scores.get(kind, 0))) for kind in _DOMAIN_ORDER)  # type: ignore[return-value]


def _ontology_tokens(plan: ExtractionPlan) -> set[str]:
    tokens: set[str] = set()
    for class_id, class_spec in plan.class_specs.items():
        tokens.add(canonical_name(class_id))
        tokens.add(canonical_name(class_spec.display_name))
        for prop in class_spec.properties:
            tokens.add(canonical_name(prop.identifier))
            tokens.add(canonical_name(prop.display_name))
    for rel_id, specs in plan.relation_specs.items():
        tokens.add(canonical_name(rel_id))
        for spec in specs:
            tokens.add(canonical_name(spec.relation_id))
            tokens.add(canonical_name(spec.display_label))
            tokens.add(canonical_name(spec.signature))
    return {item for item in tokens if item}


def _score_ontology(plan: ExtractionPlan) -> tuple[dict[str, int], set[str]]:
    tokens = _ontology_tokens(plan)
    scores = {"official_document": 0, "standard": 0}
    reasons: set[str] = set()

    official_hits = {
        "officialdocument": "route.ontology.official.class",
        "documentunit": "route.ontology.official.unit",
        "operationalactivity": "route.ontology.official.operation",
        "operationalexchange": "route.ontology.official.exchange",
        "issuedby": "route.ontology.official.issue_relation",
        "文件标题": "route.ontology.official.title_property",
        "文号": "route.ontology.official.number_property",
        "来文字号": "route.ontology.official.number_property",
        "收文号": "route.ontology.official.receipt_property",
    }
    standard_hits = {
        "standarddocument": "route.ontology.standard.class",
        "standard": "route.ontology.standard.class",
        "requirementclause": "route.ontology.standard.requirement",
        "clause": "route.ontology.standard.clause",
        "subclause": "route.ontology.standard.clause",
        "section": "route.ontology.standard.section",
        "definesrequirement": "route.ontology.standard.requirement_relation",
        "hassubclause": "route.ontology.standard.structure_relation",
        "标准号": "route.ontology.standard.number_property",
        "standardno": "route.ontology.standard.number_property",
        "standardnumber": "route.ontology.standard.number_property",
        "文件编号": "route.ontology.standard.number_property",
        "clauseno": "route.ontology.standard.clause_property",
        "条编号": "route.ontology.standard.clause_property",
        "章编号": "route.ontology.standard.section_property",
    }
    for token, reason in official_hits.items():
        if token in tokens:
            scores["official_document"] += 5
            reasons.add(reason)
    for token, reason in standard_hits.items():
        if token in tokens:
            scores["standard"] += 5
            reasons.add(reason)
    return scores, reasons


def _chunk_signal_text(chunk: ExtractionChunk) -> str:
    metadata = chunk.metadata or {}
    selected_metadata = []
    for key in (
        "title",
        "heading",
        "section_title",
        "unit",
        "block_kind",
        "document_number",
        "standard_number",
    ):
        value = metadata.get(key)
        if value is not None:
            selected_metadata.append(str(value))
    return "\n".join(
        item
        for item in (
            chunk.section_title or "",
            chunk.clause_number or "",
            "\n".join(selected_metadata),
            (chunk.text or "")[:_CHUNK_TEXT_LIMIT],
        )
        if item
    )


def _score_chunks(chunks: list[ExtractionChunk]) -> tuple[dict[str, int], set[str]]:
    scores = {"official_document": 0, "standard": 0}
    reasons: set[str] = set()
    seen_categories: set[str] = set()
    for chunk in chunks[:_MAX_ROUTE_CHUNKS]:
        text = _chunk_signal_text(chunk)
        if _STANDARD_NUMBER.search(text) and "standard.number" not in seen_categories:
            seen_categories.add("standard.number")
            scores["standard"] += 4
            reasons.add("route.chunks.standard.number")
        if (
            "standard.clause_structure" not in seen_categories
            and (
                chunk.clause_number
                or _CLAUSE_NUMBER.search(chunk.section_title or "")
                or _CLAUSE_NUMBER.search(text)
            )
        ):
            seen_categories.add("standard.clause_structure")
            scores["standard"] += 2
            reasons.add("route.chunks.standard.clause_structure")
        if (
            "standard.lexical" not in seen_categories
            and any(marker in text for marker in ("标准", "规范", "试验方法", "检验方法", "要求"))
        ):
            seen_categories.add("standard.lexical")
            scores["standard"] += 1
            reasons.add("route.chunks.standard.lexical")
        if _OFFICIAL_TITLE.search(text) and "official.title" not in seen_categories:
            seen_categories.add("official.title")
            scores["official_document"] += 4
            reasons.add("route.chunks.official.title")
        if (
            "official.number" not in seen_categories
            and (
                _OFFICIAL_NUMBER.search(text)
                or any(marker in text for marker in ("文号", "来文字号", "收文号"))
            )
        ):
            seen_categories.add("official.number")
            scores["official_document"] += 3
            reasons.add("route.chunks.official.number")
        if (
            "official.lexical" not in seen_categories
            and any(marker in text for marker in ("通知", "发文机关", "主送", "抄送", "附件：", "请各单位"))
        ):
            seen_categories.add("official.lexical")
            scores["official_document"] += 1
            reasons.add("route.chunks.official.lexical")
    return scores, reasons


def _select(scores: dict[str, int]) -> tuple[RouteKind, str]:
    official = scores["official_document"]
    standard = scores["standard"]
    if official >= standard:
        top_kind: RouteKind = "official_document"
        top_score = official
        other_score = standard
    else:
        top_kind = "standard"
        top_score = standard
        other_score = official
    if top_score < _DOMAIN_THRESHOLD:
        return "generic", "route.fallback.low_score"
    if top_score - other_score < _DOMAIN_MARGIN:
        return "generic", "route.fallback.margin_conflict"
    return top_kind, ""


def resolve_document_route(plan: ExtractionPlan, chunks: list[ExtractionChunk]) -> DocumentRoute:
    """Mirror V2 scoring; skills remain V3.1 assets, never V2 prompt extensions.

    Keep parity with v2.prompt_router; tests compare the full route metadata.
    """
    if isinstance(chunks, (str, bytes, dict)):
        raise TypeError("route requires a typed ExtractionChunk sequence")
    resolved_chunks = list(chunks)
    if any(not isinstance(chunk, ExtractionChunk) for chunk in resolved_chunks):
        raise TypeError("route requires a typed ExtractionChunk sequence")

    ontology_scores, ontology_reasons = _score_ontology(plan)
    ontology_kind, ontology_fallback = _select(ontology_scores)
    chunk_scores, chunk_reasons = _score_chunks(resolved_chunks)
    dual_strong_ontology = all(
        ontology_scores[domain] >= _DOMAIN_THRESHOLD for domain in _DOMAIN_ORDER
    )

    reasons = set(ontology_reasons)
    if ontology_kind != "generic" and not dual_strong_ontology:
        scores = ontology_scores
        source: RouteSource = "ontology"
        kind = ontology_kind
    else:
        scores = {
            domain: ontology_scores[domain] + chunk_scores[domain]
            for domain in _DOMAIN_ORDER
        }
        reasons.update(chunk_reasons)
        kind, fallback_reason = _select(scores)
        source = "chunks" if kind != "generic" else "fallback"
        if dual_strong_ontology:
            reasons.add("route.ontology.dual_strong")
        if fallback_reason:
            reasons.add(fallback_reason)
        elif ontology_fallback or dual_strong_ontology:
            reasons.add("route.chunks.domain_selected")

    if kind == "generic":
        if scores["official_document"] > 0 and scores["standard"] > 0:
            reasons.add("route.fallback.weak_conflict")
        if ontology_fallback:
            reasons.add(ontology_fallback)
        if not reasons:
            reasons.add("route.fallback.no_domain_signal")
        source = "fallback"
    else:
        reasons.add(f"route.selected.{kind}")

    return DocumentRoute(
        kind=kind,
        source=source,
        scores=_score_rows(scores),
        reasons=tuple(sorted(reasons)),
    )


def _catalog_text(plan: ExtractionPlan, *, include_comments: bool = False, type_ids=None) -> str:
    lines: list[str] = []
    for type_id in sorted(plan.allowed_types if type_ids is None else type_ids):
        spec = plan.class_specs.get(type_id)
        if spec is None:
            continue
        line = f"- `{spec.display_name}` -> type `{type_id}`"
        if spec.identity_properties:
            line += f"；identity: {', '.join(spec.identity_properties)}"
        lines.append(line)
        for prop in spec.properties:
            suffix = ""
            if include_comments:
                comment = " ".join((prop.comment or "").split())
                suffix = f" // {comment[:200]}" if comment else ""
            lines.append(f"  - `{prop.display_name}` -> `{prop.identifier}` ({prop.data_type}){suffix}")
    return "\n".join(lines) if lines else "（无本体类目录）"


def _type_catalog_text(plan: ExtractionPlan) -> str:
    """Minimal entity catalog; properties are resolved after span grounding."""
    lines = []
    for type_id in sorted(plan.allowed_types):
        spec = plan.class_specs.get(type_id)
        if spec is not None:
            lines.append(f"- `{spec.display_name}` -> type `{type_id}`")
    return "\n".join(lines) if lines else "（无本体类目录）"


def _known_entities_payload(
    known_entities: list[dict[str, Any]] | None, *, task: str,
) -> list[dict[str, Any]]:
    """Preserve the old catalog fields and repair budget in structured form."""
    entries = known_entities or []
    fields = ("type", "name", "properties", "occurrences", "missing_properties")
    if task == "repair":
        entries = entries[:25]
        fields = fields[:-1]
    result = []
    for entry in entries:
        item = {key: entry[key] for key in fields
                if key in entry and entry[key] not in (None, "", {}, [])}
        if "occurrences" in item:
            item["occurrences"] = [
                {key: value for key, value in {
                    "properties": occurrence.get("properties"),
                    "evidence": {"quote": occurrence["evidence"]["quote"]}
                        if (occurrence.get("evidence") or {}).get("quote") else None,
                }.items() if value not in (None, "", {}, [])}
                for occurrence in item["occurrences"]
            ]
            item["occurrences"] = [o for o in item["occurrences"] if o]
            if not item["occurrences"]:
                item.pop("occurrences")
        result.append(item)
    return result


def _chunk_payload(chunk: ExtractionChunk, *, task: str) -> dict[str, Any]:
    if task == "repair":
        return {"text": chunk.text or ""}
    payload = chunk.to_dict()
    for key in ("chunk_id", "document_id", "section_id", "parent_chunk_id",
                "previous_chunk_id", "next_chunk_id", "start_offset", "end_offset"):
        payload.pop(key, None)
    payload["metadata"] = {key: value for key, value in payload.get("metadata", {}).items()
                           if key != "signal_source" and not key.startswith("_v31_")}
    return {key: value for key, value in payload.items() if key == "text" or value not in (None, "", {}, [])}


def _looks_like_record_table(chunk: ExtractionChunk) -> bool:
    """识别 JSON 记录表 chunk。

    两种形态都算（判定细节见 structured_text）：

    * 整段就是 JSON——benchmark 数据集的 docs/*.md；批量路径的 "## 文件:" 头
      同样会被剥掉；
    * Markdown 外壳 + json 代码块——解析层按行为规范 §10 产出的真实形态
      （Excel 的 "## sheet 名 + 表格 JSON"，docx/OCR 原地替换的表格 JSON），
      此时要求 JSON 主体在 chunk 里占绝对多数，避免误判正文夹带 JSON 的文档。

    历史实现只认第一种，导致前端上传 xlsx 永远命中不了记录表策略。
    """
    return is_structured_payload(chunk.text or "")


def build_chat_messages(
    *,
    task: Literal["entity", "property", "repair"] | str,
    plan: ExtractionPlan,
    route: DocumentRoute,
    chunk: ExtractionChunk,
    source_chunks: list[ExtractionChunk] | None = None,
    known_entities: list[dict[str, Any]] | None = None,
    output_schema: dict[str, Any] | None = None,
    repair_issues: list[dict[str, Any]] | None = None,
    assembly_planner: Any | None = None,
) -> ChatMessages:
    entity_task = task == "entity" or (
        task == "repair" and "entities" in (output_schema or {}).get("properties", {})
    )
    if task not in {"entity", "property", "repair"}:
        raise ValueError(f"Unsupported extraction task: {task}")
    if output_schema is None:
        if entity_task:
            output_schema = plan.output_schemas["entity_candidates"]
        else:
            raise ValueError(f"output_schema is required for {task}")
    if task == "repair" and not entity_task:
        raise ValueError("Repair only supports entity candidates")
    if task == "repair":
        discipline = "只修复列出的 verifier issues；不得重复已接受事实，不得处理未列问题。"
    elif entity_task:
        discipline = "同时提取实体候选和当前片段支持的本体属性，禁止输出关系；按属性说明理解字段，不得因处于候选阶段而省略已知属性。"
    elif task == "property":
        discipline = "按给定实体的 type + name 补全属性并以 JSONL 逐行返回完整实体；跨切片相同 type + name 统一输出一个实体，不同 type 的同名实体分开；不得新增、遗漏或重命名实体，不得输出关系。"
    else:
        raise ValueError(f"Unsupported extraction task: {task}")

    record_table = _looks_like_record_table(chunk)
    type_ids = output_schema["properties"]["entities"]["items"]["properties"]["type"]["enum"]
    # Every stage now extracts properties. Their meaning must survive source
    # windowing, even when a window no longer forms a complete JSON table.
    ontology_section = _catalog_text(plan, include_comments=True, type_ids=type_ids)
    known = [] if entity_task else _known_entities_payload(known_entities, task=task)
    contract = build_output_contract(output_schema, entity_task=entity_task,
                                     task=task)
    payload = dict(
        task={
            "operation": task,
            "discipline": discipline,
            "known_entities": known,
            "repair_issues": repair_issues or [],
        },
        output_contract=contract,
        domain_policy={"route": route.to_dict()} if assembly_planner is not None else select_domain_policy(
            kind=route.kind, route=route.to_dict(),
            task="entity" if entity_task else task,
        ),
        ontology={
            "catalog": ontology_section,
        },
        documents=[_chunk_payload(source, task=task) for source in (source_chunks or [chunk])],
    )
    if assembly_planner is not None:
        payload = assembly_planner.assemble(payload, stage="entity" if entity_task else task)
    if entity_task:
        property_policy = (assembly_planner.assemble(payload, stage="property")["domain_policy"]
                           if assembly_planner is not None else select_domain_policy(
                               kind=route.kind, route=route.to_dict(), task="property"))
        # Candidate discovery now also extracts local properties. Both policies
        # are compiled once per request; append only stage-specific new lines.
        profiles = payload["domain_policy"].setdefault("profiles", {})
        for name, profile in property_policy.get("profiles", {}).items():
            if name not in profiles:
                profiles[name] = profile
                continue
            current = profiles[name]["extraction_rules"]
            existing = set(current.splitlines())
            extra = [line for line in profile["extraction_rules"].splitlines() if line and line not in existing]
            if extra:
                profiles[name]["extraction_rules"] = current + "\n\n" + "\n".join(extra)
    user = render_user(**payload)
    return [
        {"role": "system", "content": build_system_prompt(
            task=task, entity_task=entity_task,
        )},
        {"role": "user", "content": user},
    ]


__all__ = [
    "BASE_SYSTEM_PROMPT",
    "ChatMessage",
    "ChatMessages",
    "DocumentRoute",
    "build_chat_messages",
    "resolve_document_route",
]
