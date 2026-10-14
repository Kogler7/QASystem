"""Entity/property test runtime. Relationship calls are rejected."""
from __future__ import annotations

import json
import re
from typing import Any, Callable

_TASK = re.compile(r"## task\s*\n(\w+)")
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
    payload = _user_payload(prompt)
    if payload:
        return payload["task"]["operation"]
    match = _TASK.search(prompt)
    return match.group(1) if match else ""


def _user_payload(prompt: str) -> dict[str, Any]:
    try:
        payload = json.loads(prompt)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) and "task" in payload else {}


def source_documents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only model-visible text; source segmentation remains internal."""
    text = payload['documents']
    pattern = r'【原文片段(\d+)；([^\n]*)】\n(.*?)\n【原文片段\1结束】'
    parts = [{'text': match[2], 'context_only': '仅辅助上下文' in match[1]}
             for match in re.findall(pattern, text, re.S)]
    return parts or [{'text': text}]



def current_chunk(prompt: str) -> dict[str, Any]:
    """解析 "## current chunk" 段落中的 chunk JSON（单行 json.dumps）。"""
    payload = _user_payload(prompt)
    if payload:
        documents = source_documents(payload)
        if payload['task']['operation'] in {'entity', 'repair'} and 'known_entities' not in payload['task']:
            return next((document for document in documents if not document.get('context_only')), documents[0])
        return {'text': '\n\n'.join(document['text'] for document in documents)}
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
        return payload["output_contract"]["success_schema"].get("required", [])
    lines = prompt.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "## output schema" and index + 1 < len(lines):
            try:
                schema = json.loads(lines[index + 1])
            except json.JSONDecodeError:
                return []
            return list(schema.get("required") or [])
    return []




def parse_known_entities(prompt: str) -> list[dict[str, str]]:
    """解析 known entities 表：| `id` | `type` | name | properties | missing_properties |。"""
    payload = _user_payload(prompt)
    if payload:
        return [{
            "type": item["type"], "name": item["name"],
            "properties": ", ".join(f"{k}={v}" for k, v in item.get("properties", {}).items()),
            "missing": ", ".join(item.get("missing_properties", {})),
        } for item in payload["task"]["known_entities"]]
    rows = []
    for match in _KNOWN_ROW.finditer(prompt):
        row = match.groupdict()
        if row["id"] == "id":  # 表头
            continue
        rows.append(row)
    return rows


def span_item(type_id: str, mention: str, quote: str | None = None) -> dict[str, Any]:
    """构造 entity pass 候选；不包含模型字符偏移。"""
    return {"type": type_id, "mention": mention, "evidence": {"quote": quote or mention}}




class StubLLM:
    """按 task 路由的测试桩；默认实体核验 yes，属性核验 uncertain。

    默认 yes 只表示测试 fixture 声明的实体获支持，不执行真实语义判断。
    verification_handler 同时覆盖实体/属性；实体专用判据可单独提供。
    """

    def __init__(
        self,
        entity_handler: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None,
        property_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        repair_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        verification_handler=None,
        selection_handler=None,
        identity_handler=None,
        entity_verification_handler=None,
    ) -> None:
        self.entity_handler = entity_handler
        self.property_handler = property_handler
        self.repair_handler = repair_handler
        self.verification_handler = verification_handler
        self.selection_handler = selection_handler
        self.identity_handler = identity_handler
        self.entity_verification_handler = entity_verification_handler
        self.calls: list[tuple[str, str]] = []

    def _ground(self, item: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
        text = str(chunk.get("text") or "")
        mention = str(item.get("mention") or "")
        start = text.find(mention) if mention else -1
        assert mention and start >= 0, f"mention {mention!r} 不在当前 chunk 文本中"
        return {
            "type": item["type"],
            "mention": mention,
            "evidence": dict(item.get("evidence") or {"quote": mention}),
            **({"properties": item["properties"]} if "properties" in item else {}),
        }

    def __call__(self, messages: Any) -> str:
        prompt = user_prompt(messages)
        task = task_of(prompt)
        self.calls.append((task, prompt))
        if task == 'skill_selection':
            user = _user_payload(prompt)
            if self.selection_handler is not None:
                return json.dumps(self.selection_handler(user), ensure_ascii=False)
            # Deterministic fixtures only; runtime selection uses Skill descriptions.
            types = set(user['task']['allowed_types'])
            family = ('official_document' if 'OfficialDocument' in types else
                      'rule' if 'RuleType' in types else
                      'trouble' if 'FaultComponent' in types else None)
            refs = [s['skill_ref'] for s in user['task']['skills'] if s['name'] == family]
            return json.dumps({'status': 'selected' if refs else 'no_match',
                               'skill_refs': refs, 'reason': 'fixture selection'})
        chunk = current_chunk(prompt)
        if task in {"relation", "relation_verification"}:
            raise AssertionError("V3.1 must not request relationship extraction")
        elif task == 'candidate_identity':
            items = _user_payload(prompt)['task']['items']
            payload = (self.identity_handler(_user_payload(prompt)) if self.identity_handler else
                       {item['item_id']: {'mention': item['name']} for item in items})
            return json.dumps(payload, ensure_ascii=False)
        elif task == "evidence_verification":
            items = _user_payload(prompt)["task"]["items"]
            def verdict(item):
                handler = (self.entity_verification_handler if 'property' not in item
                           and self.entity_verification_handler is not None else self.verification_handler)
                return handler(item, chunk) if handler else ('yes' if 'property' not in item else 'uncertain')
            payload = {"verdicts": [{"item_id": item["item_id"],
                "supported": verdict(item),
                "reason": "fixture verdict"} for item in items]}
        elif task == "entity":
            items = self.entity_handler(prompt, chunk) if self.entity_handler else []
            payload: dict[str, Any] = {"entities": [self._ground(item, chunk) for item in items]}
        elif task == "property" or (task == 'repair' and 'known_entities' in _user_payload(prompt)['task']):
            handler = self.repair_handler if task == 'repair' else self.property_handler
            payload = (
                handler(prompt, chunk) if handler else {
                    "entities": [{"type": e["type"], "name": e["name"], "properties": {}}
                                 for e in _user_payload(prompt)["task"]["known_entities"]]
                }
            )
        else:  # repair 或其他：按 output schema 顶层 required 键返回空集合
            if self.repair_handler is not None:
                payload = self.repair_handler(prompt, chunk)
            else:
                required = output_schema_required(prompt) or ["entities"]
                payload = {key: [] for key in required}
        return ("\n".join(json.dumps(item, ensure_ascii=False) for item in payload["entities"]) or "{}") if "entities" in payload else "\n".join(json.dumps(item, ensure_ascii=False) for item in payload["verdicts"])
