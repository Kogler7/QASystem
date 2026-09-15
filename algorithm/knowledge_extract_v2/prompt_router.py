"""Canonical chat prompt builder and deterministic document routing."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from .chunk_context import ExtractionChunk
from .graph_utils import canonical_name
from .ontology_compiler import ExtractionPlan

RouteKind = Literal["generic", "official_document", "standard"]
RouteSource = Literal["ontology", "chunks", "fallback"]


class ChatMessage(TypedDict):
    role: Literal["system", "user"]
    content: str


ChatMessages = list[ChatMessage]


BASE_SYSTEM_PROMPT = """KNOWLEDGE_EXTRACT_V2_BASE_SYSTEM_PROMPT v1
你是 ontology-bound 知识图谱抽取器。只能依据已编译 ontology 与当前上游 chunk/局部上下文抽取事实。
必须遵守：只使用 ontology 中允许的 type/property/relation；每个事实必须有当前 chunk 的逐字 evidence quote；
禁止补写常识、禁止猜测缺失字段、禁止扩大 schema；输出必须是 JSON object，且只能包含本轮 schema 允许的顶层键。"""

OFFICIAL_DOCUMENT_PROMPT_EXTENSION = """OFFICIAL_DOCUMENT_PROMPT_EXTENSION v4
领域表达提示：若文本是公文/通知/意见/办法/方案，重点识别文件标题、文号、发文/来文单位、主送/抄送、正文条目、附件说明、办理动作、约束和交换对象。
这些提示只帮助映射 ontology 已声明字段；不得新增实体类型、关系、字段或无证据事实。

公文抽取精度规则：
- 一份公文只保留一个 OfficialDocument 实体，mention 使用“文件标题”；文号、收文号、来文字号等是属性/证据，不要作为单独的 OfficialDocument 或 OperationalInformation 实体。
- 正文中的“正文开头段落、一、二、三、四、五等条目、报送要求段落、附件列表段落”都应优先抽取为 DocumentUnit，而不是拆成多个 OperationalActivity。
- 公文中明确提到的附件、材料、反馈表、报告等信息对象应抽取为 OperationalInformation，例如“附件1 六要六不要建议”“附件2 排查反馈表”“排查情况反馈信息”。
- 发布通知、报送反馈、安全事件报告等明确传递/报送行为应抽取为 OperationalExchange。
- 公文中一条工作事项通常应聚合成一个高层级 OperationalActivity，不要把一个动作句的状语、否定词、结果短语拆成多个活动；例如“组织开展开源智能体专项排查”“落实办公网与互联网隔离”“高等级网络系统部署管控”“其他网络系统风险防范”“安全监测与事件处置”“报送排查反馈表”应各为一个活动，而不是“向集团报告”“第一时间处置”“梳理本企业使用开源智能体情况”等互相拆分。
- 包含“不得/必须/未经……不得/严禁/禁止”等要求的表述优先作为 OperationalConstraint，不应把其中的动作短语拆成 OperationalActivity。
- 不要把风险描述中的“病毒”“内部系统”“邮箱”等普通名词都抽成 OperationalObject；只有公文中明确作为治理/建设/约束/交换对象的业务对象才需要抽取。

公文表单/收文处理单规则：
- “收文处理单”“表单”“审批单”等公文登记/流转材料不是 DocumentUnit、OperationalAgent 或独立业务实体，它只是同一份公文的属性与处理信息载体。
- 表单中的字段（收文号、来文字号、来文单位、收文日期、收文部门、拟办意见、领导批示、联系电话、主送、抄送、主办部门等）应作为对应 OfficialDocument 或相关主体的属性/证据，不应为字段名或字段值单独建实体。
- 不要在审批流转记录中为每个“已阅/批示/签名”的人名单独创建 OperationalAgent，除非该人承担了 ontology 中明确要求抽取的主体职责。
- 不要把拟办意见、领导批示、审批意见或流转记录中的每个句子分别建为 OperationalActivity/OperationalConstraint/OperationalExchange；只有正文中明确的工作任务、约束、交互才需要抽取。
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


def _catalog_text(plan: ExtractionPlan) -> str:
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
            lines.append(f"  - `{prop.display_name}` -> `{prop.identifier}` ({prop.data_type})")
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


def _known_entities_text(known_entities: list[dict[str, Any]] | None) -> str:
    if not known_entities:
        return "（无）"
    lines = [
        "| id | type | name | properties | missing_properties |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in known_entities:
        properties = ", ".join(
            f"{key}={value}" for key, value in dict(entry.get("properties") or {}).items()
        )
        missing = ", ".join(
            f"{key}({dict(value).get('display_name', key)}:{dict(value).get('data_type', 'string')})"
            if isinstance(value, dict)
            else str(key)
            for key, value in dict(entry.get("missing_properties") or {}).items()
        )
        lines.append(
            f"| `{entry.get('id', '')}` | `{entry.get('type', '')}` | {entry.get('name', '')} | "
            f"{properties or '-'} | {missing or '-'} |"
        )
    return "\n".join(lines)


def _chunk_text(chunk: ExtractionChunk) -> str:
    return json.dumps(chunk.to_dict(), ensure_ascii=False, sort_keys=True, default=str)

_OFFICIAL_FORM_RULES = """
### 当前 chunk 是公文表单/收文处理单，必须执行以下特殊约束
- 该 chunk 不是公文正文，不要抽取 DocumentUnit。
- 不要为“已阅/批示/签名”的人员创建 OperationalAgent；这些名字只是审批流转记录，不是主体。
- 不要为拟办意见、领导批示、审批意见中的句子创建 OperationalActivity、OperationalConstraint 或 OperationalExchange。
- 表单中的字段标签（收文号、来文字号、来文单位、收文日期、联系电话、拟办意见、领导批示、主送、抄送等）不是实体，也不要为字段值单独建实体。
- 当前表单文本在实体任务中最多输出 1 个实体：与公文标题对应的 OfficialDocument mention；只有该表单中存在明确承担主体职责、且必须有对应实体的极少数内容时，才可额外输出。
- 如果没有可靠公文标题 mention，输出空数组。"""


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
        discipline = "只分类给定 relation pair；不得创建新端点、不得输出未列出的关系。"
        top_level = '{"decisions": []}' if relation_pairs is not None else '{"relations": []}'
    else:
        discipline = "只修复列出的 verifier issues；不得重复已接受事实，不得处理未列问题。"
        top_level = "按本轮 schema 输出 JSON object"

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
- mention 必须是 current chunk.text 中逐字连续的原文片段；start_char/end_char 是 chunk-relative、左闭右开区间。
- 数值、单位、标量属性值、字段标签和修饰词不是独立实体；同一 type + 完全相同 mention 每个 chunk 只输出一次。
- 不得从 local_context 或 ontology 属性名推导实体；没有明确原文 mention 时输出空数组。"""
        if _looks_like_official_form(chunk):
            entity_rules += _OFFICIAL_FORM_RULES
    else:
        ontology_section = _catalog_text(plan)
        feature_section = _feature_text(plan)
        relation_section = _signature_text(plan)
        known_section = _known_entities_text(known_entities)
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

    if task == "relation" and relation_pairs is not None:
        ontology_section = "候选 relation pair（只能选择这些 pair 的 relation_id 或 NONE）\n" + "\n".join(
            f"- `{pair['pair_id']}`: {pair['source_id']}({pair['source_type']}) -[{pair['relation_id']}]-> {pair['target_id']}({pair['target_type']})"
            for pair in relation_pairs
        )
        feature_section = "（关系 pair 分类不展示属性目录）"
        relation_section = "（关系 pair 分类不开放关系目录）"
        known_section = "（关系端点已由候选 pair 固定）"
        context_section = local_context or "（无）"
        entity_rules = """
- 每项只能包含 pair_id、label、evidence.quote；label 只能是该 pair 的 relation_id 或 NONE。
- NONE 表示当前 chunk 没有足够证据，NONE 不产生关系。
- pair_id 必须来自候选 pair；禁止输出新端点、新 pair 或其他关系。
- evidence.quote 必须是 current chunk.text 中逐字连续的关系证据；没有证据时选择 NONE。"""

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

## known entities
{known_section}

## current chunk
{_chunk_text(chunk)}

## local context
{context_section}

## repair issues
{issue_text}

## output schema
{json.dumps(output_schema or {}, ensure_ascii=False, sort_keys=True, default=str)}

## output constraints
- 只输出 JSON object；没有结果时输出 {top_level}。
- evidence.quote 必须逐字来自 current chunk.text。
 - type/property/relation 只能使用 ontology catalog/signatures 中的稳定标识。
{entity_rules}"""
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
    "resolve_document_route",
]
