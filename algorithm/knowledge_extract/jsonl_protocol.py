"""JSONL wire protocol; internal graph contracts remain collection objects."""
from __future__ import annotations

import json
from typing import Any


def collection_name(schema: dict) -> str | None:
    fields = schema.get("properties", {})
    arrays = [key for key, value in fields.items() if value.get("type") == "array"]
    return arrays[0] if len(fields) == 1 and len(arrays) == 1 else None


def messages_for_jsonl(messages: list, schema: dict) -> list:
    name = collection_name(schema)
    item_schema = schema["properties"][name]["items"] if name else schema
    instruction = (
        "\n\n输出协议更新：使用 JSONL，覆盖此前要求的外层 JSON 对象/数组格式。"
        "每条记录独占一行，不要 Markdown、解释、外层数组或集合键。"
        "字符串中的换行必须转义为 \\n。每行必须独立完整，禁止重复记录。"
        "没有记录时只输出一行 null。"
        "每行记录的字段约束（仅供本地校验，不使用服务端结构化输出）："
        + json.dumps(item_schema, ensure_ascii=False)
    )
    if name == "updates":
        instruction += "每行只更新一个属性：properties 中只能有一个键；每行携带 entity_id 和该属性的 evidence。"
    if not name:
        instruction += "当前任务没有单一记录集合：保留原输出对象的全部字段，在一行内输出完整对象。"
    result = [dict(message) for message in messages]
    # Put the protocol in the system message so source content cannot override it.
    if result and result[0].get("role") == "system":
        result[0]["content"] = str(result[0].get("content", "")) + instruction
    else:
        result.insert(0, {"role": "system", "content": instruction})
    return result


def decode_jsonl(text: Any, schema: dict) -> tuple[dict, list[str]]:
    """Keep complete records, report every malformed line; never invent a tail."""
    name = collection_name(schema)
    if isinstance(text, dict):  # Internal adapters/tests and saved legacy results.
        return text, []
    raw = str(getattr(text, "text", text) or "")
    # Read compatibility for saved JSON outputs, without requesting JSON remotely.
    try:
        legacy = json.loads(raw)
    except ValueError:
        legacy = None
    if isinstance(legacy, dict) and (not name or name in legacy):
        return legacy, []
    records, errors, seen = [], [], set()
    empty_marker = False
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            errors.append(f"JSONL line {number}: incomplete or invalid JSON")
            continue
        if value is None:
            empty_marker = True
            continue
        if not isinstance(value, dict):
            errors.append(f"JSONL line {number}: expected object")
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            records.append(value)
    if not raw.strip():
        errors.append("empty JSONL response")
    if empty_marker and records:
        errors.append("null must be the only JSONL record")
    if name:
        return {name: records}, errors
    if len(records) != 1:
        errors.append("expected one JSONL record for an object result")
    return (records[0] if records else {}), errors


def load_jsonl_response(response: Any, schema: dict) -> dict:
    """Strict reader for auxiliary calls that cannot publish partial results."""
    if getattr(response, "finish_reason", None) == "length":
        raise ValueError("incomplete JSONL response")
    data, errors = decode_jsonl(response, schema)
    if errors:
        raise ValueError("; ".join(errors[:5]))
    return data
