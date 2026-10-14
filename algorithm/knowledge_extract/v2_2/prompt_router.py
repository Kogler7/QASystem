"""Canonical chat prompt builder and deterministic document routing."""
from __future__ import annotations

from algorithm.knowledge_extract.repair_scope import relation_repair_scope

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from .chunk_context import ExtractionChunk
from .structured_text import is_structured_payload
from .graph_utils import canonical_name
from .ontology_compiler import ExtractionPlan
from .relation_resolver import pair_relation_ids

RouteKind = Literal["generic", "official_document", "standard"]
RouteSource = Literal["ontology", "chunks", "fallback"]


class ChatMessage(TypedDict):
    role: Literal["system", "user"]
    content: str


ChatMessages = list[ChatMessage]


BASE_SYSTEM_PROMPT = """KNOWLEDGE_EXTRACT_V2_BASE_SYSTEM_PROMPT v4
你是 ontology-bound 知识图谱抽取器。只能依据已编译 ontology 与当前上游 chunk/局部上下文抽取事实。
只使用 ontology 中允许的 type/property/relation；禁止扩大 schema；输出必须是 JSON object，且只能包含本轮 schema 允许的顶层键。

[核心原则]
- 所有提取必须基于原文证据，不得凭空编造；但允许填写 ontology 已声明、且有公认标准编码表（如 ICS/CCS 分类）支撑的规范值。
- mention 可以是原文中的连续字符串，也可以是对原文的忠实归纳（例如由多条记录归纳出的规则名）。
- evidence.quote 尽量逐字引用原文；无法逐字对应时，给出最相关的原文片段作为来源，不得编造与原文无关的内容。
- 在忠实原文的前提下尽量抽全：原文中符合 ontology 的事实不要因过度保守而漏抽，也不得臆造。

[实体提取规则]
- 优先提取显式命名的实体。
- 避免抽象概念（除非 ontology 中定义）；原文以集合名词指代且 ontology 允许的对象（如“各相关单位”“办公设备”）应保留。
- 同类型实体的 mention 长度应合理（避免截断或过度包含）。
- 原文中并列出现的同类实体（如起草单位、附件、主送/收文单位列表）必须逐一抽取，不得只取首个或合并成一个。
- 数值、单位、标量属性值、字段标签和修饰词不是独立实体。
- 同一 type + 完全相同 mention 的实体，每个 chunk 只输出一次。

[关系提取规则]
- 只提取原文明确表达的关系。
- 避免推测性关系，不得虚构端点。
- 关系必须符合 ontology 声明的签名。
- 结构/证据类关系（如 HAS_PART、EVIDENCED_BY、sourceAgent、targetAgent）在 ontology 签名允许时应输出。

[属性提取规则]
- 字面属性（编号、名称、日期、数值、单位、原文引文等）必须逐字或按本体规范化值来自原文。
- 描述、目的、职责、范围、类别、预期结果等语义属性可忠实归纳，但不得引入证据之外的新事实。
- ICS/CCS 分类名称等公认标准编码值可依据规范编码表补全，但不得臆造编码或分类名。
- 避免填充原文不支持的属性；原文确实为空/缺失时按本轮 schema 允许的空值表达，不要臆造。
- 枚举属性多项时尽量全部提取。
- 禁止补写常识、禁止猜测缺失字段；无证据时不得输出该事实。
"""

OFFICIAL_DOCUMENT_PROMPT_EXTENSION = """OFFICIAL_DOCUMENT_PROMPT_EXTENSION v6
领域表达提示：若文本是公文/通知/意见/办法/方案，重点识别文件标题、文号、发文/来文单位、主送/抄送、正文条目、附件说明、办理动作、约束和交换对象。
这些提示只帮助映射 ontology 已声明字段；不得新增实体类型、关系、字段或无证据事实。

公文抽取精度规则：
- 一份公文只保留一个 OfficialDocument 实体，mention 使用“文件标题”；文号、收文号、来文字号等是属性/证据，不要作为单独的 OfficialDocument 或 OperationalInformation 实体。
- 正文中的“正文开头段落、一、二、三、四、五等条目、报送要求段落、附件列表段落”都应优先抽取为 DocumentUnit，而不是拆成多个 OperationalActivity。
- 公文中明确提到的附件、材料、反馈表、报告等信息对象应抽取为 OperationalInformation，例如“附件1 六要六不要建议”“附件2 排查反馈表”“排查情况反馈信息”。
- 发布通知、报送反馈、安全事件报告等明确传递/报送行为应抽取为 OperationalExchange。
- OperationalActivity 以“动宾工作事项”为粒度：同一动宾短语连同其状语、否定词、结果短语聚合成一个活动；但原文中并列出现的多个动宾事项必须分别抽取，不得只取其中一个或强行合并。
  · 例如“组织开展开源智能体专项排查”“落实办公网与互联网隔离”“高等级网络系统部署管控”“其他网络系统风险防范”“安全监测与事件处置”“报送排查反馈表”各为一个活动。
  · 又如“……按照任务分工进行分解落实、健全项目管理机制、加强资源投入、确保按计划完成项目实施、打造人工智能高质量数据集建设标杆示范”这类并列动宾，应各自抽为一个活动。
  · 不要把“向集团报告”“第一时间处置”“梳理本企业使用开源智能体情况”这类同一动宾事项的状语/结果短语再拆成独立活动。
- 包含“不得/必须/未经……不得/严禁/禁止”等要求的表述优先作为 OperationalConstraint，不应把其中的动作短语拆成 OperationalActivity。约束原文要保留条件和禁止语义，例如“未经论证和测试、未经批准，不得部署”。
- 区分执行主体、接收部门和联系人：报送、报告的接收方是 targetAgent 或接收部门，不要写成执行该活动的主体；联系人不是运行主体，除非原文明确赋责。
- “各企业”等指代在证据支持时对应通知的适用单位，不要另造一个无原文的组织。
- 不要把风险描述中的“病毒”“内部系统”“邮箱”等普通名词都抽成 OperationalObject；只有公文中明确作为治理/建设/约束/交换对象的业务对象才需要抽取。

公文表单/收文处理单规则：
- “收文处理单”“表单”“审批单”等公文登记/流转材料本身不是 DocumentUnit 或独立业务实体，它是同一份公文的属性与处理信息载体；但表单文字中明确承担职责的部门/单位，以及明确的工作事项、要求、报送行为仍应正常抽取为 operational 实体。
- 表单中的字段（收文号、来文字号、来文单位、收文日期、收文部门、拟办意见、领导批示、联系电话、主送、抄送、主办部门等）应作为对应 OfficialDocument 或相关主体的属性/证据，不应为字段名或字段值单独建实体。
- 不要在审批流转记录中为每个“已阅/批示/签名”的人名单独创建 OperationalAgent，除非该人承担了 ontology 中明确要求抽取的主体职责。
- 拟办意见、领导批示中明确承担职责的部门/单位/角色，以及明确的动宾工作事项、要求或报送/通知/反馈行为，应分别抽取为 OperationalAgent / OperationalActivity / OperationalConstraint / OperationalExchange；“已阅/签名”的个人姓名和纯流转记录不抽。
- 遇到表单时优先抑制低价值候选：姓名、日期、联系方式、部门名单独出现但无明确主体职责时，不应成为实体。"""

STANDARD_PROMPT_EXTENSION = """STANDARD_PROMPT_EXTENSION v2
领域表达提示：若文本是标准/规范，重点识别标准号、标准标题、章条编号、要求/试验/检验条款、引用标准、约束指标和适用对象。
这些提示只帮助映射 ontology 已声明字段；不得新增实体类型、关系、字段或无证据事实。

标准抽取精度规则：
- 机构角色优先使用最具体的类型：发布机构 Publishing Organization、提出机构 Proposing Organization、归口部门 CompetentDepartment、起草单位 Drafting Organization、组织实施单位 Implementing Organization 等；不要额外创建父类 Organization 来表达同一个机构。
- 一个标准只保留一个 Standard 实体，不要将标准编号、标题或简称拆成多个 Standard。
- 标准整体对象使用 StandardizationObject（例如“电动汽车用动力蓄电池单体”）；不要在条款中为同一对象的简称/别名另建 Subject of Standardization。
- 若文本中的“电池单体”“锂离子电池单体”“金属氢化物镍电池单体”等只是同一标准化对象在不同条款中的表达，应复用已有 StandardizationObject，不要创建冗余对象实体。
- Clause、Subclause、Section、Annex 等结构实体只按实际标题层级创建，不要同时用多个结构类型重复表达同一个标题。
- 不要因为条款中出现了多个约束数值，就把同一条要求拆成多个互相独立的 Constrain；先合并为同一特性的约束，再在属性中记录最大值/最小值/单位。"""


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
    prompt_extension: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "scores": [list(item) for item in self.scores],
            "reasons": list(self.reasons),
        }


def _score_rows(scores: dict[str, int]) -> tuple[tuple[str, int], tuple[str, int]]:
    return tuple((kind, int(scores.get(kind, 0))) for kind in _DOMAIN_ORDER)  # type: ignore[return-value]


def _route_extension(kind: RouteKind) -> str:
    if kind == "official_document":
        return OFFICIAL_DOCUMENT_PROMPT_EXTENSION
    if kind == "standard":
        return STANDARD_PROMPT_EXTENSION
    return ""


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
        prompt_extension=_route_extension(kind),
    )


def _catalog_text(plan: ExtractionPlan, *, include_comments: bool = False) -> str:
    lines: list[str] = []
    for type_id in sorted(plan.allowed_types):
        spec = plan.class_specs.get(type_id)
        if spec is None:
            continue
        line = f"- `{spec.display_name}` -> type `{type_id}`"
        if spec.identity_properties:
            line += f"；identity: {', '.join(spec.identity_properties)}"
        class_comment = " ".join((getattr(spec, "comment", "") or "").split())
        if class_comment:
            line += f" // {class_comment[:500]}"
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
            line = f"- `{spec.display_name}` -> type `{type_id}`"
            class_comment = " ".join((getattr(spec, "comment", "") or "").split())
            if class_comment:
                line += f" // {class_comment[:500]}"
            lines.append(line)
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
    seen: set[str] = set()
    lines: list[str] = []
    for specs in plan.relation_specs.values():
        for spec in specs:
            if spec.source not in plan.allowed_types or spec.target not in plan.allowed_types:
                continue
            text = f"`{spec.source}` -[`{spec.relation_id}`]-> `{spec.target}`"
            comment = " ".join((getattr(spec, "comment", "") or "").split())
            if comment:
                text += f" // {comment[:500]}"
            if text not in seen:
                seen.add(text)
                lines.append(text)
    lines.sort()
    return "\n".join(lines) if lines else "（无关系签名）"


def _known_entities_text(
    known_entities: list[dict[str, Any]] | None,
    *,
    task: str = "",
) -> str:
    if not known_entities:
        return "（无）"
    # repair 目录预算由调用方控制；渲染不得再次截断，避免与 schema/issue 作用域不一致。
    # missing_properties 由独立属性补全 pass 处理。
    repair = task == "repair"
    entries = known_entities
    lines = [
        "| id | type | name | properties | missing_properties |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        properties = ", ".join(
            f"{key}={value}" for key, value in dict(entry.get("properties") or {}).items()
        )
        missing = ", ".join(
            f"{key}({dict(value).get('display_name', key)}:{dict(value).get('data_type', 'string')})"
            if isinstance(value, dict)
            else str(key)
            for key, value in dict(entry.get("missing_properties") or {}).items()
        )
        if repair:
            lines.append(
                f"| `{entry.get('id', '')}` | `{entry.get('type', '')}` | {entry.get('name', '')} | "
                f"{properties or '-'} |"
            )
        else:
            lines.append(
                f"| `{entry.get('id', '')}` | `{entry.get('type', '')}` | {entry.get('name', '')} | "
                f"{properties or '-'} | {missing or '-'} |"
            )
    if repair and len(known_entities) > len(entries):
        lines.append(f"（共 {len(known_entities)} 个实体，repair 仅展示前 {len(entries)} 个）")
    return "\n".join(lines)


def _chunk_text(chunk: ExtractionChunk, *, task: str = "") -> str:
    if task == "repair":
        # repair 的上下文 chunk 只需要原文证据，全量 metadata JSON 体积大且无用。
        return json.dumps(
            {"chunk_id": chunk.chunk_id, "text": (chunk.text or "")[:8000]},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    return json.dumps(chunk.to_dict(), ensure_ascii=False, sort_keys=True, default=str)

_OFFICIAL_FORM_RULES = """
### 当前 chunk 是公文表单/收文处理单，必须执行以下特殊约束
- 该 chunk 不是公文正文，不要抽取 DocumentUnit。
- 表单字段标签（收文号、来文字号、来文单位、收文日期、联系电话、拟办意见、领导批示、主送、抄送、主办部门等）不是实体，也不要为纯字段值单独建实体。
- 不要为“已阅/签名”的个人姓名创建 OperationalAgent；审批流转记录中的个人不是主体。
- 但拟办意见/领导批示中明确承担职责的部门、单位、角色（如“技术中心”“规划运营部”“信息公司”“数科公司”）应抽取为 OperationalAgent，并在“职责”属性中归纳其被指派的职责。
- 拟办意见/领导批示中明确的动宾工作事项（如“明确工作承担部门与责任人”“将任务分工纳入部门重点工作”）应抽取为 OperationalActivity；明确的要求/限制抽取为 OperationalConstraint；明确的报送/通知/反馈行为抽取为 OperationalExchange。
- 上述抽取必须逐条对应表单原文并有证据，不得把同一句话拆成大量碎片，也不得臆造未出现的主体。
- 表单提供的公文标题/文号等元数据可作为对应 OfficialDocument 的属性/证据；没有可靠标题 mention 时也可以只输出上述 operational 实体。"""


_RECORD_TABLE_PROPERTY_RULES = """
### 当前 chunk 是结构化记录表，属性补全必须遵守以下特殊约束
- 同一规则/对象可能对应记录表中的多条记录（例如按分类字段归并）：语义属性必须综合该实体对应的全部记录进行归纳，不得只取第一条，也不得把多条记录逐条罗列成同义拼接。
- 语义属性不得直接复制实体名；同一实体的不同属性必须各自给出与其语义匹配的内容，不得多个属性输出完全相同的值。
- 条件类属性（identifier/display_name 含 antecedent、前件、前提、条件）：归纳规则成立所依赖的前提。
- 结果类属性（identifier/display_name 含 consequent、后件、结论、结果）：归纳前提成立时得到的结果。
- 连接类属性（identifier/display_name 含 connection、连接、符号）：给出前件到后件的连接词，如「则」「导致」「引起」。
- 模板类属性（identifier/display_name/注释含 template、模板、模版、表达式）：按「若<前提>，则<结果>」形式给出规则模板。
- 正文/描述类属性（identifier/display_name/注释含 body、正文、描述、说明、内容）：给出对规则语义的完整陈述（包含前提与结果），不得只写规则名或规则类型。
- 标识类属性（identifier/display_name/注释含 id、identifier、编号、唯一标识）：只有规则唯一对应单条记录标识时才填写；由多条记录归纳得到的规则不填该属性。
- 名称类属性（identifier/display_name 含 name、名称、title）：直接使用实体名或原文中的规范短名，不得扩写成长句。
- 语义/模板/前件/后件属性必须包含原文的关键限定（对象、地点、数值、处置等），做完整归纳；不得过度简化成几个词。
- 规则模板按「若<前件>，则<后件>。」形式输出，直接拼接 antecedent 与 consequent 的文本，不要使用尖括号或额外符号。
- 前件取规则的前提/触发条件，后件取由它引出的结果、诊断原因或处置结论；原文以「现象，原因是 X」表述时，前件=现象、后件=X；原文以「X 导致 Y」表述时，前件=X、后件=Y。
- 必须为 known entities 中列出的每个 missing property 输出更新；确实无法确定的语义属性也要按原文做忠实归纳，不得直接复制实体名。"""


_RECORD_TABLE_ENTITY_RULES = """
### 当前 chunk 是结构化记录表（JSON/表格形式的键值记录列表），必须执行以下特殊约束
- 结构化记录/表格不是「没有实体」的理由：应按记录行归纳出 ontology 允许的实体（如规则、事件、对象、条款），不得因为「内容都是字段标签和标量值」就直接输出空数组。
- 若记录表有分类/类型/类别字段（如「故障详细分类」「类别」「类型」），实体 mention 直接使用该字段的值本身（如「RTU」「对标不准」），不要添加「故障」「规则」等后缀，也不要改写成描述性名称。
- 同一语义的规则/对象（同一分类或同一规则下的多条记录）只输出一个实体；按 ontology 的 identity/名称语义归并，不要为同一条规则的每条原始记录重复创建实体。
- 抽全优先：只要 current chunk 中存在可承载 ontology 类型的业务对象，必须逐条或按语义归纳给出候选；只有整个 chunk 与 ontology 完全无关时，才允许输出空数组。"""


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


def _looks_like_official_form(chunk: ExtractionChunk) -> bool:
    """识别公文收文处理单/表单 chunk，抑制审批流转记录造成的实体爆炸。"""
    text_head = (chunk.text or "")[:600]
    name_source = " ".join(
        item
        for item in (
            chunk.document_id or "",
            str((chunk.metadata or {}).get("source_file") or ""),
        )
        if item
    )
    if "表单" not in name_source and "表单" not in text_head and "收文处理单" not in text_head:
        return False
    form_markers = ("收文处理单", "拟办意见", "领导批示", "已阅", "来文单位", "来文字号")
    return any(marker in text_head for marker in form_markers)



def _clip_pair_name(value: Any, limit: int = 80) -> str:
    return " ".join(str(value or "").split())[:limit]


def format_relation_pair_line(pair: dict[str, Any]) -> str:
    """One ontology-screened entity pair, with the labels it may receive."""
    relation_ids = pair_relation_ids(pair)
    label = relation_ids[0] if len(relation_ids) == 1 else "|".join(relation_ids)
    line = (
        f"- `{pair['pair_id']}`: {pair['source_id']}({pair['source_type']}) "
        f"-[{label}]-> {pair['target_id']}({pair['target_type']})"
    )
    source_name = _clip_pair_name(pair.get("source_name"))
    target_name = _clip_pair_name(pair.get("target_name"))
    if source_name or target_name:
        line += f" 「{source_name}」→「{target_name}」"
    source_mentions = [
        _clip_pair_name(item)
        for item in (pair.get("source_mentions") or [])
        if _clip_pair_name(item) and _clip_pair_name(item) != source_name
    ]
    target_mentions = [
        _clip_pair_name(item)
        for item in (pair.get("target_mentions") or [])
        if _clip_pair_name(item) and _clip_pair_name(item) != target_name
    ]
    if source_mentions or target_mentions:
        line += " mention「" + "、".join(source_mentions[:2]) + "」→「" + "、".join(target_mentions[:2]) + "」"
    context_labels = {
        "anchor": "文档锚点",
        "same_sentence": "同句",
        "same_unit": "同结构单元",
        "adjacent_unit": "相邻结构单元",
        "same_paragraph": "同段",
        "adjacent_paragraph": "相邻段",
    }
    context = context_labels.get(str(pair.get("context_reason") or ""))
    if context:
        line += f" 上下文={context}"
    comments = pair.get("relation_comments") if isinstance(pair.get("relation_comments"), dict) else {}
    glosses = [
        f"{relation_id}={comments[relation_id]}"
        for relation_id in relation_ids
        if comments.get(relation_id)
    ]
    if glosses:
        line += " （" + "；".join(glosses) + "）"
    return line


def build_chat_messages(
    *,
    task: Literal["entity", "relation", "repair"] | str,
    plan: ExtractionPlan,
    route: DocumentRoute,
    chunk: ExtractionChunk,
    local_context: str = "",
    known_entities: list[dict[str, Any]] | None = None,
    output_schema: dict[str, Any] | None = None,
    repair_issues: list[dict[str, Any]] | None = None,
    relation_pairs: list[dict[str, Any]] | None = None,
) -> ChatMessages:
    system = BASE_SYSTEM_PROMPT
    if route.prompt_extension:
        system = f"{system}\n\n{route.prompt_extension}"

    span_entity_task = task == "entity" or (
        isinstance(output_schema, dict)
        and isinstance(output_schema.get("properties"), dict)
        and "entities" in output_schema["properties"]
    )
    if span_entity_task:
        discipline = "只提议实体候选，禁止输出关系。"
        top_level = '{"entities": []}'
    elif task == "property":
        discipline = (
            "只为给定的已接受实体补全缺失属性；实体集合、实体类型和关系集合均已冻结，"
            "不得创建、删除或重命名实体，不得输出关系。"
        )
        top_level = '{"updates": []}'
    elif task == "relation":
        discipline = "只判断本体已经允许的实体对；不得创建新端点，不得输出该对未列出的关系。"
        top_level = '{"decisions": []}' if relation_pairs is not None else '{"relations": []}'
    else:
        discipline = "只修复列出的 verifier issues；不得重复已接受事实，不得处理未列问题。"
        top_level = "按本轮 schema 输出 JSON object"

    repair_scope = None
    if task == "repair" and "relations" in (output_schema or {}).get("properties", {}):
        repair_scope = relation_repair_scope(plan, known_entities, repair_issues, output_schema)
        output_schema = repair_scope.schema
        repair_issues = repair_scope.issues

    record_table = _looks_like_record_table(chunk)
    if record_table:
        output_constraint = (
            "- 只输出 JSON object。不要用空数组敷衍：结构化记录/表格中每一行或每一条可归纳对象都应输出；"
            '只有确认 current chunk 中不存在任何符合 ontology 的对象时，才允许输出 {"entities": []}。'
        )
    else:
        output_constraint = f"- 只输出 JSON object；没有结果时输出 {top_level}。"

    issue_text = (
        json.dumps(repair_issues, ensure_ascii=False, sort_keys=True, default=str)
        if repair_issues
        else "（无）"
    )
    # Entity extraction is span-first: properties and canonical names are
    # resolved later by the graph pool.  Keep this prompt's entity branch
    # limited to the type directory so the model does not turn scalar values
    # or property labels into nodes.
    if span_entity_task:
        ontology_section = _type_catalog_text(plan)
        feature_section = "（实体任务不展示属性目录）"
        relation_section = "（实体任务不展示关系目录）"
        known_section = "（实体任务不使用已知实体目录）"
        context_section = "（实体任务禁止从 local_context 抽取；仅使用 current chunk）"
        entity_rules = """
- 每项只能包含 type、mention、start_char、end_char、evidence.quote；禁止 name、properties 或其他键。
- mention 可以是 current chunk.text 中逐字连续的原文片段，也可以是对原文的忠实归纳（例如由多条记录归纳出的规则名）。
- start_char/end_char 是 chunk-relative、左闭右开区间；mention 无法逐字定位时填 0，系统会按 evidence.quote 兜底。
- 数值、单位、标量属性值、字段标签和修饰词不是独立实体；同一 type + 完全相同 mention 每个 chunk 只输出一次。
- 仍要给出 evidence.quote 作为来源（优先逐字原文片段）；确实无法逐字对应时给出最相关的原文片段，不要输出空实体。"""
        if record_table:
            entity_rules += _RECORD_TABLE_ENTITY_RULES
        if _looks_like_official_form(chunk):
            entity_rules += _OFFICIAL_FORM_RULES
    else:
        ontology_section = _catalog_text(plan, include_comments=record_table)
        feature_section = _feature_text(plan)
        relation_section = _signature_text(plan)
        if repair_scope is not None:
            relation_section = repair_scope.signatures
        known_section = _known_entities_text(known_entities, task=task)
        context_section = local_context or "（无）"
        entity_rules = ""

    if task == "property":
        relation_section = "（属性补全不开放关系目录）"
        entity_rules = """
- entity_id 只能来自 known entities；properties 只能包含该实体 type 在 ontology 中声明且当前缺失的稳定属性标识。
- 不得输出 name/type/relation，不得把属性值、同义词、修饰语或短语变成实体。
- 原文中能定位到值的属性，必须从原文提取真实值，不能错误地输出空字符串。
- 标识、名称、编号、日期、数值、单位等字面属性必须能在 current chunk 中定位；禁止臆造流水号或默认值。
- 描述、定义、范围、类别等语义属性可以忠实归纳，但不得加入证据之外的新事实。
- 如果某个字符串属性确实在原文中为空/缺失/无法确定，允许保留该属性并输出空字符串 ""，不要删除该属性。
- 不要用 null 代替字符串属性；空字符串 "" 是允许的空值表达。
- 同一个实体的 updates 中，一个属性只能出现一次；如果属性已有值，不要重复输出。
- evidence.quote 必须是 current chunk.text 中逐字连续、足以支撑本次全部属性更新的原文。"""
        if record_table:
            entity_rules += _RECORD_TABLE_PROPERTY_RULES

    if task == "relation" and relation_pairs is not None:
        ontology_section = (
            "候选实体对（已按本体签名筛过；label 只能是该对方括号中的关系，或 NONE）\n"
            + "\n".join(format_relation_pair_line(pair) for pair in relation_pairs)
        )
        feature_section = "（关系 pair 分类不展示属性目录）"
        relation_section = "（关系 pair 分类不开放关系目录）"
        known_section = "（关系端点已由候选 pair 固定，名称写在「」中）"
        context_section = local_context or "（无）"
        entity_rules = """
- 每项只能包含 pair_id、label、evidence.quote。
- label 只能是该 pair 方括号里的关系标识；方括号中用 | 分隔时只能选其中一个。不能选其它 pair 的关系。
- 「」中是规范名称。若同一行给出 mention，用原文 mention 判断关系；规范名称没有逐字出现不是选择 NONE 的理由。
- 原文没有支持这对实体的对应关系时，label 必须是 NONE。
- NONE 不产生关系。pair_id 必须来自候选 pair；禁止输出新端点、新 pair 或其他关系。
- evidence.quote 必须是 current chunk.text 中的逐字连续片段。同句关系引用该句；跨句关系引用同时覆盖两侧证据位置的最短连续原文。找不到这样的原文时选择 NONE。"""

    user = f"""## task
{task}

## task discipline
{discipline}

## resolved route
kind={route.kind}; source={route.source}; reasons={', '.join(route.reasons)}

## ontology catalog
{ontology_section}

## ontology feature catalog
{feature_section}

## ontology relation signatures
{relation_section}

## output schema
{json.dumps(output_schema or {}, ensure_ascii=False, sort_keys=True, default=str)}

## output constraints
{output_constraint}
- evidence.quote 尽量逐字来自 current chunk.text；无法逐字对应时给出最相关的原文片段。
 - type/property/relation 只能使用 ontology catalog/signatures 中的稳定标识。
{entity_rules}

## known entities
{known_section}

## local context
{context_section}

## current chunk
{_chunk_text(chunk, task=task)}

## repair issues
{issue_text}"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_relation_task_messages(
    *,
    plan: ExtractionPlan,
    route: DocumentRoute,
    candidates: list[dict[str, Any]] | list[Any],
    fragments: list[Any],
    output_schema: dict[str, Any],
) -> ChatMessages:
    """关系小任务：只给出本批端点、一种或几种已固定的候选，以及这些候选的原文片段。"""
    system = BASE_SYSTEM_PROMPT
    if route.prompt_extension:
        system = f"{system}\n\n{route.prompt_extension}"
    candidate_lines = []
    for item in candidates:
        if isinstance(item, dict):
            candidate_id = item["candidate_id"]
            source_id = item["source_id"]
            source_type = item["source_type"]
            target_id = item["target_id"]
            target_type = item["target_type"]
            relation_key = item.get("relation_key") or item.get("relation_id")
            source_name = item.get("source_name") or ""
            target_name = item.get("target_name") or ""
            comment = item.get("comment") or ""
        else:
            candidate_id = item.candidate_id
            source_id = item.source_id
            source_type = item.source_type
            target_id = item.target_id
            target_type = item.target_type
            relation_key = item.relation_key
            source_name = item.source_name
            target_name = item.target_name
            comment = item.comment
        line = (
            f"- `{candidate_id}`: {source_id}({source_type}) "
            f"-[{relation_key}]-> {target_id}({target_type}) "
            f"「{_clip_pair_name(source_name)}」→「{_clip_pair_name(target_name)}」"
        )
        if comment:
            line += f" （{relation_key}={comment}）"
        candidate_lines.append(line)
    document_lines: list[str] = []
    for fragment in fragments:
        fragment_id = fragment["fragment_id"] if isinstance(fragment, dict) else fragment.fragment_id
        chunk_id = fragment["chunk_id"] if isinstance(fragment, dict) else fragment.chunk_id
        text = fragment["text"] if isinstance(fragment, dict) else fragment.text
        document_lines.append(f"- `{fragment_id}` chunk=`{chunk_id}`")
        document_lines.append(str(text))
    user = f"""## task
relation

## task discipline
只判断下面列出的候选关系。关系类型和两端实体已经固定，禁止创建新端点、新候选或未列出的关系。

## output contract
每个 candidate_id 必须返回一条决策，status 只能是 supported、not_supported 或 insufficient_evidence。
supported 的 evidence.quote 必须是本次 documents 中某个片段的逐字连续原文，并且能支持这条关系；不能只因为两个名称同时出现，也不能只粘贴两端名称而无关系表述（谓语、角色或作用说明）。
not_supported 表示这组证据不支持该候选，不是整篇文档证明该关系不存在；此时 evidence 用空数组。
insufficient_evidence 用于指代不清、引用目标不在本次片段中、条件无法确定。不要输出推理过程。

## ontology catalog
{chr(10).join(candidate_lines) if candidate_lines else "（无候选）"}

## ontology relation signatures
（候选行已经给出关系标识和含义；不要改用其它关系）

## output schema
{json.dumps(output_schema, ensure_ascii=False, sort_keys=True)}

## documents
{chr(10).join(document_lines) if document_lines else "（无）"}

## resolved route
kind={route.kind}; ontology={getattr(plan, "ontology_id", "")}
"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


__all__ = [
    "BASE_SYSTEM_PROMPT",
    "OFFICIAL_DOCUMENT_PROMPT_EXTENSION",
    "STANDARD_PROMPT_EXTENSION",
    "ChatMessage",
    "ChatMessages",
    "DocumentRoute",
    "build_chat_messages",
    "build_relation_task_messages",
    "resolve_document_route",
]
