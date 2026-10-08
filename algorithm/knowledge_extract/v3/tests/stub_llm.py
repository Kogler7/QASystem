"""knowledge_extract_v3 测试共享的 stub LLM（重构后两段式 + 属性补全 + pair 关系分类协议）。

当前 extract_split 的 LLM 契约（见 prompt_router.build_chat_messages）：
- entity   pass：响应 {"entities": [{type, mention, start_char, end_char, evidence:{quote}}]}，
  mention/quote 必须逐字来自 current chunk；
- property pass：响应 {"updates": [{entity_id, properties, evidence:{quote}}]}；
- relation pass：prompt 列出候选 pair（``- `pair-x`: entity-0(T) -[rel]-> entity-1(T)``），
  响应 {"decisions": {pair_id: {label, evidence:{quote}}}}，每个 pair 必须明确输出 relation_id 或 NONE；
- repair   pass：按 prompt 内 output schema 的顶层 required 键返回空集合。

StubLLM 接收 ChatMessages（list[dict]），按 "## task" 段落路由到对应 handler；
handler 拿不到/不需要的一律返回合法空响应，保证测试 hermetic（不访问真实 LLM）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

_TASK = re.compile(r"## task\s*\n(\w+)")
_PAIR_ROW = re.compile(
    r"-\s*`(?P<pair_id>[^`]+)`:\s*(?P<source_id>\S+)\((?P<source_type>[^)]+)\)"
    r"\s*-\[(?P<relation_id>[^\]]+)\]->\s*(?P<target_id>\S+)\((?P<target_type>[^)]+)\)"
)
_KNOWN_ROW = re.compile(
    r"\|\s*`(?P<id>[^`]+)`\s*\|\s*`(?P<type>[^`]+)`\s*\|\s*(?P<name>[^|]+?)\s*\|"
    r"\s*(?P<properties>[^|]*?)\s*\|\s*(?P<missing>[^|]*?)\s*\|"
)


def user_prompt(messages: Any) -> str:
    """从 ChatMessages 取最后一条 user 内容；兼容直接传字符串的旧式 stub。"""
    if isinstance(messages, str):
        return messages
    if isinstance(messages, (list, tuple)):
        for message in reversed(list(messages)):
            if isinstance(message, dict) and message.get("role") == "user":
                return str(message.get("content") or "")
        return "".join(str(m.get("content") or "") for m in messages if isinstance(m, dict))
    return str(messages)


def task_of(prompt: str) -> str:
    match = _TASK.search(prompt)
    return match.group(1) if match else ""


def current_chunk(prompt: str) -> dict[str, Any]:
    """解析 "## current chunk" 段落中的 chunk JSON（单行 json.dumps）。"""
    lines = prompt.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "## current chunk" and index + 1 < len(lines):
            try:
                return json.loads(lines[index + 1])
            except json.JSONDecodeError:
                return {}
    return {}


def output_schema_required(prompt: str) -> list[str]:
    lines = prompt.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "## output schema" and index + 1 < len(lines):
            try:
                schema = json.loads(lines[index + 1])
            except json.JSONDecodeError:
                return []
            return list(schema.get("required") or [])
    return []


def verification_items(prompt: str) -> list[dict[str, Any]]:
    """解析 evidence_verification prompt 中的 candidates JSON。"""
    lines = prompt.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "## candidates" and index + 1 < len(lines):
            try:
                value = json.loads(lines[index + 1])
            except json.JSONDecodeError:
                return []
            return value if isinstance(value, list) else []
    return []


def parse_relation_pairs(prompt: str) -> list[dict[str, str]]:
    """解析 relation pass prompt 中枚举的候选 pair。"""
    return [match.groupdict() for match in _PAIR_ROW.finditer(prompt)]


def parse_known_entities(prompt: str) -> list[dict[str, str]]:
    """解析 known entities 表：| `id` | `type` | name | properties | missing_properties |。"""
    rows = []
    for match in _KNOWN_ROW.finditer(prompt):
        row = match.groupdict()
        if row["id"] == "id":  # 表头
            continue
        rows.append(row)
    return rows


def span_item(type_id: str, mention: str, quote: str | None = None) -> dict[str, Any]:
    """构造 entity pass 候选；start/end 由 StubLLM 按 current chunk 文本填充。"""
    return {"type": type_id, "mention": mention, "evidence": {"quote": quote or mention}}


def decision(pair_id: str, label: str, quote: str) -> dict[str, Any]:
    return {"pair_id": pair_id, "label": label, "evidence": {"quote": quote}}


class StubLLM:
    """按 task 路由的测试桩；未提供的 handler 返回合法空响应。"""

    def __init__(
        self,
        entity_handler: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None,
        identity_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        property_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        evidence_handler: Callable[[str, dict[str, Any], list[dict[str, Any]]], dict[str, Any]] | None = None,
        relation_handler: Callable[[str, list[dict[str, str]], dict[str, Any]], list[dict[str, Any]]] | None = None,
        repair_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.entity_handler = entity_handler
        self.identity_handler = identity_handler
        self.property_handler = property_handler
        self.evidence_handler = evidence_handler
        self.relation_handler = relation_handler
        self.repair_handler = repair_handler
        self.calls: list[tuple[str, str]] = []

    def _ground(self, item: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
        if item.get("ungrounded"):
            # P0 fail-open 测试用：构造 mention/quote 均不在原文的归纳型候选。
            # 输出 schema 要求 offset 为整数，默认补 0（与原文不一致，走 fail-open）。
            mention = str(item.get("mention") or "")
            start = item.get("start_char")
            end = item.get("end_char")
            return {
                "type": item["type"],
                "mention": mention,
                "start_char": start if isinstance(start, int) else 0,
                "end_char": end if isinstance(end, int) else 0,
                "evidence": {"quote": str((item.get("evidence") or {}).get("quote") or "")},
            }
        text = str(chunk.get("text") or "")
        mention = str(item.get("mention") or "")
        start = text.find(mention) if mention else -1
        assert mention and start >= 0, f"mention {mention!r} 不在当前 chunk 文本中"
        return {
            "type": item["type"],
            "mention": mention,
            "start_char": start,
            "end_char": start + len(mention),
            "evidence": {"quote": str((item.get("evidence") or {}).get("quote") or mention)},
        }

    def __call__(self, messages: Any) -> str:
        prompt = user_prompt(messages)
        task = task_of(prompt)
        chunk = current_chunk(prompt)
        self.calls.append((task, prompt))
        if task == "entity":
            items = self.entity_handler(prompt, chunk) if self.entity_handler else []
            payload: dict[str, Any] = {"entities": [self._ground(item, chunk) for item in items]}
        elif task == "identity":
            payload = (
                self.identity_handler(prompt, chunk) if self.identity_handler else {"updates": []}
            )
        elif task == "property":
            payload = (
                self.property_handler(prompt, chunk) if self.property_handler else {"updates": []}
            )
        elif task == "evidence_verification":
            items = verification_items(prompt)
            payload = (
                self.evidence_handler(prompt, chunk, items)
                if self.evidence_handler
                else {
                    "verdicts": [
                        {
                            "item_id": str(item.get("item_id") or ""),
                            "supported": "yes",
                            "reason": "test stub accepts the semantic claim bundle",
                        }
                        for item in items
                    ]
                }
            )
        elif task == "relation":
            pairs = parse_relation_pairs(prompt)
            raw_decisions = (
                self.relation_handler(prompt, pairs, chunk) if self.relation_handler else []
            )
            if isinstance(raw_decisions, dict):
                decisions = dict(raw_decisions)
            else:
                decisions = {
                    str(item.get("pair_id") or ""): {
                        "label": str(item.get("label") or "NONE"),
                        "evidence": dict(item.get("evidence") or {"quote": ""}),
                    }
                    for item in raw_decisions
                    if isinstance(item, dict) and item.get("pair_id")
                }
            for pair in pairs:
                decisions.setdefault(
                    pair["pair_id"],
                    {"label": "NONE", "evidence": {"quote": ""}},
                )
            payload = {"decisions": decisions}
        else:  # repair 或其他：按 output schema 顶层 required 键返回空集合
            if self.repair_handler is not None:
                payload = self.repair_handler(prompt, chunk)
            else:
                required = output_schema_required(prompt) or ["entities"]
                payload = {key: [] for key in required}
        return json.dumps(payload, ensure_ascii=False)
