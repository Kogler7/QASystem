"""Schema-bounded entity and relation candidate generation.

This module owns prompts, structured LLM retries, and per-chunk candidate
passes.  It deliberately does not resolve identities or accept relations;
those decisions belong to the global candidate pool and verifier.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any

from .chunk_context import ChunkContextManager, ExtractionChunk, ExtractionRequest
from .evidence import Evidence, bind_evidence, locate_quote, verify_evidence
from .graph_utils import canonical_name
from .ontology_compiler import ExtractionPlan
from .ontology_schema import OntologySchema
from ._logging import get_step_logger
from .prompt_router import ChatMessages, DocumentRoute, _looks_like_record_table, build_chat_messages
from .table_semantics import analyze_table_semantics, table_entity_types
from .table_evidence import rule_candidate_in_scope, table_source_span
from .official_document_scope import official_entity_types


logger = get_step_logger(__name__)
from .relation_resolver import enumerate_relation_pairs
from .semantic_evidence import VERIFICATION_SCHEMA, build_verification_messages
from .scheduler import get_llm_scheduler
from .structured_output import validate_json_schema

GLOBAL_RELATION_CATALOG_THRESHOLD = 80
MAX_LOCAL_RELATION_CATALOG = 160
# 批大小受输出上限约束：每条 pair 决策（pair_id+label+evidence.quote）约 60~90
# token，默认 max_tokens=16384 最多容纳约 150~250 条。超限会导致响应 JSON 截断、
# generate_structured 解析失败后整批丢弃（批越大丢得越多），故取 150。
RELATION_PAIR_BATCH_SIZE = 200
# A failed batch may be too large for a model to complete, but recursively
# splitting all the way to single pairs can amplify one permanent failure into
# dozens of calls.  One split recovers the common length/complexity case while
# keeping the worst case bounded at three generated batches per initial batch.
RELATION_MAX_SPLIT_DEPTH = 1
RELATION_VERIFICATION_BATCH_SIZE = 16
SUBSTANTIVE_INVALID_RESPONSE_CHARACTERS = 512
# 表格切片按行拆分：单次调用只裁决一批记录，避免 JSON 被输出上限截断；
# 同时按字符封顶，防止"行少但单元格极长"的表格把单次 prompt 撑爆。
TABLE_ROWS_PER_PART = 20
TABLE_CHARS_PER_PART = 6000
_TABLE_JSON_FENCE = re.compile(
    r"(?ims)^[ \t]*```[ \t]*json[ \t]*\r?\n(?P<body>.*?)^[ \t]*```[ \t]*$"
)
_MARKDOWN_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+.+?\s*$")


def _structured_row_segments(chunk: ExtractionChunk) -> list[tuple[int, ExtractionChunk]]:
    """把超长表格切片按行拆成多个自洽子表，返回 (偏移, 子片)（偏移恒为 0）。

    分片规则：先到 TABLE_ROWS_PER_PART 行、或 TABLE_CHARS_PER_PART 字符即封片。
    子表自带 columns 与完整围栏，是合法 JSON；子表文本由原始单元格值重新序列化，
    引文仍可在原文中逐字定位。返回空表示"不是可拆的结构化表格"，上层回落原逻辑。
    """
    if (chunk.metadata or {}).get("table_windowed"):
        return []
    text = chunk.text or ""
    fenced_blocks = list(_TABLE_JSON_FENCE.finditer(text))
    if len(fenced_blocks) > 1 and not (chunk.metadata or {}).get("table_blocks_split"):
        parts: list[tuple[int, ExtractionChunk]] = []
        previous_end = 0
        table_metadata = (chunk.metadata or {}).get("tables")
        for table_index, match in enumerate(fenced_blocks):
            scaffolding = text[previous_end:match.start()]
            headings = list(_MARKDOWN_HEADING.finditer(scaffolding))
            prefix = scaffolding[headings[-1].start():] if headings else ""
            part_metadata = dict(chunk.metadata)
            part_metadata["table_blocks_split"] = True
            part_metadata["table_source_block_index"] = table_index + 1
            if isinstance(table_metadata, list) and table_index < len(table_metadata):
                table_info = dict(table_metadata[table_index] or {})
                part_metadata["table_count"] = 1
                part_metadata["tables"] = [table_info]
                for key in ("source_file", "sheet_name", "source_row_start", "source_row_end"):
                    if table_info.get(key) is not None:
                        part_metadata[key] = table_info[key]
            parts.append((
                0,
                ExtractionChunk(
                    chunk_id=chunk.chunk_id,
                    text=prefix + match.group(0),
                    document_id=chunk.document_id,
                    section_id=chunk.section_id,
                    section_title=(
                        str(part_metadata.get("sheet_name") or chunk.section_title or "") or None
                    ),
                    clause_number=chunk.clause_number,
                    page_no=chunk.page_no,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    parent_chunk_id=chunk.parent_chunk_id,
                    previous_chunk_id=chunk.previous_chunk_id,
                    next_chunk_id=chunk.next_chunk_id,
                    metadata=part_metadata,
                ),
            ))
            previous_end = match.end()
        return parts
    fence = chr(96) * 3
    open_at = text.find(fence)
    fenced = open_at >= 0
    if fenced:
        body_start = text.find("\n", open_at)
        close_at = text.rfind(fence)
        if body_start < 0 or close_at <= body_start:
            return []
        body = text[body_start + 1:close_at]
        prefix = text[:body_start + 1]
        suffix = text[close_at:]
    else:
        body = text.strip()
        prefix = ""
        suffix = ""
    try:
        payload = json.loads(body)
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return []
    serialized_size = len(text) if fenced else len(body)
    if len(rows) <= TABLE_ROWS_PER_PART and serialized_size <= TABLE_CHARS_PER_PART:
        return []
    windows: list[tuple[int, int, list[Any]]] = []
    window: list[Any] = []
    window_start = 0
    for row_index, row in enumerate(rows):
        candidate_rows = [*window, row]
        candidate_body = json.dumps(
            {**payload, "rows": candidate_rows},
            ensure_ascii=False,
            indent=1,
            default=str,
        )
        candidate_size = (
            len(prefix) + len(candidate_body) + (len(suffix) + 1 if fenced else 0)
        )
        if window and (len(window) >= TABLE_ROWS_PER_PART or candidate_size > TABLE_CHARS_PER_PART):
            windows.append((window_start, row_index, window))
            window = []
            window_start = row_index
        window.append(row)
    if window:
        windows.append((window_start, len(rows), window))
    parts: list[tuple[int, ExtractionChunk]] = []
    for row_start, row_end, window_rows in windows:
        rebuilt_body = json.dumps({**payload, "rows": window_rows}, ensure_ascii=False, indent=1)
        rebuilt = prefix + rebuilt_body + (("\n" + suffix) if fenced else "")
        metadata = dict(chunk.metadata)
        metadata.update(
            {
                "table_windowed": True,
                "table_data_row_start": row_start + 1,
                "table_data_row_end": row_end,
            }
        )
        source_row_start = metadata.get("source_row_start")
        if isinstance(source_row_start, int):
            metadata["source_row_start"] = source_row_start + row_start
            metadata["source_row_end"] = source_row_start + row_end - 1
        parts.append((
            0,
            ExtractionChunk(
                chunk_id=chunk.chunk_id,
                text=rebuilt,
                document_id=chunk.document_id,
                section_id=chunk.section_id,
                section_title=chunk.section_title,
                clause_number=chunk.clause_number,
                page_no=chunk.page_no,
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
                parent_chunk_id=chunk.parent_chunk_id,
                previous_chunk_id=chunk.previous_chunk_id,
                next_chunk_id=chunk.next_chunk_id,
                metadata=metadata,
            ),
        ))
    return parts


def _append_warning(
    warnings: list[dict[str, Any]] | None,
    code: str,
    message: str,
    path: str = "",
    value: Any = None,
) -> None:
    if warnings is None:
        return
    warning: dict[str, Any] = {"code": code, "message": message, "path": path}
    if value is not None:
        warning["value"] = value
    warnings.append(warning)


def decode_json_object(response: Any) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(response, dict):
        return response, None
    response_text = _response_text(response)
    if not response_text.strip():
        return None, "LLM 返回为空"
    stripped = response_text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        stripped = "\n".join(lines[1:]) if len(lines) > 1 else stripped
        if stripped.endswith("```"):
            stripped = stripped[:-3].rstrip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        stripped = stripped[start : end + 1]
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败: {exc.msg}"
    if not isinstance(parsed, dict):
        return None, "LLM 返回顶层必须是 JSON object"
    return parsed, None


def _array_collection_name(schema: dict[str, Any]) -> str | None:
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        return None
    required = schema.get("required") or []
    ordered_names = [*required, *properties]
    for name in dict.fromkeys(str(item) for item in ordered_names):
        field = properties.get(name)
        if isinstance(field, dict) and field.get("type") == "array":
            return name
    return None


def _recover_array_prefix(response: Any, collection_name: str | None) -> list[dict[str, Any]]:
    """Recover only fully decoded object items before a truncated JSON array tail."""
    if not collection_name or not isinstance(response, str):
        return []
    key_match = re.search(rf'"{re.escape(collection_name)}"\s*:', response)
    if key_match is None:
        return []
    array_start = response.find("[", key_match.end())
    if array_start < 0:
        return []

    decoder = json.JSONDecoder()
    cursor = array_start + 1
    recovered: list[dict[str, Any]] = []
    while cursor < len(response):
        while cursor < len(response) and response[cursor].isspace():
            cursor += 1
        if cursor >= len(response) or response[cursor] == "]":
            break
        if response[cursor] == ",":
            cursor += 1
            continue
        try:
            value, end = decoder.raw_decode(response, cursor)
        except json.JSONDecodeError:
            break
        if not isinstance(value, dict):
            break
        recovered.append(value)
        cursor = end
        while cursor < len(response) and response[cursor].isspace():
            cursor += 1
        if cursor < len(response) and response[cursor] not in {",", "]"}:
            break
    return recovered


def _collection_item_key(collection_name: str, item: dict[str, Any]) -> tuple[str, ...]:
    if collection_name == "entities":
        return (
            canonical_name(item.get("type")),
            canonical_name(item.get("name")),
            json.dumps(item.get("properties") or {}, ensure_ascii=False, sort_keys=True, default=str),
            canonical_name((item.get("evidence") or {}).get("quote")),
        )
    if collection_name == "relations":
        return (
            canonical_name(item.get("from_id") or item.get("from_type")),
            canonical_name(item.get("from_name")),
            canonical_name(item.get("rel_type")),
            canonical_name(item.get("to_id") or item.get("to_type")),
            canonical_name(item.get("to_name")),
            canonical_name((item.get("evidence") or {}).get("quote")),
        )
    return (json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),)


def _merge_collection_items(
    collection_name: str,
    existing: list[dict[str, Any]],
    incoming: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = list(existing)
    seen = {_collection_item_key(collection_name, item) for item in merged}
    for item in incoming:
        key = _collection_item_key(collection_name, item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _valid_recovered_items(
    items: Iterable[dict[str, Any]],
    *,
    collection_name: str,
    schema: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if not validate_json_schema({collection_name: [item]}, schema)
    ]


def _recovery_summary(collection_name: str, items: list[dict[str, Any]]) -> str:
    labels: list[str] = []
    for item in items[:80]:
        if collection_name == "entities":
            labels.append(f"{item.get('type', '')}::{item.get('name', '')}")
        else:
            labels.append(
                f"{item.get('from_id', '')}:{item.get('rel_type', '')}:{item.get('to_id', '')}"
            )
    return "; ".join(labels)


def relation_catalog_for_context(
    catalog: list[dict[str, Any]],
    chunk: ExtractionChunk,
    neighbors: Iterable[ExtractionChunk],
    *,
    global_threshold: int = GLOBAL_RELATION_CATALOG_THRESHOLD,
    max_local_entities: int = MAX_LOCAL_RELATION_CATALOG,
) -> list[dict[str, Any]]:
    """Keep small catalogs intact; scope large catalogs to evidenced/local mentions."""
    if len(catalog) <= max(0, global_threshold):
        return list(catalog)

    neighbor_list = list(neighbors)
    local_chunk_ids = {chunk.chunk_id, *(item.chunk_id for item in neighbor_list)}
    current_text = canonical_name(chunk.text)
    context_text = canonical_name("\n".join([chunk.text, *(item.text for item in neighbor_list)]))

    def mentioned(entry: dict[str, Any], text: str) -> bool:
        values = [entry.get("name"), *dict(entry.get("properties") or {}).values()]
        return any(
            normalized and normalized in text
            for value in values
            if (normalized := canonical_name(value))
        )

    current: list[dict[str, Any]] = []
    adjacent: list[dict[str, Any]] = []
    for entry in catalog:
        source_ids = set(entry.get("source_block_ids") or [])
        if chunk.chunk_id in source_ids or mentioned(entry, current_text):
            current.append(entry)
        elif source_ids.intersection(local_chunk_ids) or mentioned(entry, context_text):
            adjacent.append(entry)
    return (current + adjacent)[: max(1, max_local_entities)]

def output_schema_for_types(
    plan: ExtractionPlan,
    schema_name: str,
    type_ids: Iterable[str],
) -> dict[str, Any]:
    schema = deepcopy(plan.output_schemas[schema_name])
    allowed = set(type_ids)
    if schema_name == "entity_candidates":
        type_schema = schema["properties"]["entities"]["items"]["properties"]["type"]
        type_schema["enum"] = [item for item in type_schema["enum"] if item in allowed]
    else:
        relation_schema = schema["properties"]["relations"]["items"]["properties"]["rel_type"]
        relation_schema["enum"] = sorted(
            {
                spec.relation_id
                for specs in plan.relation_specs.values()
                for spec in specs
                if spec.source in allowed and spec.target in allowed
            }
        )
    return schema


def relation_pair_decision_schema(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an exact, pair-keyed decision contract for one relation batch.

    An array schema can constrain the number of rows, but it cannot guarantee
    that every pair is decided exactly once.  Making pair IDs required object
    keys turns omissions, duplicates, and unknown IDs into schema errors that
    the bounded structured-output retry loop can repair.
    """
    pair_ids = [str(pair["pair_id"]) for pair in pairs]
    decision_properties: dict[str, Any] = {}
    for pair in pairs:
        pair_id = str(pair["pair_id"])
        decision_properties[pair_id] = {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "enum": [str(pair["relation_id"]), "NONE", "ABSTAIN"],
                },
                "evidence": {
                    "type": "object",
                    "properties": {"quote": {"type": "string"}},
                    "required": ["quote"],
                    "additionalProperties": False,
                },
            },
            "required": ["label", "evidence"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "object",
                "properties": decision_properties,
                "required": pair_ids,
                "additionalProperties": False,
            }
        },
        "required": ["decisions"],
        "additionalProperties": False,
    }


def _pair_catalog_text(pairs: list[dict[str, Any]]) -> str:
    if not pairs:
        return "（无合法候选 pair）"
    return "\n".join(
        f"- `{p['pair_id']}`: ({p['source_id']} {p['source_type']} {p['source_name']}) "
        f"-[{p['relation_id']}]-> ({p['target_id']} {p['target_type']} {p['target_name']})"
        for p in pairs
    )


def _is_deepseek_llm(llm: Any) -> bool:
    """识别 DeepSeek 系列模型（含 MaaS 上的 deepseek-v4-flash / -0731）。"""
    return str(getattr(llm, "model", "") or "").casefold().startswith("deepseek")


def _is_timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.casefold()


def _is_retryable_exception(exc: Exception) -> bool:
    """Retry transient failures while failing fast on permanent client errors."""
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    try:
        resolved_status = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        resolved_status = None
    if resolved_status is None:
        return True
    if 400 <= resolved_status < 500:
        return resolved_status in {408, 409, 425, 429}
    return True


def _next_response_mode(response_mode: str, error_type: str) -> str:
    return response_mode


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    return str(response or "")


def _response_metadata(response: Any) -> dict[str, Any]:
    if isinstance(response, str) or response is None:
        return {
            "api": "legacy_callable",
            "response_mode": None,
            "status": "completed" if _response_text(response).strip() else "empty",
            "finish_reason": None,
            "incomplete_details": {},
            "usage": None,
            "usage_unavailable": True,
        }
    to_trace = getattr(response, "to_trace", None)
    if callable(to_trace):
        trace = to_trace()
        if isinstance(trace, dict):
            return {
                "api": trace.get("api"),
                "response_mode": trace.get("response_mode"),
                "status": trace.get("status"),
                "finish_reason": trace.get("finish_reason"),
                "incomplete_details": trace.get("incomplete_details") or {},
                "usage": trace.get("usage"),
                "usage_unavailable": bool(trace.get("usage_unavailable", trace.get("usage") is None)),
            }
    usage = getattr(response, "usage", None)
    status = getattr(response, "status", None)
    finish_reason = getattr(response, "finish_reason", None)
    return {
        "api": getattr(response, "api", "unknown"),
        "response_mode": getattr(response, "response_mode", None),
        "status": status or ("completed" if _response_text(response).strip() else "empty"),
        "finish_reason": finish_reason,
        "incomplete_details": getattr(response, "incomplete_details", {}) or {},
        "usage": usage,
        "usage_unavailable": usage is None,
    }


def _pre_parse_response_error(response: Any) -> tuple[str, str] | None:
    metadata = _response_metadata(response)
    status = str(metadata.get("status") or "").casefold()
    finish_reason = str(metadata.get("finish_reason") or "").casefold()
    incomplete_details = metadata.get("incomplete_details") or {}
    reason = str(incomplete_details.get("reason") or "")
    if status == "incomplete" or finish_reason == "length":
        message = "LLM 响应不完整"
        if reason:
            message = f"{message}: {reason}"
        elif finish_reason == "length":
            message = f"{message}: length"
        return "incomplete", message
    if status == "empty" or not _response_text(response).strip():
        return "empty", "LLM 返回为空"
    return None


def _invoke_generation(
    llm: Any,
    schema: dict[str, Any],
    messages: ChatMessages,
    *,
    pass_name: str,
    chunk_id: str,
    attempt_number: int,
    response_mode: str,
    record_table: bool = False,
) -> Any:
    structured = getattr(llm, "generate_structured", None)
    if callable(structured):
        return structured(
            schema,
            messages,
            {
                "schema_name": f"{pass_name}_candidates",
                "pass_name": pass_name,
                "chunk_id": chunk_id,
                "attempt": attempt_number,
                "response_mode": response_mode,
                "record_table": record_table,
            },
        )
    return llm(messages)


def generate_structured(
    llm: Any,
    messages: ChatMessages,
    schema: dict[str, Any],
    *,
    use_scheduler: bool,
    attempts: list[dict[str, Any]],
    pass_name: str,
    chunk_id: str,
    max_attempts: int,
    retry_base_seconds: float,
    call_metrics: list[dict[str, Any]] | None = None,
    retry_empty_collection: bool = False,
    record_table: bool | None = None,
) -> dict[str, Any] | None:
    """Call one required pass with bounded retry and conservative prefix recovery."""
    attempt_messages = messages
    resolved_max_attempts = max(1, int(max_attempts))
    response_mode = "json_schema"
    collection_name = _array_collection_name(schema)
    recovered_items: list[dict[str, Any]] = []
    largest_invalid_response = 0
    record_table_flag = retry_empty_collection if record_table is None else bool(record_table)
    for attempt_number in range(1, resolved_max_attempts + 1):
        started = time.monotonic()
        error_type = ""
        message = ""
        response: Any = None
        retryable = True
        logger.info(
            "[步骤][LLM] 输入: pass=%s chunk=%s attempt=%d/%d mode=%s 消息=%d prompt=%d字符",
            pass_name,
            chunk_id,
            attempt_number,
            resolved_max_attempts,
            response_mode,
            len(attempt_messages),
            sum(len(str(item.get("content") or "")) for item in attempt_messages),
        )
        try:
            if use_scheduler:
                with get_llm_scheduler().request_slot():
                    response = _invoke_generation(
                        llm,
                        schema,
                        attempt_messages,
                        pass_name=pass_name,
                        chunk_id=chunk_id,
                        attempt_number=attempt_number,
                        response_mode=response_mode,
                        record_table=record_table_flag,
                    )
            else:
                response = _invoke_generation(
                    llm,
                    schema,
                    attempt_messages,
                    pass_name=pass_name,
                    chunk_id=chunk_id,
                    attempt_number=attempt_number,
                    response_mode=response_mode,
                    record_table=record_table_flag,
                )
            pre_parse_error = _pre_parse_response_error(response)
            if pre_parse_error is not None:
                error_type, message = pre_parse_error
                response_characters = len(_response_text(response))
                largest_invalid_response = max(largest_invalid_response, response_characters)
                if error_type == "incomplete" and collection_name:
                    recovered = _valid_recovered_items(
                        _recover_array_prefix(_response_text(response), collection_name),
                        collection_name=collection_name,
                        schema=schema,
                    )
                    recovered_items = _merge_collection_items(
                        collection_name,
                        recovered_items,
                        recovered,
                    )
            else:
                parsed, parse_error = decode_json_object(response)
                if parse_error is not None:
                    error_type = "json"
                    message = parse_error
                    response_characters = len(_response_text(response))
                    largest_invalid_response = max(largest_invalid_response, response_characters)
                    if collection_name:
                        recovered = _valid_recovered_items(
                            _recover_array_prefix(_response_text(response), collection_name),
                            collection_name=collection_name,
                            schema=schema,
                        )
                        recovered_items = _merge_collection_items(
                            collection_name,
                            recovered_items,
                            recovered,
                        )
                else:
                    schema_errors = validate_json_schema(parsed, schema)
                    if schema_errors:
                        error_type = "schema"
                        message = "; ".join(schema_errors[:5])
                    elif (
                        collection_name
                        and not (parsed.get(collection_name) or [])
                        and largest_invalid_response >= SUBSTANTIVE_INVALID_RESPONSE_CHARACTERS
                    ):
                        error_type = "schema"
                        message = "前序大体量响应校验失败，重试不得用空集合覆盖"
                    elif (
                        retry_empty_collection
                        and collection_name == "entities"
                        and not (parsed.get(collection_name) or [])
                        and attempt_number < resolved_max_attempts
                    ):
                        error_type = "empty_collection"
                        message = "实体阶段返回空集合，按提示词要求重试补全"
                    else:
                        _record_call(
                            call_metrics,
                            attempt=attempt_number,
                            pass_name=pass_name,
                            chunk_id=chunk_id,
                            status="succeeded",
                            elapsed_seconds=time.monotonic() - started,
                            messages=attempt_messages,
                            response=response,
                            response_mode=response_mode,
                        )
                        logger.info(
                            "[步骤][LLM] 输出: pass=%s chunk=%s attempt=%d status=succeeded response=%d字符 %s=%d 耗时=%.2fs",
                            pass_name,
                            chunk_id,
                            attempt_number,
                            len(_response_text(response)),
                            collection_name or "items",
                            len(parsed.get(collection_name) or []) if collection_name else len(parsed),
                            time.monotonic() - started,
                        )
                        return parsed
        except Exception as exc:  # noqa: BLE001 - recorded as typed run evidence
            error_type = "timeout" if _is_timeout(exc) else "request"
            message = str(exc) or type(exc).__name__
            retryable = _is_retryable_exception(exc)

        next_response_mode = _next_response_mode(response_mode, error_type)
        logger.warning(
            "[步骤][LLM] 输出: pass=%s chunk=%s attempt=%d status=%s response=%d字符 恢复片段=%d 下一模式=%s message=%s",
            pass_name,
            chunk_id,
            attempt_number,
            error_type or "unknown",
            len(_response_text(response)),
            len(recovered_items),
            next_response_mode,
            message,
        )

        response_metadata = _response_metadata(response)
        attempts.append(
            {
                "attempt": attempt_number,
                "type": error_type,
                "pass": pass_name,
                "chunk": chunk_id,
                "api": response_metadata.get("api"),
                "response_mode": response_metadata.get("response_mode") or response_mode,
                "response_status": response_metadata.get("status"),
                "finish_reason": response_metadata.get("finish_reason"),
                "incomplete_details": response_metadata.get("incomplete_details") or {},
                "usage": response_metadata.get("usage"),
                "usage_unavailable": response_metadata.get("usage_unavailable"),
                "next_response_mode": next_response_mode,
                "message": message,
                "response_characters": len(_response_text(response)),
                "recovered_items": len(recovered_items),
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "retryable": retryable,
                "terminal": False,
            }
        )
        _record_call(
            call_metrics,
            attempt=attempt_number,
            pass_name=pass_name,
            chunk_id=chunk_id,
            status="failed",
            elapsed_seconds=time.monotonic() - started,
            messages=attempt_messages,
            response=response,
            error_type=error_type,
            response_mode=response_mode,
        )
        if not retryable or attempt_number >= resolved_max_attempts:
            attempts[-1]["terminal"] = True
            break
        if error_type in {"json", "schema", "empty_collection"}:
            response_size = len(_response_text(response))
            recovery_instruction = ""
            if collection_name and recovered_items:
                recovery_instruction = (
                    f"\n上一次前缀中检测到 {len(recovered_items)} 条完整候选（仅作诊断）："
                    f"{_recovery_summary(collection_name, recovered_items)}\n"
                    "本次必须重新输出该批次的完整 JSON；不要依赖前缀恢复。"
                )
            empty_instruction = ""
            if error_type == "empty_collection":
                empty_instruction = (
                    "上一次输出是空集合，不允许交白卷：请逐条检查 current chunk 中的记录/条目/条款，"
                    "凡可归纳为 ontology 实体的对象都必须输出；只有整个 chunk 与 ontology 完全无关时才可输出空集合。\n"
                )
            attempt_messages = [
                messages[0],
                {
                    "role": "user",
                    "content": (
                        f"{messages[1]['content']}\n\n## 上一次输出校验失败，请纠正后重新输出完整 JSON\n"
                        f"错误类型：{error_type}\n错误：{message}\n"
                        f"上一次输出摘要：已省略原文，字符数 {response_size}\n"
                        f"{recovery_instruction}"
                        f"{empty_instruction}"
                        "不得解释错误，只输出符合 schema 的 JSON 对象。"
                    ),
                },
            ]
        response_mode = next_response_mode
        if retry_base_seconds > 0:
            time.sleep(retry_base_seconds * (2 ** (attempt_number - 1)))
    return None


def verify_items_with_llm(
    llm: Any,
    *,
    chunk: ExtractionChunk,
    items: list[dict[str, Any]],
    pass_name: str = "evidence_verification",
    chunk_id: str = "",
    use_scheduler: bool = False,
    attempts: list[dict[str, Any]] | None = None,
    max_attempts: int = 2,
    retry_base_seconds: float = 0.05,
    call_metrics: list[dict[str, Any]] | None = None,
    record_table: bool = False,
    verdict_diagnostics: dict[str, Any] | None = None,
) -> dict[str, str]:
    """把快速语义比对未通过的候选交给 LLM 核验。

    核验失败（网络/解析问题）返回空 dict。事实写入调用方必须
    fail closed：只有显式 ``yes`` 可以提交，缺失结论不能视为通过。
    """
    if not items:
        return {}
    messages = build_verification_messages(chunk=chunk, items=items)
    try:
        parsed = generate_structured(
            llm,
            messages,
            VERIFICATION_SCHEMA,
            use_scheduler=use_scheduler,
            attempts=attempts if attempts is not None else [],
            pass_name=pass_name,
            chunk_id=chunk_id or chunk.chunk_id,
            max_attempts=max(1, int(max_attempts)),
            retry_base_seconds=retry_base_seconds,
            call_metrics=call_metrics,
            record_table=record_table,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed, but preserve diagnostics
        logger.warning(
            "[步骤][LLM] 证据核验异常: pass=%s chunk=%s type=%s message=%s",
            pass_name,
            chunk_id or chunk.chunk_id,
            type(exc).__name__,
            str(exc),
        )
        if attempts is not None:
            attempts.append({
                "attempt": 1,
                "pass": pass_name,
                "chunk": chunk_id or chunk.chunk_id,
                "type": "request",
                "message": str(exc),
                "incomplete_details": {"exception_type": type(exc).__name__},
                "terminal": True,
            })
        return {}
    verdicts: dict[str, str] = {}
    raw_item_ids: list[str] = []
    if isinstance(parsed, dict):
        for row in parsed.get("verdicts") or []:
            if not isinstance(row, dict):
                continue
            item_id = str(row.get("item_id") or "")
            raw_item_ids.append(item_id)
            supported = str(row.get("supported") or "").casefold()
            if item_id and supported in {"yes", "no", "uncertain"}:
                previous = verdicts.get(item_id)
                if previous is None or previous == supported:
                    verdicts[item_id] = supported
                else:
                    # Contradictory duplicate verdicts are ambiguous regardless
                    # of response order and must fail closed at the caller.
                    verdicts[item_id] = "uncertain"
    if verdict_diagnostics is not None:
        verdict_diagnostics["raw_item_ids"] = raw_item_ids
    return verdicts


def verify_relation_claims(
    llm: Any,
    *,
    chunk: ExtractionChunk,
    relations: list[dict[str, Any]],
    entity_catalog: Iterable[dict[str, Any]] = (),
    use_scheduler: bool = False,
    attempts: list[dict[str, Any]] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    pass_name: str = "relation_evidence_verification",
    chunk_id: str = "",
    max_attempts: int = 2,
    retry_base_seconds: float = 0.05,
    call_metrics: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Fail-closed semantic gate shared by Pass 2 and repair relations.

    Verification is bounded in small batches.  A syntactically valid but
    incomplete verdict set is recorded as a terminal schema failure so a run
    cannot silently report success after dropping undecided relations.
    """
    if not relations:
        return []
    attempt_rows = attempts if attempts is not None else []
    catalog_by_id = {
        str(entity.get("id") or ""): entity
        for entity in entity_catalog
        if isinstance(entity, dict) and str(entity.get("id") or "")
    }
    accepted: list[dict[str, Any]] = []
    resolved_chunk_id = chunk_id or chunk.chunk_id
    for offset in range(0, len(relations), RELATION_VERIFICATION_BATCH_SIZE):
        batch = relations[offset : offset + RELATION_VERIFICATION_BATCH_SIZE]
        items: list[dict[str, Any]] = []
        for index, relation in enumerate(batch, start=offset):
            from_id = str(relation.get("from_id") or "")
            to_id = str(relation.get("to_id") or "")
            source = catalog_by_id.get(from_id) or {}
            target = catalog_by_id.get(to_id) or {}
            items.append({
                "item_id": f"relation-{index}",
                "claim_kind": "relation",
                "source": {
                    "id": from_id,
                    "name": str(relation.get("from_name") or source.get("name") or ""),
                    "type": str(relation.get("from_type") or source.get("type") or ""),
                },
                "relation": str(relation.get("rel_type") or ""),
                "target": {
                    "id": to_id,
                    "name": str(relation.get("to_name") or target.get("name") or ""),
                    "type": str(relation.get("to_type") or target.get("type") or ""),
                },
                "evidence_quote": str((relation.get("evidence") or {}).get("quote") or ""),
            })
        verdict_diagnostics: dict[str, Any] = {}
        verdicts = verify_items_with_llm(
            llm,
            chunk=chunk,
            items=items,
            pass_name=pass_name,
            chunk_id=f"{resolved_chunk_id}.batch-{offset // RELATION_VERIFICATION_BATCH_SIZE}",
            use_scheduler=use_scheduler,
            attempts=attempt_rows,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
            call_metrics=call_metrics,
            verdict_diagnostics=verdict_diagnostics,
        )
        expected_ids = {str(item["item_id"]) for item in items}
        raw_item_ids = [
            str(item_id)
            for item_id in verdict_diagnostics.get("raw_item_ids") or []
        ]
        duplicate_ids = sorted({
            item_id for item_id in raw_item_ids if raw_item_ids.count(item_id) > 1
        })
        unknown_ids = sorted(set(raw_item_ids) - expected_ids)
        missing_ids = sorted(expected_ids - set(verdicts))
        protocol_errors = {
            "missing_item_ids": missing_ids,
            "duplicate_item_ids": duplicate_ids,
            "unknown_item_ids": unknown_ids,
        }
        if any(protocol_errors.values()):
            attempt_rows.append({
                "attempt": 1,
                "type": "schema",
                "pass": pass_name,
                "chunk": resolved_chunk_id,
                "message": "关系语义核验 item_id 集合不完整或不唯一",
                "incomplete_details": protocol_errors,
                "terminal": True,
            })
            _append_warning(
                warnings,
                "RELATION_EVIDENCE_VERIFIER_INCOMPLETE",
                "关系语义核验 item_id 集合不完整或不唯一",
                resolved_chunk_id,
                protocol_errors,
            )
        for duplicate_id in duplicate_ids:
            verdicts.pop(duplicate_id, None)
        for relation, item in zip(batch, items):
            item_id = str(item["item_id"])
            verdict = verdicts.get(item_id)
            if verdict == "yes":
                accepted.append(relation)
                continue
            _append_warning(
                warnings,
                "RELATION_SEMANTIC_EVIDENCE_REJECTED",
                "关系 evidence 未能明确支持完整三元组，候选已拒绝",
                f"{resolved_chunk_id}.{item_id}",
                {
                    "from_id": relation.get("from_id"),
                    "to_id": relation.get("to_id"),
                    "rel_type": relation.get("rel_type"),
                    "verdict": verdict or "missing",
                },
            )
    return accepted


def _normalize_entities(
    parsed: dict[str, Any],
    schema: OntologySchema,
    target_types: Iterable[str] | None,
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sanitized, validation_warnings = schema.validate_extraction(
        parsed,
        target_types=target_types,
        mode="strict",
    )
    warnings.extend(validation_warnings)
    return schema.normalize_extraction(sanitized).get("entities", [])


def _normalize_span_entities(
    parsed: dict[str, Any],
    *,
    chunk: ExtractionChunk,
    schema: OntologySchema,
    target_types: Iterable[str] | None,
    warnings: list[dict[str, Any]],
    deduplicate: bool = True,
) -> list[dict[str, Any]]:
    """Turn strict span candidates into the legacy graph entity contract.

    The model never supplies canonical names or properties.  A name may be a
    faithful abstraction of the source (e.g. an inferred rule name), but every
    accepted fact must still carry an ``evidence.quote`` locatable in the chunk,
    and all grounding decisions are made here so malformed offsets cannot create
    a plausible-looking node.
    """
    raw_entities = parsed.get("entities", []) if isinstance(parsed, dict) else []
    if not isinstance(raw_entities, list):
        _append_warning(warnings, "INVALID_ENTITIES", "entities 必须是数组", "entities")
        return []
    requested = [schema.resolve_class(item) for item in (target_types or [])]
    requested = [item for item in requested if item is not None]
    accepted: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    text = chunk.text or ""
    for index, raw in enumerate(raw_entities):
        path = f"entities[{index}]"
        if not isinstance(raw, dict):
            _append_warning(warnings, "INVALID_ENTITY", "实体必须是对象", path)
            continue
        ontology_class = schema.resolve_class(raw.get("type"))
        if ontology_class is None:
            _append_warning(warnings, "UNKNOWN_ENTITY_TYPE", "实体类型不在 ontology 中", f"{path}.type", raw.get("type"))
            continue
        if requested and not any(schema.is_assignable(ontology_class.identifier, item.identifier) for item in requested):
            _append_warning(warnings, "ENTITY_OUT_OF_SCOPE", "实体类型不在目标范围内", f"{path}.type", ontology_class.identifier)
            continue
        evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
        raw_mention = raw.get("mention")
        raw_name = raw.get("name")
        name = next(
            (
                value.strip()
                for value in (raw_mention, raw_name)
                if isinstance(value, str) and value.strip()
            ),
            "",
        )
        quote = evidence.get("quote") if isinstance(evidence.get("quote"), str) else ""
        if not name:
            _append_warning(warnings, "INVALID_ENTITY_MENTION", "mention 必须是非空字符串", f"{path}.mention")
            continue

        # 实体名允许是归纳得到的（如规则名），不要求逐字；但证据必须落到原文，
        # 因此 name 或 evidence.quote 至少有一个能在当前 chunk 中逐字定位。
        name_in_text = name in text
        quote_in_text = bool(quote) and quote in text
        start = raw.get("start_char")
        end = raw.get("end_char")
        offsets_match = (
            isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start <= end <= len(text)
            and text[start:end] == name
        )
        if name_in_text:
            starts = [match.start() for match in re.finditer(re.escape(name), text)]
            if offsets_match:
                pass
            elif len(starts) == 1:
                start = starts[0]
                end = start + len(name)
                _append_warning(warnings, "ENTITY_SPAN_OFFSET_CORRECTED", "offset 与 mention 不一致，已按唯一原文位置纠正", path)
            elif quote_in_text and name in quote:
                quote_start = text.find(quote)
                start = quote_start + quote.index(name)
                end = start + len(name)
                _append_warning(warnings, "ENTITY_SPAN_OFFSET_CORRECTED", "offset 与 mention 不一致，已按证据 quote 定位纠正", path)
            else:
                start = starts[0]
                end = start + len(name)
                _append_warning(warnings, "ENTITY_SPAN_OFFSET_CORRECTED", "offset 与 mention 不一致，已按首个原文位置纠正", path)
        elif quote_in_text:
            # 归纳型实体名：用逐字 evidence.quote 承载证据 span。
            quote_start = text.find(quote)
            start = quote_start
            end = quote_start + len(quote)
            _append_warning(
                warnings,
                "ENTITY_NAME_UNGROUNDED",
                "mention 无法逐字定位，已保留归纳型实体名并以 evidence.quote 作为证据 span",
                path,
                name,
            )
        else:
            _append_warning(
                warnings,
                "ENTITY_EVIDENCE_REJECTED",
                "mention 与 evidence.quote 均无法在当前 chunk 定位，候选已拒绝",
                path,
                name,
            )
            continue

        key = (ontology_class.identifier, name)
        if deduplicate and key in seen:
            _append_warning(warnings, "ENTITY_DUPLICATE_MENTION", "同一 type+mention 在 chunk 内重复，已去重", path)
            continue
        seen.add(key)
        accepted.append({
            "type": ontology_class.identifier,
            "name": name,
            "properties": {},
            "evidence": {"quote": quote, "start_char": start, "end_char": end},
        })
    return accepted


def bind_entity_evidence(
    entities: list[dict[str, Any]],
    manager: ChunkContextManager,
    *,
    chunk_ids: Iterable[str] | None = None,
    source_chunk: ExtractionChunk | None = None,
) -> list[dict[str, Any]]:
    # Preserve the selected table row across repeated values and reconstructed
    # windows. Global first-quote matching can otherwise change its provenance.
    verified_spans: dict[int, dict[str, Any]] = {}
    if source_chunk is not None:
        original_chunk = manager.get_chunk(source_chunk.chunk_id)
        for index, entity in enumerate(entities):
            span = table_source_span(source_chunk, original_chunk, entity)
            if span is not None:
                start, end = span
                verified_spans[index] = Evidence(
                    span_id=f"{original_chunk.chunk_id}:{start}:{end}", source_block_ids=[original_chunk.chunk_id],
                    quote=original_chunk.text[start:end], start_char=start, end_char=end, evidence_type="explicit",
                ).to_dict()
    bound = bind_evidence(
        {"entities": entities, "relations": []},
        manager,
        chunk_ids=chunk_ids,
    )
    for index, evidence in verified_spans.items():
        bound["entities"][index]["evidence"] = evidence
    return bound.get("entities", [])


def bind_relation_evidence(
    raw_relation: dict[str, Any],
    manager: ChunkContextManager,
    *,
    chunk_ids: Iterable[str] | None = None,
) -> Evidence | None:
    quote = (raw_relation.get("evidence") or {}).get("quote")
    if quote:
        evidence_chunk_ids = (raw_relation.get("evidence") or {}).get("source_block_ids")
        located = locate_quote(
            manager,
            quote,
            chunk_ids=(chunk_ids if chunk_ids is not None else evidence_chunk_ids or None),
        )
        if located is not None:
            return located
    # Endpoint mentions alone do not prove the relation semantics.  Leave the
    # candidate without evidence so the verifier can reject/repair it.
    return None


def entity_pass(
    plan: ExtractionPlan,
    schema: OntologySchema,
    manager: ChunkContextManager,
    request: ExtractionRequest,
    llm: Any,
    *,
    route: DocumentRoute,
    route_map: dict[str, DocumentRoute] | None = None,
    max_workers: int,
    use_scheduler: bool,
    attempts: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    local_previous: int,
    local_next: int,
    same_section: bool,
    known_entities: list[dict[str, Any]] | None = None,
    max_attempts: int = 3,
    retry_base_seconds: float = 0.05,
    call_metrics: list[dict[str, Any]] | None = None,
) -> list[tuple[str, list[dict[str, Any]]]]:
    chunks = list(request.chunks)

    def run(chunk: ExtractionChunk) -> tuple[
        str,
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        local_attempts: list[dict[str, Any]] = []
        local_warnings: list[dict[str, Any]] = []
        local_call_metrics: list[dict[str, Any]] = []
        # 表格切片按行拆子表：几十/几百/几千行的 Excel 若整块送进单次调用，
        # 要么被输出上限截断、要么 prompt 过大超时，最终整片判失败、整篇报错。
        # 拆成自洽子表后每片只裁决 TABLE_ROWS_PER_PART 行，单次调用恒有界；
        # 子表行数不超过上限，因此递归在下一层自然终止。
        row_parts = _structured_row_segments(chunk)
        if row_parts:
            merged_entities: list[dict[str, Any]] = []
            for _, part in row_parts:
                _, part_entities, part_attempts, part_warnings, part_metrics = run(part)
                local_attempts.extend(part_attempts)
                local_warnings.extend(part_warnings)
                local_call_metrics.extend(part_metrics)
                merged_entities.extend(part_entities)
            if any((part.metadata or {}).get("table_blocks_split") for _, part in row_parts):
                _append_warning(
                    local_warnings,
                    "ENTITY_TABLE_SPLIT_BY_BLOCKS",
                    f"包含多个 JSON 表格的切片已拆为 {len(row_parts)} 个独立表格",
                    chunk.chunk_id,
                )
            elif len(row_parts) == 1:
                _append_warning(
                    local_warnings,
                    "ENTITY_TABLE_OVERSIZED_ATOMIC",
                    "表格的列定义或单行内容已超过字符预算，无法无损继续拆分，已保持完整并单独抽取",
                    chunk.chunk_id,
                )
            else:
                _append_warning(
                    local_warnings,
                    "ENTITY_TABLE_SPLIT_BY_ROWS",
                    f"表格切片按行拆为 {len(row_parts)} 个子表",
                    chunk.chunk_id,
                )
            return chunk.chunk_id, merged_entities, local_attempts, local_warnings, local_call_metrics
        neighbors = [
            item
            for item in manager.local_context(
                chunk.chunk_id,
                previous=local_previous,
                next=local_next,
                same_section=same_section,
            )
            if item.chunk_id != chunk.chunk_id
        ]
        scoped_known_entities = relation_catalog_for_context(
            known_entities or [],
            chunk,
            neighbors,
        )
        allowed_entity_types = official_entity_types(
            plan, chunk, table_entity_types(plan, chunk), (route_map or {}).get(chunk.chunk_id, route)
        )
        if not allowed_entity_types:
            _append_warning(
                local_warnings,
                "TABLE_NO_SUPPORTED_ENTITY_TYPES",
                "表格没有支持本体规则语义的记录，且本体未开放其他事实类型，已跳过规则生成",
                chunk.chunk_id,
            )
            return chunk.chunk_id, [], local_attempts, local_warnings, local_call_metrics
        output_schema = output_schema_for_types(
            plan,
            "entity_candidates",
            allowed_entity_types,
        )
        messages = build_chat_messages(
            task="entity",
            plan=plan,
            route=(route_map or {}).get(chunk.chunk_id, route),
            chunk=chunk,
            local_context="\n".join(item.text for item in neighbors),
            known_entities=scoped_known_entities,
            output_schema=output_schema,
        )
        retry_empty = bool(_looks_like_record_table(chunk) and _is_deepseek_llm(llm))
        parsed = generate_structured(
            llm,
            messages,
            output_schema,
            use_scheduler=use_scheduler,
            attempts=local_attempts,
            pass_name="entity",
            chunk_id=chunk.chunk_id,
            max_attempts=max(max_attempts, 4) if retry_empty else max_attempts,
            retry_base_seconds=retry_base_seconds,
            call_metrics=local_call_metrics,
            retry_empty_collection=retry_empty,
        )
        if parsed is None:
            return chunk.chunk_id, [], local_attempts, local_warnings, local_call_metrics
        entities = _normalize_span_entities(
            parsed,
            chunk=chunk,
            schema=schema,
            target_types=sorted(allowed_entity_types),
            warnings=local_warnings,
            deduplicate=False,
        )
        for entity in entities:
            if entity.get("type") not in allowed_entity_types:
                _append_warning(
                    local_warnings, "ENTITY_OUT_OF_SCOPE",
                    "实体类型未通过当前表格语义范围校验，候选已拒绝",
                    chunk.chunk_id, entity.get("type"),
                )
        entities = [entity for entity in entities if entity.get("type") in allowed_entity_types]
        table_decision = analyze_table_semantics(plan, chunk)
        scoped_entities = []
        for entity in entities:
            if rule_candidate_in_scope(chunk, table_decision, entity):
                scoped_entities.append(entity)
            else:
                _append_warning(
                    local_warnings, "RULE_TABLE_ROW_OUT_OF_SCOPE",
                    "规则候选证据未落在自动判定允许的记录行，候选已拒绝",
                    chunk.chunk_id, entity.get("name"),
                )
        bound_entities = bind_entity_evidence(
            scoped_entities,
            manager,
            chunk_ids=(chunk.chunk_id,),
            source_chunk=chunk,
        )
        grounded_entities: list[dict[str, Any]] = []
        seen_entities: set[tuple[str, str]] = set()
        original_chunk = manager.get_chunk(chunk.chunk_id)
        original_decision = analyze_table_semantics(plan, original_chunk)
        for index, entity in enumerate(bound_entities):
            if not rule_candidate_in_scope(original_chunk, original_decision, entity):
                _append_warning(
                    local_warnings, "RULE_TABLE_ROW_OUT_OF_SCOPE",
                    "绑定后的原文证据未落在允许的规则记录行，候选已拒绝",
                    chunk.chunk_id, entity.get("name"),
                )
                continue
            verdicts = verify_evidence(
                {"entities": [entity], "relations": []},
                manager,
            )
            if verdicts and all(verdict.status == "ok" for verdict in verdicts):
                key = (entity["type"], entity["name"])
                if key not in seen_entities:
                    grounded_entities.append(entity)
                    seen_entities.add(key)
                continue
            evidence = entity.get("evidence") or {}
            _append_warning(
                local_warnings,
                "ENTITY_EVIDENCE_REJECTED",
                "实体 evidence 未通过 source/span/quote 一致性校验，候选已拒绝",
                f"{chunk.chunk_id}.entities[{index}].evidence",
                evidence.get("quote"),
            )
        return (
            chunk.chunk_id,
            grounded_entities,
            local_attempts,
            local_warnings,
            local_call_metrics,
        )

    worker_count = max(1, max_workers)
    if worker_count == 1:
        worker_results = [run(chunk) for chunk in chunks]
    else:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ke-entity") as executor:
            worker_results = list(executor.map(run, chunks))
    results: list[tuple[str, list[dict[str, Any]]]] = []
    for chunk_id, entities, local_attempts, local_warnings, local_calls in worker_results:
        attempts.extend(local_attempts)
        warnings.extend(local_warnings)
        if call_metrics is not None:
            call_metrics.extend(local_calls)
        results.append((chunk_id, entities))
    return results


def relation_pass(
    plan: ExtractionPlan,
    manager: ChunkContextManager,
    request: ExtractionRequest,
    llm: Any,
    pool: Any,
    *,
    route: DocumentRoute,
    route_map: dict[str, DocumentRoute] | None = None,
    max_workers: int,
    use_scheduler: bool,
    attempts: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    local_previous: int,
    local_next: int,
    same_section: bool,
    max_attempts: int = 3,
    retry_base_seconds: float = 0.05,
    call_metrics: list[dict[str, Any]] | None = None,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Generate raw relation candidates from bounded same-document windows."""
    chunks = list(request.chunks)
    catalog = pool.entity_catalog()
    entity_objects = pool.accepted_entities()
    context_ids_by_chunk: dict[str, tuple[str, ...]] = {}
    for chunk in chunks:
        document_id = (chunk.document_id or "").strip()
        # Relation extraction treats an absent document_id as its own explicit
        # group instead of the manager's compatibility wildcard.  This keeps a
        # mixed known/unknown multi-document request from creating cross-doc
        # relation windows.
        context_ids_by_chunk[chunk.chunk_id] = tuple(
            item.chunk_id
            for item in manager.local_context(
                chunk.chunk_id,
                previous=local_previous,
                next=local_next,
                same_section=same_section,
            )
            if (item.document_id or "").strip() == document_id
        )
    entity_chunk_ids: dict[str, set[str]] = {}
    for entity in entity_objects:
        evidenced = {
            block_id
            for evidence in entity.evidence
            for block_id in evidence.source_block_ids
            if manager.has_chunk(block_id)
        }
        normalized_name = canonical_name(entity.name)
        if normalized_name:
            evidenced.update(
                chunk.chunk_id
                for chunk in chunks
                if normalized_name in canonical_name(chunk.text)
            )
        entity_chunk_ids[entity.candidate_id] = evidenced
    anchor_ids = {
        entity.candidate_id
        for entity in entity_objects
        if entity.source == "document_anchor"
    }

    def anchors_for(chunk_id: str) -> set[str]:
        """Keep document anchors inside their evidenced document when known."""
        chunk = manager.get_chunk(chunk_id)
        document_id = (chunk.document_id or "").strip()
        scoped: set[str] = set()
        for anchor_id in anchor_ids:
            evidence_docs = {
                (manager.get_chunk(block_id).document_id or "").strip()
                for block_id in entity_chunk_ids.get(anchor_id, set())
                if manager.has_chunk(block_id)
            }
            known_evidence_docs = evidence_docs - {""}
            if document_id:
                accepted = not known_evidence_docs or document_id in known_evidence_docs
            else:
                accepted = not known_evidence_docs
            if accepted:
                scoped.add(anchor_id)
        return scoped

    permitted_by_chunk: dict[str, set[str]] = {}
    for chunk in chunks:
        window_ids = set(context_ids_by_chunk[chunk.chunk_id])
        window_entities = {
            entity_id
            for entity_id, evidence_ids in entity_chunk_ids.items()
            if evidence_ids.intersection(window_ids)
        }
        permitted_by_chunk[chunk.chunk_id] = window_entities | anchors_for(chunk.chunk_id)

    anchor_types = {
        canonical_name(entity.canonical_type)
        for entity in entity_objects
        if entity.candidate_id in anchor_ids
    }
    owned_pairs_by_chunk: dict[str, list[dict[str, Any]]] = {
        chunk.chunk_id: [] for chunk in chunks
    }
    assigned_pair_ids: set[str] = set()
    for candidate_chunk in chunks:
        permitted = permitted_by_chunk[candidate_chunk.chunk_id]
        permitted_objects = [
            entity for entity in entity_objects
            if entity.candidate_id in permitted
        ]
        for pair in enumerate_relation_pairs(plan, permitted_objects):
            pair_id = str(pair.get("pair_id") or "")
            if not pair_id or pair_id in assigned_pair_ids:
                continue
            if (
                canonical_name(pair["source_type"]) in anchor_types
                and pair["source_id"] not in anchor_ids
            ):
                continue
            source_id = str(pair.get("source_id") or "")
            target_id = str(pair.get("target_id") or "")
            non_anchor_ids = {source_id, target_id} - anchor_ids
            # Anchor-to-entity relations belong to a chunk where the business
            # endpoint is actually mentioned, not merely a preceding window.
            if len(non_anchor_ids) == 1 and not any(
                candidate_chunk.chunk_id in entity_chunk_ids.get(entity_id, set())
                for entity_id in non_anchor_ids
            ):
                continue
            assigned_pair_ids.add(pair_id)
            owned_pairs_by_chunk[candidate_chunk.chunk_id].append(pair)

    def run(chunk: ExtractionChunk) -> tuple[
        str,
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        local_attempts: list[dict[str, Any]] = []
        local_warnings: list[dict[str, Any]] = []
        local_call_metrics: list[dict[str, Any]] = []
        window_chunk_ids = context_ids_by_chunk[chunk.chunk_id]
        neighbors = [
            manager.get_chunk(chunk_id)
            for chunk_id in window_chunk_ids
            if chunk_id != chunk.chunk_id
        ]
        permitted_ids = permitted_by_chunk[chunk.chunk_id]
        local_catalog = [
            entry
            for entry in catalog
            if str(entry.get("id") or "") in permitted_ids
        ]
        if not local_catalog:
            return chunk.chunk_id, [], local_attempts, local_warnings, local_call_metrics
        # Ownership was precomputed once in stable chunk order.  Workers only
        # consume their immutable slice, avoiding repeated O(chunks) scans.
        pairs = owned_pairs_by_chunk[chunk.chunk_id]
        if not pairs:
            return chunk.chunk_id, [], local_attempts, local_warnings, local_call_metrics
        all_relations: list[dict[str, Any]] = []
        chunk_route = (route_map or {}).get(chunk.chunk_id, route)
        pending_batches = [
            (
                pairs[offset : offset + RELATION_PAIR_BATCH_SIZE],
                f"{offset // RELATION_PAIR_BATCH_SIZE}",
                0,
            )
            for offset in range(0, len(pairs), RELATION_PAIR_BATCH_SIZE)
        ]
        while pending_batches:
            pair_batch, batch_tag, split_depth = pending_batches.pop(0)
            output_schema = relation_pair_decision_schema(pair_batch)
            messages = build_chat_messages(
                task="relation",
                plan=plan,
                route=chunk_route,
                chunk=chunk,
                local_context="\n\n".join(
                    f"[chunk_id={item.chunk_id}]\n{item.text}"
                    for item in neighbors
                ),
                known_entities=local_catalog,
                output_schema=output_schema,
                relation_pairs=pair_batch,
            )
            attempt_start = len(local_attempts)
            parsed = generate_structured(
                llm,
                messages,
                output_schema,
                use_scheduler=use_scheduler,
                attempts=local_attempts,
                pass_name="relation",
                chunk_id=f"{chunk.chunk_id}.batch-{batch_tag}",
                max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds,
                call_metrics=local_call_metrics,
            )
            if parsed is None:
                batch_attempts = local_attempts[attempt_start:]
                terminal_type = str((batch_attempts[-1] if batch_attempts else {}).get("type") or "")
                split_eligible = terminal_type in {"empty", "incomplete", "json", "schema"}
                if (
                    len(pair_batch) > 1
                    and split_depth < RELATION_MAX_SPLIT_DEPTH
                    and split_eligible
                ):
                    # The failed parent batch is recovered by an explicit
                    # fallback path.  It must not remain a terminal run
                    # failure when its child batches subsequently succeed.
                    for attempt in batch_attempts:
                        attempt["terminal"] = False
                        attempt["recovered_by"] = "relation_batch_split"
                    midpoint = len(pair_batch) // 2
                    pending_batches[0:0] = [
                        (pair_batch[:midpoint], f"{batch_tag}a", split_depth + 1),
                        (pair_batch[midpoint:], f"{batch_tag}b", split_depth + 1),
                    ]
                    _append_warning(
                        local_warnings,
                        "RELATION_BATCH_SPLIT_RETRY",
                        "关系批次多次生成失败，已拆分为更小批次重试",
                        f"relation_batches[{batch_tag}]",
                        {"pair_count": len(pair_batch)},
                    )
                else:
                    pair_ids = [str(pair["pair_id"]) for pair in pair_batch]
                    code = (
                        "RELATION_PAIR_UNDECIDED"
                        if len(pair_batch) == 1
                        else "RELATION_BATCH_UNDECIDED"
                    )
                    _append_warning(
                        local_warnings,
                        code,
                        "关系候选生成失败，未将缺失决策解释为 NONE",
                        f"relation_batches[{batch_tag}]",
                        {
                            "failure_type": terminal_type or "unknown",
                            "pair_ids": pair_ids,
                            "split_depth": split_depth,
                        },
                    )
                continue
            by_id = {pair["pair_id"]: pair for pair in pair_batch}
            decisions = parsed.get("decisions")
            if not isinstance(decisions, dict):
                _append_warning(
                    local_warnings,
                    "INVALID_RELATION_DECISIONS",
                    "关系决策必须是以 pair_id 为键的对象",
                    "decisions",
                )
                continue
            for pair_id, pair in by_id.items():
                decision = decisions.get(pair_id)
                if not isinstance(decision, dict):
                    _append_warning(
                        local_warnings,
                        "MISSING_RELATION_PAIR_DECISION",
                        "关系 pair 缺少明确决策，未将其解释为 NONE",
                        f"decisions.{pair_id}",
                        pair_id,
                    )
                    continue
                label = str(decision.get("label") or "")
                if label == "NONE":
                    continue
                if label == "ABSTAIN":
                    _append_warning(
                        local_warnings,
                        "RELATION_PAIR_ABSTAINED",
                        "关系 pair 因证据不足或指代歧义主动弃权，未解释为 NONE",
                        f"decisions.{pair_id}.label",
                        {
                            "pair_id": pair_id,
                            "from_id": pair["source_id"],
                            "to_id": pair["target_id"],
                            "rel_type": pair["relation_id"],
                        },
                    )
                    continue
                if label != pair["relation_id"]:
                    _append_warning(local_warnings, "RELATION_PAIR_SIGNATURE_MISMATCH", "关系标签不符合 pair 本体签名", f"decisions.{pair_id}.label", label)
                    continue
                evidence = bind_relation_evidence(
                    decision,
                    manager,
                    chunk_ids=window_chunk_ids,
                )
                if evidence is None:
                    quote = str((decision.get("evidence") or {}).get("quote") or "")
                    _append_warning(local_warnings, "RELATION_EVIDENCE_INVALID", "关系 evidence 无法在有界上下文窗口定位", f"decisions.{pair_id}.evidence", quote)
                    continue
                all_relations.append({
                    "from_id": pair["source_id"], "to_id": pair["target_id"],
                    "from_name": pair["source_name"], "to_name": pair["target_name"],
                    "rel_type": label, "evidence": evidence.to_dict(),
                })
        if all_relations:
            relations_by_evidence_chunk: dict[str, list[dict[str, Any]]] = {}
            for relation in all_relations:
                source_ids = list((relation.get("evidence") or {}).get("source_block_ids") or [])
                evidence_chunk_id = source_ids[0] if source_ids else chunk.chunk_id
                relations_by_evidence_chunk.setdefault(evidence_chunk_id, []).append(relation)
            all_relations = []
            for evidence_chunk_id, chunk_relations in relations_by_evidence_chunk.items():
                all_relations.extend(verify_relation_claims(
                    llm,
                    chunk=manager.get_chunk(evidence_chunk_id),
                    relations=chunk_relations,
                    entity_catalog=local_catalog,
                    pass_name="relation_evidence_verification",
                    chunk_id=evidence_chunk_id,
                    use_scheduler=use_scheduler,
                    attempts=local_attempts,
                    warnings=local_warnings,
                    max_attempts=max_attempts,
                    retry_base_seconds=retry_base_seconds,
                    call_metrics=local_call_metrics,
                ))
            for relation in all_relations:
                relation.pop("from_name", None)
                relation.pop("to_name", None)
        return chunk.chunk_id, all_relations, local_attempts, local_warnings, local_call_metrics

    worker_count = max(1, max_workers)
    if worker_count == 1:
        worker_results = [run(chunk) for chunk in chunks]
    else:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ke-relation") as executor:
            worker_results = list(executor.map(run, chunks))
    results: list[tuple[str, list[dict[str, Any]]]] = []
    for chunk_id, relations, local_attempts, local_warnings, local_calls in worker_results:
        attempts.extend(local_attempts)
        warnings.extend(local_warnings)
        if call_metrics is not None:
            call_metrics.extend(local_calls)
        results.append((chunk_id, relations))
    return results


def _record_call(
    call_metrics: list[dict[str, Any]] | None,
    *,
    attempt: int,
    pass_name: str,
    chunk_id: str,
    status: str,
    elapsed_seconds: float,
    messages: ChatMessages,
    response: Any,
    error_type: str | None = None,
    response_mode: str | None = None,
) -> None:
    if call_metrics is None:
        return
    response_metadata = _response_metadata(response)
    text = _response_text(response)
    usage = response_metadata.get("usage")
    call_metrics.append(
        {
            "attempt": attempt,
            "pass": pass_name,
            "chunk": chunk_id,
            "status": status,
            "error_type": error_type,
            "api": response_metadata.get("api"),
            "response_mode": response_metadata.get("response_mode") or response_mode,
            "response_status": response_metadata.get("status"),
            "finish_reason": response_metadata.get("finish_reason"),
            "incomplete_details": response_metadata.get("incomplete_details") or {},
            "elapsed_seconds": round(elapsed_seconds, 6),
            "input_characters": sum(len(message.get("content", "")) for message in messages),
            "output_characters": len(text),
            "usage": usage,
            "usage_unavailable": response_metadata.get("usage_unavailable"),
            "token_usage": usage,
            "messages": [dict(message) for message in messages],
        }
    )


__all__ = [
    "bind_entity_evidence",
    "bind_relation_evidence",
    "decode_json_object",
    "entity_pass",
    "generate_structured",
    "output_schema_for_types",
    "relation_catalog_for_context",
    "relation_pair_decision_schema",
    "relation_pass",
    "verify_relation_claims",
]
