"""knowledge_extract_v2 测试共享的 stub LLM（重构后两段式 + 属性补全 + pair 关系分类协议）。

当前 extract_split 的 LLM 契约（见 prompt_router.build_chat_messages）：
- entity   pass：响应 {"entities": [{type, mention, start_char, end_char, evidence:{quote}}]}，
  mention/quote 必须逐字来自 current chunk；
- property pass：响应 {"updates": [{entity_id, properties, evidence:{quote}}]}；
- relation pass：prompt 列出候选（``- `c-x`: entity-0(T) -[rel]-> entity-1(T)``）。
  新契约响应 {"decisions": [{candidate_id, status, evidence:[{fragment_id, quote}]}]}，
  status 为 supported / not_supported / insufficient_evidence。
  测试 handler 仍可返回旧的 {pair_id, label, evidence:{quote}}，StubLLM 会按 schema 转换：
  label 等于该候选关系或非 NONE 时视为 supported，NONE 视为 not_supported。
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


def parse_relation_pairs(prompt: str) -> list[dict[str, str]]:
    """解析 relation pass prompt 中枚举的候选。"""
    return [match.groupdict() for match in _PAIR_ROW.finditer(prompt)]


def _output_schema_object(prompt: str) -> dict[str, Any]:
    lines = prompt.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "## output schema" and index + 1 < len(lines):
            try:
                schema = json.loads(lines[index + 1])
            except json.JSONDecodeError:
                return {}
            return schema if isinstance(schema, dict) else {}
    return {}


def _default_fragment_id(prompt: str) -> str:
    match = re.search(r"-\s*`(f[^`]+)`\s+chunk=", prompt)
    return match.group(1) if match else "f1"


def relation_decisions_for_schema(prompt: str, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把测试 handler 的旧 label 决策转换成当前关系 schema。"""
    schema = _output_schema_object(prompt)
    item = ((schema.get("properties") or {}).get("decisions") or {}).get("items") or {}
    required = item.get("required") or []
    if "status" not in required and "candidate_id" not in required:
        return decisions
    fragment_id = _default_fragment_id(prompt)
    pairs = {row["pair_id"]: row for row in parse_relation_pairs(prompt)}
    converted: list[dict[str, Any]] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        if decision.get("status") and decision.get("candidate_id"):
            evidence = decision.get("evidence")
            if isinstance(evidence, dict):
                quote = str(evidence.get("quote") or "")
                evidence = [{"fragment_id": fragment_id, "quote": quote}] if quote else []
            converted.append({
                "candidate_id": str(decision["candidate_id"]),
                "status": str(decision["status"]),
                "evidence": evidence if isinstance(evidence, list) else [],
            })
            continue
        pair_id = str(decision.get("pair_id") or decision.get("candidate_id") or "")
        label = str(decision.get("label") or "")
        relation_id = str((pairs.get(pair_id) or {}).get("relation_id") or "")
        if label == "NONE" or not label:
            status = "not_supported"
        elif not relation_id or label == relation_id:
            status = "supported"
        else:
            status = "not_supported"
        quote = str((decision.get("evidence") or {}).get("quote") or "") if isinstance(decision.get("evidence"), dict) else ""
        evidence = [{"fragment_id": fragment_id, "quote": quote}] if status == "supported" and quote else []
        converted.append({"candidate_id": pair_id, "status": status, "evidence": evidence})
    return converted


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
        property_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        relation_handler: Callable[[str, list[dict[str, str]], dict[str, Any]], list[dict[str, Any]]] | None = None,
        repair_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.entity_handler = entity_handler
        self.property_handler = property_handler
        self.relation_handler = relation_handler
        self.repair_handler = repair_handler
        self.calls: list[tuple[str, str]] = []

    def _ground(self, item: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
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
        elif task == "property":
            payload = (
                self.property_handler(prompt, chunk) if self.property_handler else {"updates": []}
            )
        elif task == "relation":
            pairs = parse_relation_pairs(prompt)
            decisions = (
                self.relation_handler(prompt, pairs, chunk) if self.relation_handler else []
            )
            payload = {"decisions": relation_decisions_for_schema(prompt, decisions)}
        else:  # repair 或其他：按 output schema 顶层 required 键返回空集合
            if self.repair_handler is not None:
                payload = self.repair_handler(prompt, chunk)
            else:
                required = output_schema_required(prompt) or ["entities"]
                payload = {key: [] for key in required}
        return json.dumps(payload, ensure_ascii=False)
