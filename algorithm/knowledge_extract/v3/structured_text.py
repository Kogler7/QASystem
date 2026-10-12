"""识别「表格 JSON」文本：解析层产出的表格统一是 Markdown 外壳 + json 代码块。

依据 `docs/算法/文档解析行为规范.md` §10：解析结果中的表格统一输出为 json 代码块，
正文、标题、图片等其余结构仍是 Markdown——只有表格本身变成 JSON。于是真实产物是

    ## 二号线数据

    {"columns": [...], "rows": [{...}]}   （放在 json 代码块里）

而历史判定（`prompt_router._looks_like_record_table`）只认「整段就是 JSON」，那其实是
benchmark 数据集（`benchmark/*/docs/*.md` 里的裸 JSON）的形态。两者不一致的后果是：
同一份数据，benchmark 能命中记录表策略（专用提示词、空数组重试、证据兜底），
从前端上传 xlsx/docx 却永远命中不了。

本模块把「剥壳判定」收敛到一处，供 `prompt_router` 与 `candidate_selector` 共用。
注意：判定只影响抽取策略，不参与 evidence span 定位——证据仍必须逐字来自 chunk 原文。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from algorithm.table_jsonl import decode_records, row_boundaries

#: JSON 主体占有效（非空白）字符的比例下限。
#: 低于它说明 chunk 里还有成规模的正文，按普通文档处理，不套用记录表策略。
DEFAULT_MIN_RATIO = 0.8

#: 知识沉淀批量路径会在每个文件前拼接 "## 文件: <文件名>" 头。
_BATCH_HEADER = re.compile(r"^\s*#{1,6}\s*文件\s*[:：].*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_FENCE = re.compile(r"^\s*```\s*(?P<lang>[A-Za-z0-9_+-]*)\s*$")
#: 允许的代码块语言标注：空标注或 json。
_JSON_LANGS = frozenset({"", "json", "jsonl"})


@dataclass(frozen=True, slots=True)
class StructuredPayload:
    """剥壳后的表格 JSON：``text`` 是 JSON 主体，``blocks`` 是逐块解析结果。

    多 sheet 的 Excel 会产出多个 json 代码块，因此这里按块保留；
    ``text`` 是多块按出现顺序拼接后的原文（仅作展示/回溯用，不保证整体可解析）。
    """

    text: str
    blocks: tuple[Any, ...]


def _non_space_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _strip_leading_scaffolding(text: str) -> str:
    """剥掉开头的空行与批量头，让「裸 JSON」判定不受前缀影响。"""
    lines = str(text or "").splitlines()
    index = 0
    while index < len(lines) and (
        not lines[index].strip() or _BATCH_HEADER.match(lines[index])
    ):
        index += 1
    return "\n".join(lines[index:])


def _parse_payload(payload: str) -> Any | None:
    payload = payload.strip()
    if not payload or payload[0] not in "[{":
        return None
    try:
        return json.loads(payload)
    except (TypeError, ValueError):
        return decode_records(payload)


def _strip_fence_edges(text: str) -> str:
    """去掉首尾的孤立围栏行：JSON 保护切片会把 ```json/``` 留在相邻片段里。"""
    lines = text.splitlines()
    while lines and _FENCE.match(lines[0]):
        lines.pop(0)
    while lines and _FENCE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines)


def _is_record_payload(data: Any) -> bool:
    """记录表的合法形态：记录数组，或表对象（解析层固定输出 {"columns", "rows"}）。"""
    if isinstance(data, list):
        return any(isinstance(item, dict) for item in data)
    return isinstance(data, dict)


def _collect_block(
    lang: str,
    lines: list[str],
    json_blocks: list[tuple[str, Any]],
    other_parts: list[str],
) -> None:
    """把一个代码块按记录表 JSON 收进 json_blocks，否则算作普通内容。"""
    body = "\n".join(lines).strip()
    if not body:
        return
    data = _parse_payload(body)
    if lang in _JSON_LANGS and data is not None and _is_record_payload(data):
        if decode_records(body) is not None and len(body.splitlines()) > 1:
            json_blocks.extend((line, json.loads(line)) for line in body.splitlines() if line.strip())
        else:
            json_blocks.append((body, data))
        return
    other_parts.append(body)


def extract_json_payload(
    text: str,
    *,
    min_ratio: float = DEFAULT_MIN_RATIO,
) -> StructuredPayload | None:
    """返回剥壳后的表格 JSON；文本不是「以表格 JSON 为主体」时返回 None。

    依次识别两种形态：

    1. **整段就是 JSON**——benchmark 数据集、或表格 JSON 直接存成 .md 的形态；
    2. **Markdown 外壳 + json 代码块**——解析层 §10 的产物形态（Excel 的
       "## sheet 名 + 表格 JSON"、docx/OCR 原地替换的表格 JSON）。可以有多个块
       （多 sheet），此时要求 JSON 主体占有效字符不低于 ``min_ratio``，
       避免把「正文里顺带贴了一小段 JSON」的普通文档误判成记录表。
    """
    raw = str(text or "")
    if not raw.strip():
        return None

    body = _strip_leading_scaffolding(raw)
    if row_boundaries(body) is not None:
        lines = [line for line in body.splitlines() if decode_records(line) is not None]
        return StructuredPayload(text="\n".join(lines),
                                 blocks=tuple(json.loads(line) for line in lines))

    # 形态 1：整段 JSON（容忍 JSON 保护切片残留的首尾围栏行）
    stripped = body.strip()
    data = _parse_payload(stripped)
    if data is None or not _is_record_payload(data):
        stripped = _strip_fence_edges(stripped).strip()
        data = _parse_payload(stripped)
    if data is not None and _is_record_payload(data):
        return StructuredPayload(text=stripped, blocks=(tuple(decode_records(stripped))
                                 if decode_records(stripped) is not None else (data,)))

    # 形态 2：Markdown 外壳 + json 代码块（允许一个或多个）
    json_blocks: list[tuple[str, Any]] = []
    other_parts: list[str] = []
    inside_fence = False
    fence_lang = ""
    fence_lines: list[str] = []
    for line in body.splitlines():
        match = _FENCE.match(line)
        if match:
            if inside_fence:
                _collect_block(fence_lang, fence_lines, json_blocks, other_parts)
                fence_lines = []
            else:
                fence_lang = match.group("lang").lower()
            inside_fence = not inside_fence
            continue
        if inside_fence:
            fence_lines.append(line)
            continue
        # 标题、批量头、空行都是外壳，不计入正文
        if not line.strip() or _HEADING.match(line) or _BATCH_HEADER.match(line):
            continue
        other_parts.append(line)
    if inside_fence:  # 围栏未闭合时按内容容错
        _collect_block(fence_lang, fence_lines, json_blocks, other_parts)

    if not json_blocks:
        return None
    payload_text = "\n".join(block for block, _ in json_blocks)
    payload_length = _non_space_length(payload_text)
    other_length = _non_space_length("\n".join(other_parts))
    total = payload_length + other_length
    if total <= 0 or payload_length / total < min_ratio:
        return None
    return StructuredPayload(
        text=payload_text,
        blocks=tuple(block for _, block in json_blocks),
    )


def is_structured_payload(text: str, *, min_ratio: float = DEFAULT_MIN_RATIO) -> bool:
    """``text`` 是否以表格 JSON 为主体（裸 JSON 或 Markdown 外壳包裹的 JSON）。"""
    return extract_json_payload(text, min_ratio=min_ratio) is not None


def has_json_table_payload(text: str) -> bool:
    """``text`` 中是否含表格 JSON（不要求它占主体）。

    用途是结构支撑判定：表单识别、标准「表」实体补全、候选结构门控只需要确认
    「这里有表格数据」，正文夹带少量 JSON 不影响这些结论。策略选择
    （记录表提示词、空数组重试）仍用 ``is_structured_payload`` 的占比口径。
    """
    return extract_json_payload(text, min_ratio=0.0) is not None


__all__ = [
    "DEFAULT_MIN_RATIO",
    "StructuredPayload",
    "extract_json_payload",
    "has_json_table_payload",
    "is_structured_payload",
]
