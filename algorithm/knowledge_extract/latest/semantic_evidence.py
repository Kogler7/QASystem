"""轻量语义证据核验：逐字定位失败时先做语义比对，不一致再交给 LLM 核验。

该模块只提供纯函数与提示词构造，不导入抽取流水线，避免循环依赖。
"""
from __future__ import annotations

import json
from algorithm.knowledge_extract.jsonl_protocol import jsonl_instruction, record_schema
import re
from difflib import SequenceMatcher
from typing import Any

from .chunk_context import ExtractionChunk

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")
_PUNCT_PATTERN = re.compile(r"[\s\u3000\"'”‘’「」『』【】\[\]()（）,，.。:：;；/\\|]+")

VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "supported": {"type": "string", "enum": ["yes", "no", "uncertain"]},
                    "reason": {"type": "string"},
                },
                "required": ["item_id", "supported", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

VERIFICATION_SYSTEM_PROMPT = """你是抽取结果的证据核验器。
只依据给定的 current chunk 原文核验候选事实。候选可能是实体属性更新，也可能是 source-relation-target 关系断言。
实体属性值可以是对原文的忠实归纳，不要求每个属性值在 quote 中逐字出现，也不要求一个属性对应一条独立证据。
关系候选必须由原文支持指定方向、指定关系和两个指定端点；只同时出现两个实体、主题相关或依赖常识推断都不能判为 yes。
yes 必须表示 current chunk 能支持候选的完整事实，不得引入外部知识、混淆实体、颠倒关系方向或把邻近事实错误拼接。
supported 只能取 yes（原文能够支持）、no（与原文矛盾或明显不属于原文）、uncertain（无法判断）。
输出遵循共享 JSONL 协议，记录只包含 record schema 允许的字段。"""


def normalize_for_match(value: Any) -> str:
    """去掉空白与常见标点并统一大小写，便于中文/英文的包含与相似度比较。"""
    return _PUNCT_PATTERN.sub("", str(value or "").casefold())


def semantic_support(
    chunk_text: str,
    values: list[Any],
    *,
    min_longest_ratio: float = 0.8,
    min_token_overlap: float = 0.6,
) -> bool:
    """用归一化包含、最长公共片段与词元覆盖率判断 values 是否被原文支持。

    - 任一非空值完全不支持即返回 False（交由 LLM 核验）；
    - 所有值都为空时返回 False。
    """
    haystack = normalize_for_match(chunk_text)
    if not haystack:
        return False
    hay_tokens = set(_TOKEN_PATTERN.findall(haystack))
    checked = 0
    for value in values:
        needle = normalize_for_match(value)
        if not needle:
            continue
        checked += 1
        if needle in haystack:
            continue
        longest = SequenceMatcher(None, needle, haystack, autojunk=False).find_longest_match(
            0, len(needle), 0, len(haystack)
        )
        if longest.size / max(1, len(needle)) >= min_longest_ratio:
            continue
        needle_tokens = set(_TOKEN_PATTERN.findall(needle))
        if needle_tokens:
            overlap = len(needle_tokens & hay_tokens) / len(needle_tokens)
            if overlap >= min_token_overlap:
                continue
        return False
    return checked > 0


def build_verification_messages(
    *,
    chunk: ExtractionChunk,
    items: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """构造 LLM 核验消息：判断候选实体属性或关系是否被 current chunk 支持。"""
    scoped = all(str(item.get("entity_context") or "").strip() for item in items)
    chunk_payload = {
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "text": "（候选项已提供实体锚定上下文，不开放整个 chunk）" if scoped else chunk.text,
    }
    user = f"""## task
evidence_verification

## current chunk
{json.dumps(chunk_payload, ensure_ascii=False)}

## candidates
{json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)}

## output protocol
{jsonl_instruction(VERIFICATION_SCHEMA)}

## record schema
{json.dumps(record_schema(VERIFICATION_SCHEMA), ensure_ascii=False, sort_keys=True)}

## output constraints
- 逐项核验 candidates，按 item_id 输出 verdicts，不得遗漏任何 item_id。
- claim_kind=entity 时，按整个实体属性更新做语义核验；不要做属性值与 quote 的逐字一一对齐。
- claim_kind=relation 时，必须核验 source、relation、target、方向和 evidence_quote 组成的完整关系断言。
- candidate 含 entity_context 时，只能依据该实体锚定上下文判断，不得借用其他候选实体的事实。
- yes=原文支持完整候选事实；no=与原文矛盾、关系不成立或属于其他实体；uncertain=无法判断。
- 每条核验记录独占一行；不要解释。"""
    return [
        {"role": "system", "content": VERIFICATION_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


__all__ = [
    "VERIFICATION_SCHEMA",
    "build_verification_messages",
    "normalize_for_match",
    "semantic_support",
]
