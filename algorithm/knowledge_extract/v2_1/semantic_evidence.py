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
只依据给定的 current chunk 原文判断候选实体/属性是否属于原文，不引入外部知识。
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
    """构造 LLM 核验消息：判断候选实体/属性是否属于 current chunk。"""
    user = f"""## task
evidence_verification

## current chunk
{json.dumps({"chunk_id": chunk.chunk_id, "document_id": chunk.document_id, "text": chunk.text}, ensure_ascii=False)}

## candidates
{json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)}

## output protocol
{jsonl_instruction(VERIFICATION_SCHEMA)}

## record schema
{json.dumps(record_schema(VERIFICATION_SCHEMA), ensure_ascii=False, sort_keys=True)}

## output constraints
- 逐项核验 candidates，按 item_id 输出 verdicts，不得遗漏任何 item_id。
- yes=原文支持；no=与原文矛盾或明显不属于原文；uncertain=无法判断。
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
