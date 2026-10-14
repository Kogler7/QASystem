"""Hybrid stub LLM for v2.2-v31: live v3.1 entity protocol + v2.2 relation protocol.

Stage 1 (live v3_1) uses JSON user payloads (skill_selection / entity JSONL /
evidence_verification / property). Stage 2 (v2.2) uses markdown relation
prompts with decision schema. Hermetic: no real LLM calls.
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


def _user_payload(prompt: str) -> dict[str, Any]:
    try:
        payload = json.loads(prompt)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) and "task" in payload else {}


def task_of(prompt: str) -> str:
    payload = _user_payload(prompt)
    if payload:
        return str(payload["task"].get("operation") or "")
    match = _TASK.search(prompt)
    return match.group(1) if match else ""


def source_documents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    text = payload.get("documents") or ""
    pattern = r"【原文片段(\d+)；([^\n]*)】\n(.*?)\n【原文片段\1结束】"
    parts = [
        {"text": match[2], "context_only": "仅辅助上下文" in match[1]}
        for match in re.findall(pattern, text, re.S)
    ]
    return parts or [{"text": text}]


def current_chunk(prompt: str) -> dict[str, Any]:
    """解析 v3 JSON documents 或 v2 ## current chunk。"""
    payload = _user_payload(prompt)
    if payload:
        documents = source_documents(payload)
        operation = payload["task"].get("operation")
        if operation in {"entity", "repair"} and "known_entities" not in payload["task"]:
            return next(
                (document for document in documents if not document.get("context_only")),
                documents[0],
            )
        return {"text": "\n\n".join(document["text"] for document in documents)}
    lines = prompt.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "## current chunk" and index + 1 < len(lines):
            try:
                return json.loads(lines[index + 1])
            except json.JSONDecodeError:
                return {}
    return {}


def output_schema_required(prompt: str) -> list[str]:
    payload = _user_payload(prompt)
    if payload:
        contract = payload.get("output_contract") or {}
        schema = contract.get("success_schema") or {}
        return list(schema.get("required") or [])
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
    payload = _user_payload(prompt)
    if payload:
        contract = payload.get("output_contract") or {}
        schema = contract.get("success_schema")
        return schema if isinstance(schema, dict) else {}
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
        quote = (
            str((decision.get("evidence") or {}).get("quote") or "")
            if isinstance(decision.get("evidence"), dict)
            else ""
        )
        evidence = [{"fragment_id": fragment_id, "quote": quote}] if status == "supported" and quote else []
        converted.append({"candidate_id": pair_id, "status": status, "evidence": evidence})
    return converted


def parse_known_entities(prompt: str) -> list[dict[str, str]]:
    """解析 v3 known_entities 或 v2 markdown 表。"""
    payload = _user_payload(prompt)
    if payload:
        return [
            {
                "id": f"{item.get('type')}::{item.get('name')}",
                "type": str(item.get("type") or ""),
                "name": str(item.get("name") or ""),
                "properties": ", ".join(
                    f"{key}={value}" for key, value in (item.get("properties") or {}).items()
                ),
                "missing": ", ".join(item.get("missing_properties") or []),
            }
            for item in payload["task"].get("known_entities") or []
            if isinstance(item, dict)
        ]
    rows = []
    for match in _KNOWN_ROW.finditer(prompt):
        row = match.groupdict()
        if row["id"] == "id":
            continue
        rows.append(row)
    return rows


def span_item(type_id: str, mention: str, quote: str | None = None) -> dict[str, Any]:
    """构造 entity pass 候选。"""
    return {"type": type_id, "mention": mention, "evidence": {"quote": quote or mention}}


def decision(pair_id: str, label: str, quote: str) -> dict[str, Any]:
    return {"pair_id": pair_id, "label": label, "evidence": {"quote": quote}}


def _coerce_property_payload(prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Accept legacy {updates:[{entity_id,...}]} or v3 {entities:[...]}."""
    if "entities" in payload:
        return payload
    updates = payload.get("updates")
    if not isinstance(updates, list):
        return {"entities": []}
    known = {row["id"]: row for row in parse_known_entities(prompt)}
    known_by_name = {(row["type"], row["name"].strip()): row for row in known.values()}
    entities: list[dict[str, Any]] = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        entity_id = str(update.get("entity_id") or "")
        row = known.get(entity_id)
        if row is None and update.get("type") and update.get("name"):
            row = known_by_name.get((str(update["type"]), str(update["name"]).strip()))
        if row is None and len(known) == 1:
            row = next(iter(known.values()))
        if row is None:
            continue
        entities.append(
            {
                "type": row["type"],
                "name": row["name"].strip(),
                "properties": dict(update.get("properties") or {}),
                "evidence": dict(update.get("evidence") or {"quote": row["name"].strip()}),
            }
        )
    return {"entities": entities}


class StubLLM:
    """Hybrid router: v3.1 entity tasks + v2.2 relation decisions."""

    def __init__(
        self,
        entity_handler: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None,
        property_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        relation_handler: Callable[[str, list[dict[str, str]], dict[str, Any]], list[dict[str, Any]]] | None = None,
        repair_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        verification_handler: Callable[[dict[str, Any], dict[str, Any]], str] | None = None,
        selection_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        identity_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.entity_handler = entity_handler
        self.property_handler = property_handler
        self.relation_handler = relation_handler
        self.repair_handler = repair_handler
        self.verification_handler = verification_handler
        self.selection_handler = selection_handler
        self.identity_handler = identity_handler
        self.calls: list[tuple[str, str]] = []

    def _ground_v31(self, item: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
        text = str(chunk.get("text") or "")
        mention = str(item.get("mention") or "")
        start = text.find(mention) if mention else -1
        assert mention and start >= 0, f"mention {mention!r} 不在当前 chunk 文本中"
        grounded = {
            "type": item["type"],
            "mention": mention,
            "evidence": dict(item.get("evidence") or {"quote": mention}),
        }
        if "properties" in item:
            grounded["properties"] = item["properties"]
        return grounded

    def __call__(self, messages: Any) -> str:
        prompt = user_prompt(messages)
        task = task_of(prompt)
        self.calls.append((task, prompt))
        payload_user = _user_payload(prompt)

        if task == "skill_selection":
            if self.selection_handler is not None:
                return json.dumps(self.selection_handler(payload_user), ensure_ascii=False)
            types = set(payload_user["task"].get("allowed_types") or [])
            family = (
                "official_document"
                if "OfficialDocument" in types
                else "rule"
                if "RuleType" in types or "CompositeRule" in types or "SimpleRule" in types
                else "trouble"
                if "FaultComponent" in types
                else None
            )
            refs = [
                skill["skill_ref"]
                for skill in payload_user["task"].get("skills") or []
                if skill.get("name") == family
            ]
            return json.dumps(
                {
                    "status": "selected" if refs else "no_match",
                    "skill_refs": refs,
                    "reason": "fixture selection",
                },
                ensure_ascii=False,
            )

        chunk = current_chunk(prompt)
        if task == "candidate_identity":
            items = payload_user["task"]["items"]
            payload = (
                self.identity_handler(payload_user)
                if self.identity_handler
                else {item["item_id"]: {"mention": item["name"]} for item in items}
            )
            return json.dumps(payload, ensure_ascii=False)

        if task == "evidence_verification":
            items = payload_user["task"]["items"]
            payload = {
                "verdicts": [
                    {
                        "item_id": item["item_id"],
                        "supported": (
                            self.verification_handler(item, chunk)
                            if self.verification_handler
                            else "yes"
                        ),
                        "reason": "fixture verdict",
                    }
                    for item in items
                ]
            }
            return "\n".join(json.dumps(item, ensure_ascii=False) for item in payload["verdicts"])

        if task == "entity":
            items = self.entity_handler(prompt, chunk) if self.entity_handler else []
            payload = {"entities": [self._ground_v31(item, chunk) for item in items]}
            return (
                "\n".join(json.dumps(item, ensure_ascii=False) for item in payload["entities"])
                or "{}"
            )

        if task == "property" or (
            task == "repair" and payload_user and "known_entities" in payload_user.get("task", {})
        ):
            handler = self.repair_handler if task == "repair" else self.property_handler
            if handler is not None:
                raw = handler(prompt, chunk)
            else:
                raw = {
                    "entities": [
                        {
                            "type": entity["type"],
                            "name": entity["name"],
                            "properties": {},
                        }
                        for entity in payload_user["task"].get("known_entities") or []
                    ]
                }
            payload = _coerce_property_payload(prompt, raw if isinstance(raw, dict) else {})
            return (
                "\n".join(json.dumps(item, ensure_ascii=False) for item in payload["entities"])
                or "{}"
            )

        if task == "relation":
            pairs = parse_relation_pairs(prompt)
            decisions = (
                self.relation_handler(prompt, pairs, chunk) if self.relation_handler else []
            )
            return json.dumps(
                {"decisions": relation_decisions_for_schema(prompt, decisions)},
                ensure_ascii=False,
            )

        if self.repair_handler is not None:
            payload = self.repair_handler(prompt, chunk)
            if isinstance(payload, dict) and "entities" in payload and payload_user:
                return (
                    "\n".join(
                        json.dumps(item, ensure_ascii=False) for item in payload["entities"]
                    )
                    or "{}"
                )
            return json.dumps(payload, ensure_ascii=False)
        required = output_schema_required(prompt) or ["entities"]
        empty = {key: [] for key in required}
        if "entities" in empty and payload_user:
            return "{}"
        return json.dumps(empty, ensure_ascii=False)
