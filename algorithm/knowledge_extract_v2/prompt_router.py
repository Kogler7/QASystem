"""Canonical chat prompt builder and deterministic document routing."""
from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, TypedDict

from .chunk_context import ExtractionChunk
from .structured_text import _BATCH_HEADER, has_json_table_payload, is_structured_payload
from .graph_utils import canonical_name
from .ontology_compiler import ExtractionPlan
from .table_semantics import analyze_table_semantics, table_entity_types
from .official_document_scope import looks_like_official_form, official_entity_types

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
- 表单仅用于抽取一个 OfficialDocument（公文管理单元），mention 优先使用文件标题，缺失时可使用明确的收文号或来文字号；正文仅抽取结构单元和其他业务内容，不从正文新增公文管理单元。
- 正文中的“正文开头段落、一、二、三、四、五等条目、报送要求段落、附件列表段落”都应优先抽取为 DocumentUnit，而不是拆成多个 OperationalActivity。
- 正文中提到的材料、反馈表、报告等信息对象可按本体抽取；附件文件由系统保存为 OperationalInformation 容器，信息类型为附件，附件内容不参与知识抽取。
- 发布通知、报送反馈、安全事件报告等明确传递/报送行为应抽取为 OperationalExchange。
- OperationalActivity 以“动宾工作事项”为粒度：同一动宾短语连同其状语、否定词、结果短语聚合成一个活动；但原文中并列出现的多个动宾事项必须分别抽取，不得只取其中一个或强行合并。
  · 例如“组织开展开源智能体专项排查”“落实办公网与互联网隔离”“高等级网络系统部署管控”“其他网络系统风险防范”“安全监测与事件处置”“报送排查反馈表”各为一个活动。
  · 又如“……按照任务分工进行分解落实、健全项目管理机制、加强资源投入、确保按计划完成项目实施、打造人工智能高质量数据集建设标杆示范”这类并列动宾，应各自抽为一个活动。
  · 不要把“向集团报告”“第一时间处置”“梳理本企业使用开源智能体情况”这类同一动宾事项的状语/结果短语再拆成独立活动。
- 包含“不得/必须/未经……不得/严禁/禁止”等要求的表述优先作为 OperationalConstraint，不应把其中的动作短语拆成 OperationalActivity。
- 不要把风险描述中的“病毒”“内部系统”“邮箱”等普通名词都抽成 OperationalObject；只有公文中明确作为治理/建设/约束/交换对象的业务对象才需要抽取。

公文表单/收文处理单规则：
- “收文处理单”“表单”“领导批示”不是文件标题。文件标题字段缺失时保持缺失，不用模板标题、编号或其他文件标题填充。
- 收文处理单、表单、审批单只提供公文管理单元属性。拟办意见、领导批示、收文号、来文单位、收文日期、主送、抄送等均保留为 OfficialDocument 的本体属性。
- 表单中即使出现明确的职责、任务、约束或报送行为，也不拆成 OperationalAgent / OperationalActivity / OperationalConstraint / OperationalExchange；不抽取 DocumentUnit。"""

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

#: 域强信号正则。路由只信强信号（精确率优先）：漏判退回 generic 是安全降级，
#: 误判才会污染领域提示词与确定性补全，因此不做词汇/条款等弱信号的分数累积；
#: 段内采信规则（文档头锚定/正文多命中阈值）见 _evaluate_segment。
_STANDARD_NUMBER = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"GB\s*/\s*[TZ]|GB|GJB|"
    r"JB\s*/\s*T|DB\s*/\s*T|DB|YY\s*/\s*T|HG\s*/\s*T|SY\s*/\s*T|NY\s*/\s*T|"
    r"JTG|CJJ|ISO|IEC|EN|ASTM|DIN|IEEE|RFC|W3C|IETF|ANSI|OGC|Q\s*/\s*CR|T\s*/\s*[A-Z]{2,8}"
    r")\s*(?:[A-Z]{1,4}\s*)?[—–-]?\s*\d+(?:[./—–-]\d+)*",
    re.IGNORECASE,
)
#: 公文标题形式：行首（可带 Markdown 标记与 ≤30 字符发文机关前缀）的 关于/印发/发布/颁布 + 公文后缀。
_OFFICIAL_TITLE = re.compile(
    r"^\s*(?:#{1,6}\s*)?[^\n#]{0,30}?(?:公司|集团|管理局|局|委员会|委|部|厅|办公室|中心|研究院|院|所|厂|站|政府)?\s*"
    r"(?:关于|印发|发布|颁布)[^\n]{2,60}?"
    r"(?:的通知|的公告|的决定|的意见|的办法|的批复|的请示|的报告|的函|的通报|的纪要)",
    re.MULTILINE,
)
#: 企业制度类标题（行级判定；含标准号的行由调用方排除）。
_OFFICIAL_ENTERPRISE_TITLE = re.compile(
    r"^\s*(?:#{1,6}\s*)?[^\n#]{2,50}?(?:管理办法|实施细则|实施方案|管理规定|工作制度|操作规程)\s*$"
)
_OFFICIAL_NUMBER = re.compile(r"〔\d{4}〕\d+号")

#: 信号匹配前先做轻量规范化：NFKC 折叠全角/兼容字符（全角数字、全角字母、／、－），
#: 再统一斜杠与破折号排版变体。国标排版常用 U+2215(∕)、U+2044(⁄) 作斜杠，
#: NFKC 不会折叠这两个字符，必须显式映射，否则 "GB∕T 12345" 整类漏判。
_SIGNAL_TEXT_TRANSLATION = str.maketrans(
    {"—": "-", "–": "-", "－": "-", "﹣": "-", "／": "/", "∕": "/", "⁄": "/"}
)


def _normalize_signal_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").translate(_SIGNAL_TEXT_TRANSLATION)


#: 域判别只看本体声明的类（属性/关系名跨域复用，如标准本体也有「签发机构」）。
_OFFICIAL_ONTOLOGY_CLASSES = frozenset(
    {
        "officialdocument",
        "documentunit",
        "operationalactivity",
        "operationalagent",
        "operationalconstraint",
        "operationalexchange",
        "operationalinformation",
        "operationalobject",
    }
)
_STANDARD_ONTOLOGY_CLASSES = frozenset(
    {
        "standarddocument",
        "standard",
        "requirementclause",
        "clause",
        "subclause",
        "section",
    }
)

_SOURCE_RANK: dict[RouteSource, int] = {"fallback": 0, "ontology": 1, "chunks": 2}

_SIGNAL_METADATA_KEYS = (
    "title",
    "heading",
    "section_title",
    "unit",
    "block_kind",
    "document_number",
    "standard_number",
)

ROUTER_VERSION = "route-v3"

@dataclass(frozen=True)
class DocumentRoute:
    kind: RouteKind
    source: RouteSource
    labels: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    prompt_extension: str = ""
    scores: dict[str, float] | None = None
    signals: tuple[str, ...] = ()
    decision: str = "generic"
    abstain_reason: str | None = None
    router_version: str = ROUTER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "labels": list(self.labels),
            "reasons": list(self.reasons),
            "scores": dict(self.scores or {}),
            "signals": list(self.signals),
            "decision": self.decision,
            "abstain_reason": self.abstain_reason,
            "router_version": self.router_version,
        }


def _route_extension(kind: RouteKind) -> str:
    if kind == "official_document":
        return OFFICIAL_DOCUMENT_PROMPT_EXTENSION
    if kind == "standard":
        return STANDARD_PROMPT_EXTENSION
    return ""


def _extensions_for_labels(labels: tuple[str, ...]) -> str:
    return "\n\n".join(_route_extension(kind) for kind in labels)


def _kind_for_labels(labels: tuple[str, ...]) -> RouteKind:
    if "official_document" in labels:
        return "official_document"
    if "standard" in labels:
        return "standard"
    return "generic"


def _ontology_class_tokens(plan: ExtractionPlan) -> set[str]:
    tokens: set[str] = set()
    for class_id, class_spec in plan.class_specs.items():
        tokens.add(canonical_name(class_id))
        tokens.add(canonical_name(class_spec.display_name))
    return {item for item in tokens if item}


def _ontology_domain_bias(plan: ExtractionPlan) -> RouteKind | None:
    """单域本体是可靠先验（只装了标准本体的人抽的就是标准）；dual/空本体无信息量。"""
    tokens = _ontology_class_tokens(plan)
    official = bool(tokens & _OFFICIAL_ONTOLOGY_CLASSES)
    standard = bool(tokens & _STANDARD_ONTOLOGY_CLASSES)
    if official == standard:
        return None
    return "official_document" if official else "standard"


def _has_standard_number(text: str) -> bool:
    return bool(_STANDARD_NUMBER.search(_normalize_signal_text(text)))


def _has_official_signal(text: str) -> bool:
    normalized = _normalize_signal_text(text)
    if _OFFICIAL_NUMBER.search(normalized) or _OFFICIAL_TITLE.search(normalized):
        return True
    return any(
        not _STANDARD_NUMBER.search(line) and _OFFICIAL_ENTERPRISE_TITLE.match(line)
        for line in normalized.splitlines()
    )


# Signal rules are data, so adding a high-precision signal does not require
# changing the segment decision algorithm.  Detector functions receive only
# sanitized signal text.
ROUTE_SIGNAL_RULES: tuple[dict[str, Any], ...] = (
    {"label": "standard", "detector": _has_standard_number,
     "reason": "route.chunks.standard_number"},
    {"label": "official_document", "detector": _has_official_signal,
     "reason": "route.chunks.official"},
)


def _chunk_signal_text(chunk: ExtractionChunk) -> str:
    metadata = chunk.metadata or {}
    metadata_source = str(metadata.get("signal_source", metadata.get("source", ""))).strip().lower()
    # Metadata is useful only when it comes from the parser/adapter.  Explicit
    # user/LLM supplied metadata must never be allowed to steer domain prompts.
    untrusted_source = metadata_source in {"user", "llm", "model", "untrusted", "external"}

    def safe_value(value: Any) -> str:
        if value is None or isinstance(value, (dict, list, tuple, set)):
            return ""
        value_text = str(value)
        if len(value_text) > 256 or any(unicodedata.category(char) == "Cc" for char in value_text):
            return ""
        return value_text.strip()

    selected_metadata = [
        safe_value(metadata[key])
        for key in _SIGNAL_METADATA_KEYS
        if not untrusted_source and metadata.get(key) is not None
    ]
    return "\n".join(
        item
        for item in (
            chunk.section_title or "",
            chunk.clause_number or "",
            "\n".join(selected_metadata),
            chunk.text or "",
        )
        if item
    )


def _is_batch_header(text: str) -> bool:
    return any(_BATCH_HEADER.match(line) for line in (text or "").splitlines())


def _has_mid_batch_header(text: str) -> bool:
    """A header after content makes one chunk span two logical documents.

    We cannot safely split an upstream ExtractionChunk here without changing
    evidence offsets, so the caller must abstain instead of silently routing
    the latter document using the former document's prefix.
    """
    seen_content = False
    for line in (text or "").splitlines():
        if _BATCH_HEADER.match(line):
            if seen_content:
                return True
            continue
        if line.strip():
            seen_content = True
    return False


def _typed_chunks(chunks: Any) -> list[ExtractionChunk]:
    if isinstance(chunks, (str, bytes, dict)):
        raise TypeError("route requires a typed ExtractionChunk sequence")
    resolved = list(chunks)
    if any(not isinstance(chunk, ExtractionChunk) for chunk in resolved):
        raise TypeError("route requires a typed ExtractionChunk sequence")
    return resolved


def _segment_chunks(chunks: list[ExtractionChunk]) -> list[list[ExtractionChunk]]:
    """按文档分段：document_id 变化或出现 "## 文件:" 批量头（可在 chunk 任意位置）。

    知识沉淀批量路径把所有文件拼成一份 markdown 后整批抽取，document_id 常常整批相同，
    批量头是唯一可靠的文件边界。
    """
    segments: list[list[ExtractionChunk]] = []
    for chunk in chunks:
        starts_new_segment = (
            not segments
            or chunk.document_id != segments[-1][-1].document_id
            or _is_batch_header(chunk.text)
        )
        if starts_new_segment:
            segments.append([chunk])
        else:
            segments[-1].append(chunk)
    return segments


#: 段内信号采信阈值。文档头（首个实质内容 chunk——空行、批量头 "## 文件:"、
#: 纯标题行都不算文档头）命中的强信号视为文档身份信号（封面标题/文号/标准号），
#: 直接采信；仅出现在正文后续 chunk 的信号视为可能的引用，需要至少
#: _SEGMENT_BODY_MIN_HITS 个 chunk 命中、且命中数不少于段内 chunk 总数的
#: _SEGMENT_BODY_HIT_RATIO，才采信，避免"一处引用污染整段"。
_SEGMENT_BODY_MIN_HITS = 2
_SEGMENT_BODY_HIT_RATIO = 0.5

_SCAFFOLDING_LINE = re.compile(r"^\s{0,3}#{1,6}\s")


def _is_scaffolding_only(text: str) -> bool:
    """整段只有空行、批量头或 Markdown 标题行——不承载文档身份，不能当锚点。

    500-700 字符切块下，"## 文件: x.md" 批量头常独占段首 chunk；若把它当文档头，
    封面 chunk 里的标准号/文号会落进"正文"分支，凑不满多命中阈值而被静默丢弃。
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return True
    return all(
        _BATCH_HEADER.match(line) or _SCAFFOLDING_LINE.match(line) for line in lines
    )


def _segment_signal_hits(
    segment: list[ExtractionChunk], detector: Callable[[str], bool]
) -> tuple[int, int, int]:
    """返回单个域信号的 (命中数, 首个命中 chunk 下标, 文档头锚点下标)。

    锚点取首个实质内容 chunk；整段只有脚手架时退回首个非空 chunk。
    """
    hits = 0
    first_hit = -1
    first_nonblank = -1
    first_content = -1
    for index, chunk in enumerate(segment):
        text = chunk.text or ""
        if text.strip():
            if first_nonblank < 0:
                first_nonblank = index
            if first_content < 0 and not _is_scaffolding_only(text):
                first_content = index
        if detector(_chunk_signal_text(chunk)):
            hits += 1
            if first_hit < 0:
                first_hit = index
    anchor = first_content if first_content >= 0 else first_nonblank
    return hits, first_hit, anchor


def _evaluate_segment(segment: list[ExtractionChunk], bias: RouteKind | None) -> DocumentRoute:
    if any(_has_mid_batch_header(chunk.text) for chunk in segment):
        return DocumentRoute(
            kind="generic", source="fallback",
            reasons=("route.segment.batch_header_mid_chunk",),
            signals=("batch_header_mid_chunk",), decision="ambiguous",
            abstain_reason="batch_header_mid_chunk_requires_upstream_resplit",
        )
    labels: set[str] = set()
    reasons: set[str] = set()
    scores: dict[str, float] = {}
    signals: set[str] = set()
    for rule in ROUTE_SIGNAL_RULES:
        label = str(rule["label"])
        detector = rule["detector"]
        reason = str(rule["reason"])
        hits, first_hit, anchor = _segment_signal_hits(segment, detector)
        if hits == 0:
            continue
        if first_hit <= anchor:
            labels.add(label)
            reasons.add(reason)
            signals.add(reason)
            scores[label] = 1.0
        elif hits >= _SEGMENT_BODY_MIN_HITS and hits >= math.ceil(
            _SEGMENT_BODY_HIT_RATIO * len(segment)
        ):
            labels.add(label)
            reasons.add(f"{reason}.corroborated")
            signals.add(f"{reason}.corroborated")
            scores[label] = 0.8
        else:
            reasons.add(f"{reason}.unconfirmed")
            signals.add(f"{reason}.unconfirmed")
            scores[label] = 0.0
    if labels:
        source: RouteSource = "chunks"
        decision, abstain_reason = "accepted", None
    elif bias is not None:
        labels = {bias}
        source = "ontology"
        reasons.add(f"route.preference.ontology.{bias}")
        signals.add(f"route.preference.ontology.{bias}")
        scores[bias] = 0.6
        decision, abstain_reason = "accepted", None
    else:
        source = "fallback"
        reasons.add("route.fallback.no_domain_signal")
        decision, abstain_reason = "generic", None
    ordered = tuple(kind for kind in _DOMAIN_ORDER if kind in labels)
    return DocumentRoute(
        kind=_kind_for_labels(ordered),
        source=source,
        labels=ordered,
        reasons=tuple(sorted(reasons)),
        prompt_extension=_extensions_for_labels(ordered),
        scores=scores,
        signals=tuple(sorted(signals)),
        decision=decision,
        abstain_reason=abstain_reason,
    )


def resolve_chunk_routes(
    plan: ExtractionPlan, chunks: Iterable[ExtractionChunk]
) -> dict[str, DocumentRoute]:
    """按文档段解析路由；每个 chunk 取本段路由（批量上传时各文件互不串扰）。"""
    resolved = _typed_chunks(chunks)
    bias = _ontology_domain_bias(plan)
    routes: dict[str, DocumentRoute] = {}
    for segment in _segment_chunks(resolved):
        route = _evaluate_segment(segment, bias)
        for chunk in segment:
            routes[chunk.chunk_id] = route
    return routes


def resolve_document_route(
    plan: ExtractionPlan, chunks: Iterable[ExtractionChunk]
) -> DocumentRoute:
    """整篇文档路由 = 各段聚合：labels 取并集，kind 按公文优先，source 取最高优先级。"""
    resolved = _typed_chunks(chunks)
    bias = _ontology_domain_bias(plan)
    labels: set[str] = set()
    reasons: set[str] = set()
    signals: set[str] = set()
    scores: dict[str, float] = {}
    abstain_reasons: set[str] = set()
    source: RouteSource = "fallback"
    for segment in _segment_chunks(resolved):
        route = _evaluate_segment(segment, bias)
        labels.update(route.labels)
        reasons.update(route.reasons)
        signals.update(route.signals)
        if route.abstain_reason:
            abstain_reasons.add(route.abstain_reason)
        for label, score in (route.scores or {}).items():
            scores[label] = max(scores.get(label, 0.0), score)
        if _SOURCE_RANK[route.source] > _SOURCE_RANK[source]:
            source = route.source
    ordered = tuple(kind for kind in _DOMAIN_ORDER if kind in labels)
    if not reasons:
        reasons.add("route.fallback.no_domain_signal")
    return DocumentRoute(
        kind=_kind_for_labels(ordered),
        source=source,
        labels=ordered,
        reasons=tuple(sorted(reasons)),
        prompt_extension=_extensions_for_labels(ordered),
        scores=scores,
        signals=tuple(sorted(signals)),
        decision="ambiguous" if abstain_reasons else ("accepted" if ordered else "generic"),
        abstain_reason=";".join(sorted(abstain_reasons)) or None,
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
        lines.append(line)
        for prop in spec.properties:
            suffix = ""
            if include_comments:
                comment = " ".join((prop.comment or "").split())
                suffix = f" // {comment[:200]}" if comment else ""
            lines.append(f"  - `{prop.display_name}` -> `{prop.identifier}` ({prop.data_type}){suffix}")
    return "\n".join(lines) if lines else "（无本体类目录）"


def _type_catalog_text(plan: ExtractionPlan, type_ids: Iterable[str] | None = None) -> str:
    """Minimal entity catalog; properties are resolved after span grounding."""
    lines = []
    for type_id in sorted(plan.allowed_types if type_ids is None else type_ids):
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
- 只抽取一个 OfficialDocument（公文管理单元），名称优先使用本表单的文件标题；标题缺失时可使用明确的收文号或来文字号。禁止抽取 DocumentUnit 或任何 operational 实体。
- 表单字段标签（收文号、来文字号、来文单位、收文日期、联系电话、拟办意见、领导批示、主送、抄送、主办部门等）不是实体，也不要为纯字段值单独建实体。
- 不要为“已阅/签名”的个人姓名创建 OperationalAgent；审批流转记录中的个人不是主体。
- 拟办意见/领导批示中出现的部门、职责、工作事项、限制及报送行为，均作为公文管理单元属性原文保存，不拆成业务实体。
- “收文处理单”“表单”“领导批示”是模板标题或字段标签，不是公文的文件标题。文件标题缺失时保持该属性缺失，不用模板标题、编号或其他文件的标题填充。
- 缺少可靠身份时不得用字段标签制造管理单元；仅返回本表有明确依据的候选。"""


_RECORD_TABLE_PROPERTY_RULES = """
### 通用表格属性约束
- JSON 只是表格的存储格式，不代表规则场景；按本体属性的实际语义读取原记录。
- 普通对象按其身份字段匹配记录，保留原始名称、编号、数值和单位；类别相同不代表同一对象。
- 仅补充与当前实体对应的记录支持的属性，不借用其他行或其他表的数据；缺失值保持缺失。
- 不得把设备台账、人员名单、参数或公文表单概括成条件规则，不生成本体未要求的模板。"""


_RECORD_TABLE_ENTITY_RULES = """
### 通用表格实体约束
- JSON 只是表格的存储格式；按 ontology 抽取设备、人员、事件、参数所属对象、表单事项等真实对象。
- 普通对象 mention 使用原始名称；编号用于区分身份，分类/类型/类别仅是属性，禁止用类别代替对象名称。
- 不同身份的记录必须分别保留，禁止仅因同类而合并；只有同一真实对象的重复记录才可合并。
- 不把字段标签、空单元格和纯标量当成实体；遵循当前来源角色允许的类型。
- 规则归纳只有在下面的自动判定明确允许时才能启用；否则保持普通事实抽取。"""


_RULE_TABLE_PROPERTY_RULES = """
### 自动判定允许的规则类型与记录，属性补全遵守以下约束
- 以下约束仅适用于 rule_types 中的规则实体，且只能综合 rule_rows 对应的源记录；普通对象仍遵守通用表格属性约束。
- 同一规则可能对应记录表中的多条记录（例如按分类字段归并）：语义属性必须综合该规则对应的全部有效记录进行归纳，不得只取第一条，也不得把多条记录逐条罗列成同义拼接。
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
- known entities 中 missing property 只有原记录提供依据时才输出更新；无法确定时省略，不得直接复制实体名或编造规则编号。"""


_RULE_TABLE_ENTITY_RULES = """
### 自动判定允许的规则类型与记录，实体抽取遵守以下约束
- 以下归纳仅适用于 rule_types 和 rule_rows；其他记录、设备、人员、参数、表单事项仍遵守通用表格实体约束。
- 有规则语义的记录应归纳出 ontology 允许的规则，不得因为「内容都是字段标签和标量值」就直接输出空数组。
- 规则记录有分类字段（如「故障详细分类」）且未给出独立规则名称时，规则 mention 可用该分类值（如「RTU」「对标不准」），不要添加「故障」「规则」后缀。此规则不适用于普通对象。
- 同一规则下的多条记录按 ontology 的 identity/名称语义归并；不同规则编号须分别保留，分类相同本身不能覆盖不同身份。
- 缺少 RuleID 不阻止抽取有原文支持的规则；保持该身份属性缺失，禁止编造编号。"""


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
    return looks_like_official_form(chunk)



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
    elif task == "identity":
        discipline = (
            "只为给定的候选实体补全 ontology 声明的缺失身份属性；"
            "不得改变实体类型、名称或候选集合，不得输出关系。"
        )
        top_level = '{"updates": []}'
    elif task == "property":
        discipline = (
            "只为给定的已接受实体补全缺失属性；实体集合、实体类型和关系集合均已冻结，"
            "不得创建、删除或重命名实体，不得输出关系。"
        )
        top_level = '{"updates": []}'
    elif task == "relation":
        discipline = "只分类给定 relation pair；不得创建新端点、不得输出未列出的关系。"
        top_level = '{"decisions": {}}' if relation_pairs is not None else '{"relations": []}'
    else:
        discipline = "只修复列出的 verifier issues；不得重复已接受事实，不得处理未列问题。"
        top_level = "按本轮 schema 输出 JSON object"

    record_table = _looks_like_record_table(chunk)
    table_semantics = analyze_table_semantics(plan, chunk)
    has_table = "source.not_table" not in table_semantics.reasons
    rule_scope = (
        "\n### 自动表格语义判定\n"
        + json.dumps(table_semantics.to_dict(), ensure_ascii=False)
        + "\n- rule_rows 是当前 chunk 中 JSON 表序号及数据行序号（均从 1 开始）。"
        "规则专用指令仅适用于 rule_types 类型和 rule_rows 对应记录；"
        "其他类型与记录仍遵守通用表格约束，不得跨表按类别归并。"
        "该判定仅约束 JSON 表；表外正文按其原文语义与本体独立抽取。\n"
    ) if has_table else ""
    if record_table and span_entity_task:
        output_constraint = (
            "- 只输出 JSON object。不要用空数组敷衍：结构化记录/表格中每一行或每一条可归纳对象都应输出；"
            '只有确认 current chunk 中不存在任何符合 ontology 的对象时，才允许输出 {"entities": []}。'
        )
    elif task == "relation" and relation_pairs is not None:
        output_constraint = (
            "- 只输出 JSON object；必须为候选目录中的每个 pair_id 输出一项决策。"
            "确认没有关系时输出 NONE；证据不足或无法消除歧义时输出 ABSTAIN；禁止输出空 decisions。"
        )
    else:
        output_constraint = f"- 只输出 JSON object；没有结果时输出 {top_level}。"

    issue_text = (
        json.dumps(repair_issues, ensure_ascii=False, sort_keys=True, default=str)
        if repair_issues
        else "（无）"
    )
    evidence_scope_rule = (
        "- evidence.quote 必须逐字来自 current chunk.text 或 local context 中的单个 chunk。"
        if task == "relation" and relation_pairs is not None
        else "- evidence.quote 尽量逐字来自 current chunk.text；无法逐字对应时给出最相关的原文片段。"
    )
    # Entity extraction is span-first: properties and canonical names are
    # resolved later by the graph pool.  Keep this prompt's entity branch
    # limited to the type directory so the model does not turn scalar values
    # or property labels into nodes.
    if span_entity_task:
        ontology_section = _type_catalog_text(plan, official_entity_types(plan, chunk, table_entity_types(plan, chunk), route))
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
        if has_table:
            entity_rules += _RECORD_TABLE_ENTITY_RULES + rule_scope
            if table_semantics.rule_rows:
                entity_rules += _RULE_TABLE_ENTITY_RULES
        if _looks_like_official_form(chunk):
            entity_rules += _OFFICIAL_FORM_RULES
    else:
        ontology_section = _catalog_text(plan, include_comments=record_table)
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
- 属性值可以是对 current chunk 的忠实语义归纳，不要求属性值在 evidence.quote 中逐字出现。
- 同一条 evidence.quote 可以同时支持本次的多个属性，但实体、属性集合和证据的整体语义必须一致。
- 禁止引入 current chunk 之外的新事实、默认值或外部知识。
- 原文中缺失或无法确定的属性必须省略；禁止用 null、空字符串或占位符表示已补全。
- 同一个实体的 updates 中，一个属性只能出现一次；如果属性已有值，不要重复输出。
- evidence.quote 必须是 current chunk.text 中最能支撑本次整体更新的原文片段。"""
        if has_table:
            entity_rules += _RECORD_TABLE_PROPERTY_RULES + rule_scope
            known_types = {str(item.get("type") or "") for item in known_entities or []}
            if table_semantics.rule_rows and (
                not known_types or known_types.intersection(table_semantics.rule_types)
            ):
                entity_rules += _RULE_TABLE_PROPERTY_RULES

    if task == "identity":
        relation_section = "（身份补全不开放关系目录）"
        entity_rules = """
- entity_id 只能来自 known entities；properties 只能包含 missing_properties 中列出的 ontology identity 属性。
- 不得输出或修改 name/type/relation，不得创建新实体。
- 身份值必须由 current chunk 支持；允许格式归一化，禁止使用外部知识或臆造编号。
- 同一条 evidence.quote 可以支持多个组合身份字段，但整体语义必须一致。
- 原文无法确定身份时省略该更新；禁止输出 null、空字符串或占位符。
- evidence.quote 必须是 current chunk.text 中最能支持本次身份更新的原文片段。"""

    if task == "relation" and relation_pairs is not None:
        ontology_section = "候选 relation pair（只能选择这些 pair 的 relation_id 或 NONE）\n" + "\n".join(
            f"- `{pair['pair_id']}`: {pair['source_id']}({pair['source_type']}) "
            f"-[{pair['relation_id']}]-> {pair['target_id']}({pair['target_type']}); "
            f"source_name={json.dumps(pair.get('source_name') or '', ensure_ascii=False)}; "
            f"target_name={json.dumps(pair.get('target_name') or '', ensure_ascii=False)}; "
            f"relation_label={json.dumps(pair.get('relation_label') or pair['relation_id'], ensure_ascii=False)}"
            for pair in relation_pairs
        )
        feature_section = "（关系 pair 分类不展示属性目录）"
        relation_section = "（关系 pair 分类不开放关系目录）"
        known_section = "（关系端点已由候选 pair 固定）"
        context_section = local_context or "（无）"
        entity_rules = """
- decisions 必须是以 pair_id 为键的对象，并且候选目录中的每个 pair_id 必须恰好出现一次；不得遗漏。
- 每个 pair 的值只能包含 label、evidence.quote；label 只能是该 pair 的 relation_id、NONE 或 ABSTAIN。
- NONE 表示已检查 current chunk 与 local context，确认没有原文支持该关系；NONE 不产生关系。
- ABSTAIN 表示上下文不足、指代歧义或证据冲突，无法可靠判断；ABSTAIN 不得当作负样本。
- 禁止输出新端点、新 pair 或其他关系；不能确定时必须输出 ABSTAIN，而不是猜测 relation_id 或 NONE。
- relation_id 的 evidence.quote 必须来自 current chunk.text 或 local context 的单个 chunk；不得拼接多个 chunk。NONE/ABSTAIN 的 quote 输出空字符串。"""

    user = f"""## task
{task}

## task discipline
{discipline}

## resolved route
kind={route.kind}; labels={', '.join(route.labels) or 'none'}; source={route.source}; reasons={', '.join(route.reasons)}

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
{evidence_scope_rule}
 - type/property/relation 只能使用 ontology catalog/signatures 中的稳定标识。
{entity_rules}

## known entities
{known_section}

## local context
{context_section}

## current chunk
{_chunk_text(chunk)}

## repair issues
{issue_text}"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


__all__ = [
    "BASE_SYSTEM_PROMPT",
    "OFFICIAL_DOCUMENT_PROMPT_EXTENSION",
    "STANDARD_PROMPT_EXTENSION",
    "ROUTER_VERSION",
    "ROUTE_SIGNAL_RULES",
    "ChatMessage",
    "ChatMessages",
    "DocumentRoute",
    "build_chat_messages",
    "resolve_chunk_routes",
    "resolve_document_route",
]
