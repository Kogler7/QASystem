"""Canonical chat prompt builder and deterministic document routing."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from .prompt_templates import BASE_SYSTEM_PROMPT, render_user
from .domain_skills import select_domain_policy
from .output_contracts import build_output_contract
from .chunk_context import ExtractionChunk
from .structured_text import is_structured_payload
from .ontology_compiler import ExtractionPlan

RouteKind = Literal["generic", "official_document", "standard", "simple", "rule", "trouble"]
RouteSource = Literal["chunks", "fallback"]


class ChatMessage(TypedDict):
    role: Literal["system", "user"]
    content: str


ChatMessages = list[ChatMessage]
_DOMAIN_ORDER = ("official_document", "standard", "simple", "rule", "trouble")
_STANDARD_NUMBER = re.compile(
    r"\b(?:GB\s*/\s*T|GB|GJB|JB\s*/\s*T|ISO|IEC|EN|Q\s*/\s*CR)\s*[—–-]?\s*\d+(?:[./—–-]\d+)*",
    re.IGNORECASE,
)
_OFFICIAL_TITLE = re.compile(
    r'(?:^|\n|["“])\s*(?:[#*]\s*)*(?:关于)[^\n]{2,100}?'
    r"(?:的通知|的公告|的决定|的意见|的办法|的规定|的批复|的请示|的报告|的函)"
)
_OFFICIAL_NUMBER = re.compile(r"〔\d{4}〕\d+号")


@dataclass(frozen=True)
class DocumentRoute:
    kind: RouteKind
    source: RouteSource
    scores: tuple[tuple[str, int], ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "source": self.source,
                "scores": [list(item) for item in self.scores], "reasons": list(self.reasons)}


def resolve_document_route(plan: ExtractionPlan, chunks: list[ExtractionChunk]) -> DocumentRoute:
    """Classify once from the first supplied chunk text, never ontology or later chunks.

    Conservative content signatures mirror benchmark document families. Ambiguous
    or unrecognised text stays generic; filenames and metadata are not evidence.
    """
    if isinstance(chunks, (str, bytes, dict)):
        raise TypeError("route requires a typed ExtractionChunk sequence")
    resolved_chunks = list(chunks)
    if any(not isinstance(chunk, ExtractionChunk) for chunk in resolved_chunks):
        raise TypeError("route requires a typed ExtractionChunk sequence")
    text = (resolved_chunks[0].text or "") if resolved_chunks else ""
    text = unicodedata.normalize("NFKC", text)
    scores = dict.fromkeys(_DOMAIN_ORDER, 0)
    # Structured concept/example documents may discuss faults and rules. Their
    # explicit semantic headings distinguish them from maintenance row records.
    if (re.search(r"<<\s*(?:Stakeholder|Concern|Term|Problem|Solution)\s*>>", text, re.I)
            or sum(marker in text for marker in ("术语名称", "术语定义", "干系人:", "关注点:")) >= 2):
        scores["simple"] = 10
    if (_OFFICIAL_TITLE.search(text) or "收文处理单" in text
            or ("来文字号" in text and any(word in text for word in ("收文日期", "拟办意见", "领导批示")))):
        scores["official_document"] = 8
    elif _OFFICIAL_NUMBER.search(text) and any(word in text for word in ("通知", "主送", "来文单位")):
        scores["official_document"] = 6
    if _STANDARD_NUMBER.search(text) and any(word in text for word in ("标准", "规范", "试验方法", "技术要求", "范围")):
        scores["standard"] = 6
    if ("故障详细分类" in text and any(word in text for word in ("故障状况", "故障原因"))
            or ("前件" in text and "后件" in text)
            or ("antecedent" in text and "consequent" in text)):
        scores["rule"] = 6
    if ("故障内容" in text and "处理方法" in text
            or any(word in text for word in ("行修更换", "互换记录", "故障检修记录"))
            or ("故障" in text and "部件名称" in text and "故障标签" in text)):
        scores["trouble"] = 6
    ranked = sorted(scores, key=scores.get, reverse=True)
    kind = ranked[0] if scores[ranked[0]] and scores[ranked[0]] - scores[ranked[1]] >= 2 else "generic"
    reason = f"route.first_chunk.{kind}" if kind != "generic" else "route.fallback.unknown_or_ambiguous"
    return DocumentRoute(kind=kind, source="chunks" if kind != "generic" else "fallback",
                         scores=tuple(scores.items()), reasons=(reason,))


def _catalog_text(plan: ExtractionPlan, *, include_comments: bool = False) -> str:
    lines: list[str] = []
    for type_id in sorted(plan.allowed_types):
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


def _feature_text(plan: ExtractionPlan) -> str:
    lines: list[str] = []
    for type_id in sorted(plan.allowed_types):
        intrinsic = [spec for spec in plan.feature_specs.get(type_id, ()) if spec.kind == "intrinsic"]
        if not intrinsic:
            continue
        lines.append(f"- `{type_id}`")
        for spec in intrinsic:
            lines.append(f"  - `{spec.identifier}` ({spec.datatype})")
    return "\n".join(lines) if lines else "（无 intrinsic feature）"


def _signature_text(plan: ExtractionPlan) -> str:
    lines = sorted(
        {
            f"`{spec.source}` -[`{spec.relation_id}`]-> `{spec.target}`"
            for specs in plan.relation_specs.values()
            for spec in specs
            if spec.source in plan.allowed_types and spec.target in plan.allowed_types
        }
    )
    return "\n".join(lines) if lines else "（无关系签名）"


def _known_entities_payload(
    known_entities: list[dict[str, Any]] | None, *, task: str,
) -> list[dict[str, Any]]:
    """Preserve the old catalog fields and repair budget in structured form."""
    entries = known_entities or []
    fields = ("id", "type", "name", "properties", "missing_properties")
    if task == "repair":
        entries = entries[:25]
        fields = fields[:-1]
    return [{key: entry[key] for key in fields if key in entry} for entry in entries]


def _chunk_payload(chunk: ExtractionChunk, *, task: str) -> dict[str, Any]:
    if task == "repair":
        return {"chunk_id": chunk.chunk_id, "text": (chunk.text or "")[:8000]}
    return chunk.to_dict()


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
    task: Literal["entity", "property", "relation", "repair"] | str,
    plan: ExtractionPlan,
    route: DocumentRoute,
    chunk: ExtractionChunk,
    local_context: str = "",
    known_entities: list[dict[str, Any]] | None = None,
    output_schema: dict[str, Any] | None = None,
    repair_issues: list[dict[str, Any]] | None = None,
    relation_pairs: list[dict[str, Any]] | None = None,
    assembly_planner: Any | None = None,
) -> ChatMessages:
    entity_task = task == "entity" or (
        task == "repair" and "entities" in (output_schema or {}).get("properties", {})
    )
    if output_schema is None:
        if entity_task:
            output_schema = plan.output_schemas["entity_candidates"]
        elif task == "relation" and relation_pairs is None:
            output_schema = plan.output_schemas["relation_candidates"]
        else:
            raise ValueError(f"output_schema is required for {task}")
    if task == "repair":
        discipline = "只修复列出的 verifier issues；不得重复已接受事实，不得处理未列问题。"
    elif entity_task:
        discipline = "只提议实体候选，禁止输出关系。"
    elif task == "property":
        discipline = "只为给定的已接受实体补全缺失属性；实体集合、实体类型和关系集合均已冻结，不得创建、删除或重命名实体，不得输出关系。"
    elif task == "relation":
        discipline = "只分类给定 relation pair；不得创建新端点、不得输出未列出的关系。" if relation_pairs is not None else "只抽取已知实体之间、本体签名允许且原文支持的关系。"
    else:
        raise ValueError(f"Unsupported extraction task: {task}")

    record_table = _looks_like_record_table(chunk)
    ontology_section = _type_catalog_text(plan) if entity_task else _catalog_text(plan, include_comments=record_table)
    feature_section = "（实体任务不展示属性目录）" if entity_task else _feature_text(plan)
    relation_section = "（实体任务不展示关系目录）" if entity_task else _signature_text(plan)
    known = [] if entity_task else _known_entities_payload(known_entities, task=task)
    if task == "property":
        relation_section = "（属性补全不开放关系目录）"
    paired = task == "relation" and relation_pairs is not None
    if paired:
        ontology_section = "候选 relation pair（只能选择这些 pair 的 relation_id 或 NONE）"
        feature_section = "（关系 pair 分类不展示属性目录）"
        relation_section = "（关系 pair 分类不开放关系目录）"
        known = []

    contract = build_output_contract(output_schema, entity_task=entity_task,
                                     task=task, paired_relations=paired)
    payload = dict(
        task={
            "operation": task,
            "discipline": discipline,
            "input_scope": "provided_segments",
            "known_entities": known,
            "relation_pairs": relation_pairs if paired else [],
            "repair_issues": repair_issues or [],
        },
        output_contract=contract,
        domain_policy={"route": route.to_dict()} if assembly_planner is not None else select_domain_policy(
            kind=route.kind, route=route.to_dict(),
            task="entity" if entity_task else task,
        ),
        ontology={
            "format": "compiled_task_catalog",
            "ontology_id": plan.ontology_id,
            "catalog": ontology_section,
            "feature_catalog": feature_section,
            "relation_signatures": relation_section,
        },
        documents=[{
            "current_chunk": _chunk_payload(chunk, task=task),
            "local_context": "" if entity_task else local_context,
        }],
    )
    if assembly_planner is not None:
        payload = assembly_planner.assemble(payload, stage="entity" if entity_task else task)
    user = render_user(**payload)
    return [
        {"role": "system", "content": BASE_SYSTEM_PROMPT},
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
