"""Schema-bounded entity candidate generation.

This module owns bounded source windows, JSONL retries, local properties and
source binding. Exact type/name grouping belongs to property_enrichment.
"""
from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv

import json
import re
import time
from algorithm.extraction_trace import traced_generation
from copy import deepcopy
from collections.abc import Iterable
from typing import Any

from .chunk_context import ChunkContextManager, ExtractionChunk, ExtractionRequest
from .evidence import Evidence, bind_evidence, locate_quote
from .graph_utils import append_warning as _append_warning, canonical_name
from .ontology_compiler import ExtractionPlan
from .output_contracts import output_schema_for_types, validate_candidate_properties
from .request_budget import (fits, input_limit, message_bytes, window_chars, windows, iter_windows,
    split_request_failure, recover_split_attempts, document_expired, RecoveryBudget, fair_source_windows)
from .source_windows import source_parts
from .ontology_schema import OntologySchema
from ._logging import get_step_logger
from .prompt_router import ChatMessages, DocumentRoute, _looks_like_record_table, build_chat_messages
from .prompt_templates import user_with_retry_feedback
from .task_runtime import synchronous, invoke, ordered_tasks, current_runtime, CallBudgetExceeded


logger = get_step_logger(__name__)
from .semantic_evidence import build_verification_messages, semantic_support
from .output_contracts import verification_schema, validate_verification_items
from .scheduler import get_llm_scheduler
from .structured_output import validate_json_schema

GLOBAL_RELATION_CATALOG_THRESHOLD = 80
MAX_LOCAL_RELATION_CATALOG = 160
SUBSTANTIVE_INVALID_RESPONSE_CHARACTERS = 512
# 表格切片按行拆分：单次调用只裁决一批记录，避免 JSON 被输出上限截断；
# 同时按字符封顶，防止"行少但单元格极长"的表格把单次 prompt 撑爆。
TABLE_ROWS_PER_PART = 20
TABLE_CHARS_PER_PART = 6000


def _structured_row_segments(chunk: ExtractionChunk) -> list[tuple[int, ExtractionChunk]]:
    """把超长表格切片按行拆成多个自洽子表，返回 (偏移, 子片)（偏移恒为 0）。

    分片规则：先到 TABLE_ROWS_PER_PART 行、或 TABLE_CHARS_PER_PART 字符即封片。
    子表自带 columns 与完整围栏，是合法 JSON；子表文本由原始单元格值重新序列化，
    引文仍可在原文中逐字定位。返回空表示"不是可拆的结构化表格"，上层回落原逻辑。
    """
    text = chunk.text or ""
    fence = chr(96) * 3
    open_at = text.find(fence)
    if open_at < 0:
        return []
    body_start = text.find("\n", open_at)
    close_at = text.rfind(fence)
    if body_start < 0 or close_at <= body_start:
        return []
    try:
        payload = json.loads(text[body_start + 1:close_at])
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) <= TABLE_ROWS_PER_PART:
        return []
    prefix = text[:body_start + 1]
    suffix = text[close_at:]
    windows: list[list[Any]] = []
    window: list[Any] = []
    size = 0
    for row in rows:
        row_size = len(json.dumps(row, ensure_ascii=False, default=str))
        if window and (len(window) >= TABLE_ROWS_PER_PART or size + row_size > TABLE_CHARS_PER_PART):
            windows.append(window)
            window = []
            size = 0
        window.append(row)
        size += row_size
    if window:
        windows.append(window)
    if len(windows) < 2:
        return []
    parts: list[tuple[int, ExtractionChunk]] = []
    for window in windows:
        rebuilt = prefix + json.dumps({**payload, "rows": window}, ensure_ascii=False, indent=1) + "\n" + suffix
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
                metadata=dict(chunk.metadata),
            ),
        ))
    return parts


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
        def unique_object(pairs):
            # COSEC: A selection/status must not be replaced by a duplicate JSON key.
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("重复 JSON 字段")
                value[key] = item
            return value
        def reject_constant(value):
            raise ValueError("非有限数值不是合法 JSON")
        parsed = json.loads(stripped, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError):
        return None, "JSON 解析失败或包含重复字段/非有限数值"
    if not isinstance(parsed, dict):
        return None, "LLM 返回顶层必须是 JSON object"
    return parsed, None


def _unclosed_json_record(line):
    """Detect an unfinished container/string without repairing or decoding it."""
    if not line.lstrip().startswith('{'):
        return False
    stack, quoted, escaped = [], False, False
    for char in line:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '{[':
            stack.append(char)
        elif char in '}]':
            if not stack or stack.pop() != ('{' if char == '}' else '['):
                return False
    return quoted or bool(stack)


def _has_collection_envelope(text: str, collection_name: str) -> bool:
    """Recognize root envelopes even when their contents fail strict decoding.

    This is only a rejection boundary: recognizing a key never authorizes its
    value. In particular, malformed wrappers must not expose nested JSONL rows
    through the independent-line recovery path.
    """
    stack, quoted, escaped, token = [], False, False, []
    for index, char in enumerate(text):
        if quoted:
            token.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
                cursor = index + 1
                while cursor < len(text) and text[cursor] in ' \t\r\n':
                    cursor += 1
                if stack == ['{'] and text[cursor:cursor + 1] == ':':
                    try:
                        if json.loads(''.join(token)) == collection_name:
                            return True
                    except ValueError:
                        pass
            continue
        if char == '"':
            quoted, token = True, ['"']
        elif char in '{[':
            if not stack and char == '[':
                return True
            stack.append(char)
        elif char in '}]' and stack:
            stack.pop()
    return False


def _jsonl_rows(response: Any, collection_name: str, *, normalizations=None, incomplete_lines=None, verdict_schema=None) -> tuple[list[dict[str, Any]], list[str]]:
    """Read independent complete lines; damaged lines never erase their neighbours."""
    rows, errors = [], []
    text = _response_text(response)
    if not text.strip():
        return [], ["LLM 返回为空"]
    def unique_object(pairs):
        # COSEC: Reject duplicate keys instead of silently replacing a verdict/value.
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("重复 JSON 字段")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("非有限数值不是合法 JSON")

    def finite_float(literal):
        value = float(literal)
        if value in (float('inf'), float('-inf')):
            raise ValueError("非有限数值不是合法 JSON")
        return value

    # COSEC: Only remove a complete, unambiguous transport envelope. Do not
    # repair JSON, extract nested objects, or discard arbitrary surrounding text.
    if normalizations is not None:
        stripped = text.strip()
        lines = stripped.split('\n')
        if (len(lines) >= 3 and lines[0].strip() in {'```', '```json', '```jsonl'}
                and lines[-1].strip() == '```'
                and not any(line.strip().startswith('```') for line in lines[1:-1])):
            text = '\n'.join(lines[1:-1])
            normalizations.append('complete_markdown_fence')
        # Some providers emit the empty-result marker after completed records.
        # It carries no facts. Accept exactly one final marker, never an empty
        # middle record or an incomplete/malformed preceding record.
        lines = [line for line in text.split('\n') if line.strip()]
        if collection_name == 'entities' and len(lines) > 1 and lines[-1].strip() == '{}':
            prefix = '\n'.join(lines[:-1])
            _, prefix_errors = _jsonl_rows(prefix, collection_name)
            if not prefix_errors:
                text = prefix
                normalizations.append('final_empty_marker')
        decoder = json.JSONDecoder(object_pairs_hook=unique_object,
            parse_constant=reject_constant, parse_float=finite_float)
        if _has_collection_envelope(text, collection_name):
            # Live verification alone accepts one exact, fully consumed wrapper.
            # Duplicate fields/nonfinite numbers fail before any row can escape;
            # the usual schema, ID and ambiguity checks still apply afterwards.
            try:
                envelope = decoder.decode(text)
            except (json.JSONDecodeError, ValueError):
                envelope = None
            bare_array = isinstance(envelope, list)
            if bare_array:
                # A bare verification array is transport syntax only when
                # the entire active verdict contract passes before unwrapping.
                # No partial-array salvage, inferred IDs or nested extraction.
                expected = (_expected_record_keys(verdict_schema, 'verdicts')
                            if verdict_schema is not None else None)
                payload = {'verdicts': envelope}
                if (collection_name != 'verdicts' or not envelope
                        or expected is None or not all(isinstance(row, dict) for row in envelope)
                        or validate_json_schema(payload, verdict_schema)):
                    return [], ['核验裸数组必须完整符合当前逐项 schema；不接受空数组或其他数组']
                ids = [row['item_id'] for row in envelope]
                if len(ids) != len(expected) or set(ids) != expected:
                    return [], ['核验裸数组必须逐项且仅一次覆盖当前全部 item_id']
                envelope = payload
            if (collection_name != 'verdicts' or not isinstance(envelope, dict)
                    or set(envelope) != {'verdicts'}
                    or not isinstance(envelope['verdicts'], list)
                    or not all(isinstance(row, dict) for row in envelope['verdicts'])):
                return [], [f'JSONL 包装必须是完整且仅含 verdicts 对象列表的核验封套；不接受 {collection_name} 的其他包装']
            text = '\n'.join(json.dumps(row, ensure_ascii=False, allow_nan=False)
                             for row in envelope['verdicts'])
            normalizations.append('complete_verdicts_array' if bare_array else 'complete_verdicts_envelope')
        # A provider may return pretty-printed or adjacent top-level objects
        # instead of JSONL. Normalize only a fully consumed object stream. No
        # prefix/suffix stripping, nested-object extraction, or JSON repair.
        _, strict_errors = _jsonl_rows(text, collection_name)
        if strict_errors:
            complete, cursor, wrapped = [], 0, False
            try:
                while cursor < len(text):
                    while cursor < len(text) and text[cursor] in ' \t\r\n':
                        cursor += 1
                    if cursor == len(text):
                        break
                    if text[cursor] == '[':
                        wrapped = True
                        break
                    if text[cursor] != '{':
                        raise ValueError('not an object stream')
                    record, cursor = decoder.raw_decode(text, cursor)
                    if collection_name in record:
                        wrapped = True
                        break
                    complete.append(record)
                if wrapped:
                    return [], [f'JSONL 必须是独立记录，不使用数组或 {collection_name} 包装']
                empty = [index for index, record in enumerate(complete) if not record]
                valid_empty = (not empty or collection_name == 'entities'
                               and empty == [len(complete) - 1])
                if complete and valid_empty:
                    trailing_empty = len(complete) > 1 and not complete[-1]
                    if trailing_empty:
                        complete.pop()
                    text = '\n'.join(json.dumps(record, ensure_ascii=False, allow_nan=False)
                                     for record in complete)
                    normalizations.append('complete_object_stream')
                    if trailing_empty:
                        normalizations.append('final_empty_marker')
            except (json.JSONDecodeError, ValueError):
                # Preserve the established strict per-line partial recovery;
                # the failed formatting repair never authorizes a whole result.
                pass

    all_lines = text.split('\n')
    last_line = max((i for i, line in enumerate(all_lines, 1) if line.strip()), default=0)
    for number, line in enumerate(all_lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=unique_object, parse_constant=reject_constant,
                             parse_float=finite_float)
        except json.JSONDecodeError:
            errors.append(f"JSONL 第 {number} 行不完整或不是合法 JSON")
            if incomplete_lines is not None and number == last_line and _unclosed_json_record(line):
                incomplete_lines.append(number)
            continue
        except ValueError:
            errors.append(f"JSONL 第 {number} 行含重复字段或非有限数值；每个记录须独立闭合并换行")
            continue
        if not isinstance(row, dict) or collection_name in row:
            errors.append(f"JSONL 第 {number} 行必须是单个记录，不使用数组或 {collection_name} 包装")
            continue
        if not row:
            if collection_name == "entities" and text.strip() == "{}":
                return [], []
            errors.append(f"JSONL 第 {number} 行的空对象不能与记录混用")
            continue
        rows.append(row)
    return rows, errors


def decode_jsonl_entities(response: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Strict decoder; the generation loop separately retains validated records."""
    rows, errors = _jsonl_rows(response, "entities")
    return (None, "; ".join(errors[:5])) if errors else ({"entities": rows}, None)


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


def _merge_collection_items(collection_name, existing, incoming):
    """Preserve distinct entity facts; later valid keyed decisions replace earlier ones."""
    def key(item):
        if collection_name == "verdicts":
            return item["item_id"]
        if "name" in item:  # Property completion returns one complete record per exact key.
            return item["type"], item["name"]
        return json.dumps(item, ensure_ascii=False, sort_keys=True)
    merged = {key(item): item for item in existing}
    merged.update((key(item), item) for item in incoming)
    return list(merged.values())


def _valid_recovered_items(items, *, collection_name, schema, validator=None, rejection_reasons=None):
    """Validate records independently, without relaxing field or evidence rules."""
    item_schema = schema["properties"][collection_name]["items"]
    valid = []
    for index, item in enumerate(items):
        errors = validate_json_schema(item, item_schema)
        # Verification's full validator requires all IDs; its per-row enum already
        # checks scope. Entity/property validators also check grounding/exact keys.
        if not errors and collection_name != "verdicts" and validator is not None:
            errors = validator({collection_name: [item]})
        if errors:
            if rejection_reasons is not None and len(rejection_reasons) < 5:
                rejection_reasons.append({'record': index, 'errors': list(errors[:3])})
            continue
        valid.append(item)
    return valid


def _ambiguous_keys(items, collection_name):
    """Duplicate keyed decisions in one response must not be cherry-picked."""
    keys = ([row["item_id"] for row in items if isinstance(row.get("item_id"), str)]
            if collection_name == "verdicts" else
            [(row["type"], row["name"]) for row in items
             if isinstance(row.get("type"), str) and isinstance(row.get("name"), str)])
    return {key for key in keys if keys.count(key) > 1}


def _expected_record_keys(schema, collection_name):
    if collection_name not in {'entities', 'verdicts'}:
        return None
    fields = schema['properties'][collection_name]['items']['properties']
    if collection_name == 'verdicts':
        return set(fields['item_id'].get('enum', []))
    if 'name' in fields and len(fields['type'].get('enum', [])) == 1:
        return {(fields['type']['enum'][0], name) for name in fields['name'].get('enum', [])}
    return None


def _record_key(row, collection_name):
    return row.get('item_id') if collection_name == 'verdicts' else (row.get('type'), row.get('name'))


def _remaining_request(messages, schema, collection_name, pending):
    """Narrow both wire schema and operands; never change source text or rules."""
    narrowed = deepcopy(schema)
    collection = narrowed['properties'][collection_name]
    fields = collection['items']['properties']
    payload = json.loads(messages[1]['content'])
    if collection_name == 'verdicts':
        fields['item_id']['enum'] = sorted(pending)
        collection.update(minItems=len(pending), maxItems=len(pending))
        if 'items' in payload['task']:
            payload['task']['items'] = [item for item in payload['task']['items'] if item['item_id'] in pending]
    else:
        fields['name']['enum'] = sorted(name for _, name in pending)
        if 'known_entities' in payload['task']:
            payload['task']['known_entities'] = [row for row in payload['task']['known_entities']
                if (row['type'], row['name']) in pending]
    if 'output_contract' in payload:
        payload['output_contract']['success_schema'] = deepcopy(collection['items'])
    return [deepcopy(messages[0]), {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}], narrowed


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
                "thinking": trace.get("thinking") or {},
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




@traced_generation
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
    logical_context: dict | None = None,
) -> Any:
    # 以下为安全注释COSEC：扩展适配器只能操作副本，不得改写本地准入契约或重试证据。
    schema, messages = deepcopy(schema), deepcopy(messages)
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
                **(logical_context or {}),
            },
        )
    return llm(messages)


@synchronous
async def generate_structured(
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
    validator=None,
    item_validator=None,
    split_on_overflow: bool = False,
    saved_entity_keys=(),
    recovery_budget=None,
) -> dict[str, Any] | None:
    """Bounded JSONL calls retain individually validated records across failures."""
    messages, schema = deepcopy(messages), deepcopy(schema)
    attempt_messages = messages
    attempt_schema = schema
    resolved_max_attempts = max(1, int(max_attempts))
    # Transport outages must not consume every chance to repair the first
    # returned output. Both categories remain bounded, with one shared document
    # call budget/deadline and an absolute per-task ceiling.
    max_total_attempts = 2 * resolved_max_attempts - 1
    transport_failures = output_failures = 0
    response_mode = "plain"
    collection_name = _array_collection_name(schema) if len(schema.get("properties", {})) == 1 else None
    expected_keys = _expected_record_keys(schema, collection_name)
    entity_delta = False
    incomplete_discovery = False
    recovered_items: list[dict[str, Any]] = []
    largest_invalid_response = 0
    record_table_flag = retry_empty_collection if record_table is None else bool(record_table)
    def partial_result():
        if recovered_items and attempts:
            attempts[-1]["recovered_items"] = len(recovered_items)
            attempts[-1]["recovery"] = "jsonl_valid_records"
            if collection_name == "verdicts":
                expected = schema["properties"]["verdicts"]["items"]["properties"]["item_id"].get("enum", [])
                attempts[-1]["missing_item_ids"] = sorted(set(expected) - {row["item_id"] for row in recovered_items})
        return {collection_name: recovered_items} if collection_name and recovered_items else None

    for attempt_number in range(1, max_total_attempts + 1):
        if document_expired(llm):
            attempts.append({'attempt': attempt_number, 'type': 'timeout', 'pass': pass_name,
                'chunk': chunk_id, 'message': 'document deadline exhausted',
                'retryable': False, 'terminal': True})
            return partial_result()
        logical_context = recovery_budget.claim_trace() if recovery_budget is not None else None
        if recovery_budget is not None and logical_context is None:
            attempts.append({'attempt': attempt_number, 'type': 'recovery_budget', 'pass': pass_name,
                'chunk': chunk_id, 'message': 'logical task recovery budget exhausted',
                'retryable': False, 'terminal': True, **recovery_budget.trace()})
            return partial_result()
        started = time.monotonic()
        error_type = ""
        message = ""
        response: Any = None
        retryable = True
        rejected_records = 0
        rejection_reasons = []
        response_normalizations = []
        incomplete_lines = []
        ignored_saved_records = 0
        duplicate_entity_records = 0
        repetitive_entity_output = False
        discovery_progress = False
        adaptive_split = False
        retry_after = 0.0
        logger.info(
            "[步骤][LLM] 输入: pass=%s chunk=%s attempt=%d/%d mode=%s 消息=%d prompt=%d字符",
            pass_name,
            chunk_id,
            attempt_number,
            max_total_attempts,
            response_mode,
            len(attempt_messages),
            sum(len(str(item.get("content") or "")) for item in attempt_messages),
        )
        if not fits(attempt_messages, llm, reserve=0):
            attempts.append({"attempt": attempt_number, "type": "input_budget", "pass": pass_name,
                "chunk": chunk_id, "message": f"request {message_bytes(attempt_messages)} bytes exceeds {input_limit(llm)} budget",
                "terminal": True})
            return partial_result()
        try:
            def request_once():
                if document_expired(llm):
                    raise TimeoutError('document deadline exhausted before generation')
                from external_services.llm.runtime import ScheduledClient
                if use_scheduler and not isinstance(getattr(llm, 'client', None), ScheduledClient):
                    from .request_budget import document_deadline
                    with get_llm_scheduler().request_slot(deadline=document_deadline(llm)):
                        if document_expired(llm):
                            raise TimeoutError('document deadline exhausted during scheduling')
                        return _invoke_generation(
                            llm, attempt_schema, attempt_messages,
                            pass_name=pass_name, chunk_id=chunk_id,
                            attempt_number=attempt_number, response_mode=response_mode,
                            record_table=record_table_flag,
                            logical_context=logical_context,
                        )
                return _invoke_generation(
                    llm,
                    attempt_schema,
                    attempt_messages,
                    pass_name=pass_name,
                    chunk_id=chunk_id,
                    attempt_number=attempt_number,
                    response_mode=response_mode,
                    record_table=record_table_flag,
                    logical_context=logical_context,
                )
            response = await current_runtime().call(request_once, stage=pass_name, llm=llm)
            # A provider may ignore its timeout. A late result is not a successful attempt.
            if document_expired(llm):
                raise TimeoutError('document deadline exhausted during generation')
            pre_parse_error = _pre_parse_response_error(response)
            if collection_name in {"entities", "verdicts"}:
                rows, line_errors = _jsonl_rows(response, collection_name,
                    normalizations=response_normalizations, incomplete_lines=incomplete_lines,
                    verdict_schema=attempt_schema if collection_name == 'verdicts' else None)
                valid_rows = _valid_recovered_items(rows, collection_name=collection_name,
                    schema=attempt_schema, validator=item_validator or validator,
                    rejection_reasons=rejection_reasons)
                # Repeated accepted keys are scheduling noise, not malformed
                # records. Never overwrite their verified facts with the repeat.
                if collection_name == 'entities' and 'mention' in schema['properties'][collection_name]['items'].get('properties', {}):
                    keys = [(row['type'], row['mention']) for row in valid_rows]
                    duplicate_entity_records = len(keys) - len(set(keys))
                    # A saturated response repeating mostly the same exact keys
                    # is decoder/task repetition, not evidence that a smaller
                    # source alone will complete the unknown discovery scope.
                    repetitive_entity_output = (len(keys) >= 12
                        and duplicate_entity_records * 2 >= len(keys))
                    saved = set(saved_entity_keys)
                    previous_keys = {(row['type'], row['mention']) for row in recovered_items} | saved
                    discovery_progress = any((row['type'], row['mention']) not in previous_keys for row in valid_rows)
                    ignored_saved_records = sum((row['type'], row['mention']) in saved for row in valid_rows)
                    valid_rows = [row for row in valid_rows if (row['type'], row['mention']) not in saved]
                keyed = collection_name == "verdicts" or "name" in schema["properties"][collection_name]["items"].get("properties", {})
                if keyed:
                    ambiguous = _ambiguous_keys(rows, collection_name)
                    def row_key(row):
                        return row.get("item_id") if collection_name == "verdicts" else (row.get("type"), row.get("name"))
                    valid_rows = [row for row in valid_rows if row_key(row) not in ambiguous]
                    # Accepted keys from earlier attempts are outside this retry's
                    # scope. Out-of-scope duplicate rows cannot invalidate them.
                    active = _expected_record_keys(attempt_schema, collection_name)
                    recovered_items = [row for row in recovered_items
                        if row_key(row) not in ambiguous or (active is not None and row_key(row) not in active)]
                rejected_records = len(line_errors) + len(rows) - len(valid_rows) - ignored_saved_records
                recovered_items = _merge_collection_items(collection_name, recovered_items, valid_rows)
                parsed = {collection_name: valid_rows}
                parse_error = "; ".join(line_errors[:5]) if line_errors else None
            else:
                parsed, parse_error = decode_json_object(response)

            if pre_parse_error is not None:
                error_type, message = pre_parse_error
                if error_type == 'incomplete' and expected_keys is None and collection_name == 'entities':
                    incomplete_discovery = True
            elif parse_error is not None:
                if collection_name == 'entities' and expected_keys is None and incomplete_lines:
                    # A provider's stop flag cannot override an unfinished body.
                    # Repairing its final record does not prove source exhaustion.
                    error_type, message = 'incomplete', parse_error
                    incomplete_discovery = True
                else:
                    error_type, message = "json", parse_error
            elif rejected_records:
                details = '; '.join(f"record[{r['record']}]: {'; '.join(r['errors'])}" for r in rejection_reasons)
                error_type, message = 'schema', details or '重复的精确键产生多个决策；已拒收，其他有效行保留'
            else:
                schema_errors = validate_json_schema(parsed, attempt_schema)
                aggregate = ({collection_name: recovered_items} if collection_name in {'entities', 'verdicts'} else parsed)
                if not schema_errors:
                    schema_errors = validate_json_schema(aggregate, schema)
                if not schema_errors and expected_keys is not None:
                    keys = [_record_key(row, collection_name) for row in aggregate[collection_name]]
                    if set(keys) != expected_keys or len(keys) != len(expected_keys):
                        schema_errors = ['remaining records must cover every expected key exactly once']
                if not schema_errors and validator is not None:
                    schema_errors = validator(aggregate)
                if schema_errors:
                    error_type, message = "schema", "; ".join(schema_errors[:5])
                elif incomplete_discovery and not discovery_progress:
                    error_type, message = 'schema', '前序实体发现被截断；仅重复已保存键不能证明来源范围已完成'
                elif (collection_name and not parsed.get(collection_name)
                      and not (entity_delta and recovered_items and not incomplete_discovery)
                      and (recovered_items or largest_invalid_response >= SUBSTANTIVE_INVALID_RESPONSE_CHARACTERS)):
                    error_type, message = "schema", "前序响应包含有效记录或大体量无效输出，重试不得用空集合覆盖"
                elif (retry_empty_collection and collection_name == "entities"
                      and not parsed.get(collection_name) and not entity_delta
                      and output_failures + 1 < resolved_max_attempts):
                    error_type, message = "empty_collection", "实体阶段返回空集合，按提示词要求重试补全"
                else:
                    if collection_name in {"entities", "verdicts"}:
                        # Retain earlier grounded entities even when a complete retry omits them.
                        parsed = aggregate
                    _record_call(call_metrics, attempt=attempt_number, pass_name=pass_name,
                        chunk_id=chunk_id, status="succeeded", elapsed_seconds=time.monotonic() - started,
                        messages=attempt_messages, response=response, response_mode=response_mode)
                    if call_metrics:
                        call_metrics[-1].update(response_normalizations=response_normalizations,
                                                ignored_saved_records=ignored_saved_records,
                                                **(logical_context or {}))
                    return parsed
            largest_invalid_response = max(largest_invalid_response, len(_response_text(response)))
        except Exception as exc:  # noqa: BLE001 - recorded as typed run evidence
            from algorithm.token_budget import InputTokenLimitError
            error_type = ('input_budget' if isinstance(exc, InputTokenLimitError) else
                          'call_budget' if isinstance(exc, CallBudgetExceeded) else "timeout" if _is_timeout(exc) else "request")
            # 以下为安全注释COSEC：供应商异常可能回显鉴权头、URL 或请求正文。
            # 仅保留控制重试所需的错误类别，不将原始异常写入日志或结果。
            raw_message = str(exc).lower()
            overflow = any(text in raw_message for text in
                ('context length', 'context_length', 'maximum context', 'token limit', 'too many tokens'))
            message = ('request input budget exceeded' if error_type == 'input_budget' else
                       'document model call budget exhausted' if error_type == 'call_budget' else
                       'context length exceeded' if overflow else
                       'document deadline exhausted' if document_expired(llm) else
                       'LLM request timed out' if error_type == 'timeout' else 'LLM provider request failed')
            retryable = error_type != 'call_budget' and _is_retryable_exception(exc) and not document_expired(llm)
            # COSEC: Read only a bounded numeric retry hint, never persist headers.
            try:
                retry_after = min(60.0, max(0.0, float(getattr(getattr(exc, 'response', None), 'headers', {}).get('Retry-After', 0))))
            except (ValueError, TypeError, AttributeError):
                retry_after = 0.0
            # A gateway that failed after a long generation may fail repeatedly
            # on the same large output. Narrow the source/schema/items once the
            # bounded caller permits it. Rate-limit delays retain normal retry.
            try:
                status_code = int(getattr(exc, 'status_code', None) or getattr(getattr(exc, 'response', None), 'status_code', 0))
            except (ValueError, TypeError):
                status_code = 0
            adaptive_split = (retryable and error_type == 'request' and status_code in {502, 504}
                              and retry_after == 0 and time.monotonic() - started >= 60)

        if error_type in {'request', 'timeout'}:
            transport_failures += 1
            retry_limit_reached = transport_failures >= resolved_max_attempts
        elif error_type in {'json', 'schema', 'incomplete', 'empty', 'empty_collection'}:
            output_failures += 1
            retry_limit_reached = output_failures >= resolved_max_attempts
        else:
            retry_limit_reached = True
        message = message.encode("utf-8")[:1024].decode("utf-8", errors="ignore")
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
        stream_reason = (response_metadata.get('incomplete_details') or {}).get('reason')
        if stream_reason in {'repetitive_entity_output', 'consecutive_empty_evidence', 'repetitive_malformed_object'}:
            repetitive_entity_output = True
        attempts.append(
            {
                "attempt": attempt_number,
                "transport_failures": transport_failures,
                "output_failures": output_failures,
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
                "rejected_records": rejected_records,
                "rejection_reasons": rejection_reasons,
                "response_normalizations": response_normalizations,
                "ignored_saved_records": ignored_saved_records,
                "duplicate_entity_records": duplicate_entity_records,
                "repetitive_entity_output": repetitive_entity_output,
                "adaptive_split": adaptive_split,
                "unfinished_entity_tail": bool(collection_name == 'entities' and expected_keys is None and incomplete_lines),
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "retryable": retryable,
                "terminal": False,
                **(logical_context or {}),
            }
        )
        _record_call(
            call_metrics,
            attempt=attempt_number,
            pass_name=pass_name,
            chunk_id=chunk_id,
            status="partial" if recovered_items else "failed",
            elapsed_seconds=time.monotonic() - started,
            messages=attempt_messages,
            response=response,
            error_type=error_type,
            response_mode=response_mode,
        )
        pending_keys = (expected_keys - {_record_key(row, collection_name) for row in recovered_items}
                        if expected_keys is not None else None)
        if (not retryable or retry_limit_reached or attempt_number >= max_total_attempts
                or pending_keys == set()
                or (split_on_overflow and split_request_failure(attempts))):
            attempts[-1]["terminal"] = True
            break
        if error_type in {"json", "schema", "empty_collection", "incomplete"}:
            retry_messages = messages
            if pending_keys is not None:
                retry_messages, attempt_schema = _remaining_request(messages, schema, collection_name, pending_keys)
            feedback = (
                ("上一次输出校验失败，请重新输出完整 JSON 对象。\n" if collection_name not in {'entities', 'verdicts'} else
                 "上一次部分输出未采用，请只处理本轮剩余任务。\n") +
                f"错误类型：{error_type}；错误：{message}\n"
                f"程序已保留 {len(recovered_items)} 条本轮格式及局部契约校验通过的记录；损坏行未采用。已保存不等于事实已获语义证明，也不证明当前来源范围已抽全。\n"
            )
            feedback += ("只输出一个符合 success_schema 的 JSON 对象，不输出 Markdown 或解释。" if collection_name not in {'entities', 'verdicts'} else
                         "只输出 JSONL，每行一个符合 success_schema 的记录，不输出数组、包装或解释。")
            if collection_name is None:
                feedback = ("上一次输出校验失败，请返回一个完整 JSON 对象，严格遵守 success_schema。"
                            f"错误类型：{error_type}；错误：{message}。不要集合包装、Markdown 或额外字段。")
            if collection_name == "verdicts":
                feedback += "逐项且仅一次覆盖本轮 task.items 中剩余的 item_id，按本轮 success_schema 返回；不能用实体存在代替属性核验。"
            elif collection_name == "entities":
                feedback += "已保存记录由程序保留，本轮输出不覆盖它们。"
                if 'mention' in schema['properties'][collection_name]['items'].get('properties', {}) and recovered_items:
                    accepted_keys = list(dict.fromkeys((row['type'], row['mention']) for row in recovered_items))
                    entity_delta = True
                    feedback += ('已保存的 type/mention 如下；只补充遗漏候选或修复不合格行，不重复这些已保存实体，'
                        '不通过改写 mention 重复同一实体。完成后立即停止输出：'
                        + json.dumps(accepted_keys, ensure_ascii=False))
                    if not incomplete_discovery:
                        feedback += '没有新增候选时返回单独一行 {}，表示续补结束，不删除已保存记录。'
                    else:
                        feedback += '先前输出被供应商标记截断，仅返回 {} 不能证明该来源范围已完成。'
            attempt_messages = [retry_messages[0], {"role": "user", "content":
                user_with_retry_feedback(retry_messages[1]["content"], feedback)}]
        response_mode = next_response_mode
        if retry_base_seconds > 0 or retry_after > 0:
            from .request_budget import document_deadline
            deadline = document_deadline(llm)
            delay = retry_base_seconds * (2 ** (attempt_number - 1))
            if error_type in {'request', 'timeout'}:
                import random
                delay = max(retry_after, max(0.5, delay) * random.uniform(0.8, 1.2))
            if deadline is not None:
                delay = min(delay, max(0, deadline - time.monotonic()))
            await current_runtime().delay(delay)
    return partial_result()



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
) -> dict[str, str]:
    """把语义比对不一致的实体/属性交给 LLM 核验，返回 item_id -> yes/no/uncertain。

    核验失败（网络/解析问题）返回空 dict，调用方应保留原结果而不是丢弃。
    """
    if not items:
        return {}
    messages = build_verification_messages(chunk=chunk, items=items)
    try:
        parsed = generate_structured(
            llm,
            messages,
            verification_schema(items),
            validator=lambda payload: validate_verification_items(payload, items),
            use_scheduler=use_scheduler,
            attempts=attempts if attempts is not None else [],
            pass_name=pass_name,
            chunk_id=chunk_id or chunk.chunk_id,
            max_attempts=max(1, int(max_attempts)),
            retry_base_seconds=retry_base_seconds,
            call_metrics=call_metrics,
            record_table=record_table,
        )
    except Exception:  # noqa: BLE001 - 核验失败时保留原结果，不丢弃
        return {}
    verdicts: dict[str, str] = {}
    if isinstance(parsed, dict):
        for row in parsed.get("verdicts") or []:
            if not isinstance(row, dict):
                continue
            item_id = str(row.get("item_id") or "")
            supported = str(row.get("supported") or "").casefold()
            if item_id and supported in {"yes", "no", "uncertain"}:
                verdicts[item_id] = supported
    return verdicts


def _normalize_span_entities(
    parsed: dict[str, Any],
    *,
    chunk: ExtractionChunk,
    schema: OntologySchema,
    target_types: Iterable[str] | None,
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Turn strict span candidates into the legacy graph entity contract.

    The model supplies verbatim mentions and optional locally supported properties.  A name may be a
    faithful abstraction of the source (e.g. an inferred rule name), but every
    accepted fact must still carry an ``evidence.quote`` locatable in the chunk,
    with source positions computed by the evidence binder, never by the model.
    """
    raw_entities = parsed.get("entities", []) if isinstance(parsed, dict) else []
    if not isinstance(raw_entities, list):
        _append_warning(warnings, "INVALID_ENTITIES", "entities 必须是数组", "entities")
        return []
    requested = [schema.resolve_class(item) for item in (target_types or [])]
    requested = [item for item in requested if item is not None]
    accepted: list[dict[str, Any]] = []
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
        if not name_in_text and quote_in_text:
            _append_warning(warnings, "ENTITY_NAME_UNGROUNDED",
                "mention 无法逐字定位，已保留归纳型实体名并以 evidence.quote 作为证据",
                path, name)
        elif not name_in_text and not quote_in_text:
            if any(ChunkContextManager([source]).find_span(source.chunk_id, quote)
                   for source in source_parts(chunk)):
                quote_in_text = True
            else:
                _append_warning(warnings, "ENTITY_UNGROUNDED",
                    "mention 与 evidence.quote 均无法定位，拒收无来源实体", path, name)
                continue
        # Offsets are exclusively computed by the evidence binder. Do not accept
        # model offsets or manufacture 0:0 spans when no source can be located.
        locator = quote if quote_in_text else name if name_in_text else ""
        if locator and len(list(re.finditer(f"(?={re.escape(locator)})", text))) > 1:
            _append_warning(warnings, "ENTITY_EVIDENCE_AMBIGUOUS",
                "证据在当前切片重复出现，绑定首个匹配位置，无法唯一确定来源位置", path, locator)

        accepted.append({
            "type": ontology_class.identifier,
            "name": name,
            "properties": dict(raw.get("properties") or {}),
            "_properties_scanned": "properties" in raw,
            "evidence": {"quote": quote, **({"property_quotes": evidence["property_quotes"]} if "property_quotes" in evidence else {})},
        })
    return accepted


def bind_entity_evidence(
    entities: list[dict[str, Any]],
    manager: ChunkContextManager,
    *,
    chunk_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    bound = bind_evidence(
        {"entities": entities, "relations": []},
        manager,
        chunk_ids=chunk_ids,
    )
    return bound.get("entities", [])


@synchronous
async def entity_pass(
    plan: ExtractionPlan,
    schema: OntologySchema,
    manager: ChunkContextManager,
    request: ExtractionRequest,
    llm: Any,
    *,
    route: DocumentRoute,
    assembly_planner: Any | None = None,
    max_workers: int,
    use_scheduler: bool,
    attempts: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    max_attempts: int = 3,
    retry_base_seconds: float = 0.05,
    call_metrics: list[dict[str, Any]] | None = None,
    task: str = "entity",
    repair_issues: list[dict[str, Any]] | None = None,
    recovery_chunk_ids: Iterable[str] = (),
    local_previous: int = 1,
    local_next: int = 1,
    same_section: bool = True,
) -> list[tuple[str, list[dict[str, Any]]]]:
    chunks = list(request.chunks)
    recovery_chunk_ids = frozenset(recovery_chunk_ids)

    async def run(chunk: ExtractionChunk, *, type_ids=None, saved_keys=(), audit_types=True) -> tuple[
        str,
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        local_attempts: list[dict[str, Any]] = []
        local_warnings: list[dict[str, Any]] = []
        local_call_metrics: list[dict[str, Any]] = []
        pass_name = ('recover_entity' if task == 'repair' and chunk.chunk_id in recovery_chunk_ids else
                     'repair_entity' if task == 'repair' else 'entity')
        from .property_grounding import ground_properties, admit_entity_names

        def context_sources(part):
            sources = source_parts(part)
            original = manager.get_chunk(part.chunk_id)
            start = int(part.metadata.get('_v31_window_start', 0))
            end = start + len(part.text)
            from dataclasses import replace
            ranges = [] if part.metadata.get('_v31_table_rows') else ((max(0, start - 200), start), (end, min(len(original.text), end + 200)))
            for left, right in ranges:
                if left < right:
                    sources.append(replace(original, text=original.text[left:right], metadata={**original.metadata,
                        '_v31_window_start': left, '_v31_context_only': True}))
            neighbours = (manager.local_context(part.chunk_id, previous=local_previous,
                          next=local_next, same_section=same_section) if len(manager) > 1 else [])
            for neighbour in neighbours:
                if neighbour.chunk_id == part.chunk_id:
                    continue
                previous = manager.index_of(neighbour.chunk_id) < manager.index_of(part.chunk_id)
                left = max(0, len(neighbour.text) - 200) if previous else 0
                sources.append(replace(neighbour, text=neighbour.text[left:left + 200], metadata={**neighbour.metadata,
                    '_v31_window_start': left, '_v31_context_only': True}))
            return sources

        async def finish(parsed, part):
            warning_start = len(local_warnings)
            entities = _normalize_span_entities(parsed, chunk=part, schema=schema,
                target_types=request.target_types, warnings=local_warnings)
            for warning in local_warnings[warning_start:]:
                if warning.get('code') == 'ENTITY_UNGROUNDED':
                    local_attempts.append({'attempt': 1, 'type': 'candidate_rejected',
                        'pass': 'entity_admission', 'chunk': chunk.chunk_id,
                        'message': 'candidate has no locatable evidence in the primary source',
                        'retryable': False, 'terminal': False})
            sources = context_sources(part)
            entities = await invoke(admit_entity_names, entities, sources, llm, attempts=local_attempts,
                warnings=local_warnings, use_scheduler=use_scheduler, max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds, call_metrics=local_call_metrics, plan=plan,
                route=route, assembly_planner=assembly_planner, max_workers=max_workers)
            for entity in entities:
                start = int(part.metadata.get('_v31_window_start', 0))
                entity['_scanned_range'] = (start, start + len(part.text))
                entity['_attempted_properties'] = list(entity.get('properties', {}))
            return await invoke(ground_properties, entities, sources, llm, attempts=local_attempts,
                warnings=local_warnings, use_scheduler=use_scheduler, max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds, call_metrics=local_call_metrics, plan=plan,
                route=route, assembly_planner=assembly_planner, max_workers=max_workers)

        async def scan(part, types, depth=0, accepted_keys=(), recovery_budget=None):
            if recovery_budget is None:
                recovery_budget = RecoveryBudget(max_attempts,
                    # Recovery windows overlap and punctuation can shorten them.
                    # Count actual minimum windows before allocating the shared
                    # tree allowance; dividing source length silently undercounts.
                    branches=max(1, len(types)) * len(windows(part, 128)))
            if document_expired(llm):
                local_attempts.append({'attempt': 1, 'type': 'timeout',
                    'pass': pass_name,
                    'chunk': chunk.chunk_id, 'message': 'document deadline exhausted',
                    'retryable': False, 'terminal': True})
                return []
            output_schema = output_schema_for_types(plan, "entity_candidates", types)
            messages = build_chat_messages(assembly_planner=assembly_planner,
                task=task, plan=plan, route=route, chunk=part, output_schema=output_schema,
                source_chunks=context_sources(part), repair_issues=repair_issues, accepted_keys=accepted_keys)
            before = len(local_attempts)
            # Schema partitions are scheduling only: visit every allowed type.
            # Shrink before calling, including ontology, skills and metadata.
            # Reserve feedback space while a request can still be partitioned.
            # At the minimum source/type scope, the actual wire budget decides;
            # an optional reserve must not reject an otherwise legal request.
            too_big = not fits(messages, llm, reserve=0 if len(types) == 1 and len(part.text) <= 256 else 2048)
            parsed = None
            if not too_big:
                parsed = await invoke(generate_structured, llm, messages, output_schema,
                    use_scheduler=use_scheduler, attempts=local_attempts,
                    pass_name=pass_name, chunk_id=chunk.chunk_id,
                    max_attempts=max_attempts, retry_base_seconds=retry_base_seconds,
                    call_metrics=local_call_metrics,
                    split_on_overflow=depth < 12 and (len(types) > 1 or len(part.text) > 256),
                    # Source entailment is a candidate admission decision, not
                    # a serialization failure requiring regeneration of peers.
                    validator=lambda payload: validate_candidate_properties(payload, plan),
                    saved_entity_keys=accepted_keys, recovery_budget=recovery_budget)
            if parsed is None and document_expired(llm):
                return []
            output_failure = bool(local_attempts[before:] and local_attempts[-1].get('terminal')
                                  and local_attempts[-1].get('type') in {'json', 'schema'} and depth < 2)
            if too_big or split_request_failure(local_attempts[before:]) or output_failure:
                if depth < 12 and (len(types) > 1 or (len(part.text) > 256 and not part.metadata.get('_v31_atomic_table'))):
                    parent_failures = local_attempts[before:]
                    retained = await finish(parsed, part) if parsed is not None else []
                    for row in retained:
                        row['_properties_scanned'] = False  # The interrupted scan cannot establish property completeness.
                    saved = set(accepted_keys) | {(row['type'], row['name']) for row in retained}
                    recover_split_attempts(parent_failures)
                    unfinished_tail = any(a.get('unfinished_entity_tail') for a in parent_failures)
                    repetition = any(a.get('repetitive_entity_output') for a in parent_failures)
                    if len(types) > 1 and (too_big or parsed is not None) and (repetition or not unfinished_tail):
                        mid = len(types) // 2
                        first = await scan(part, types[:mid], depth + 1, saved, recovery_budget)
                        saved.update((row['type'], row['name']) for row in first)
                        return retained + first + await scan(part, types[mid:], depth + 1, saved, recovery_budget)
                    if len(part.text) > 256 and not part.metadata.get('_v31_atomic_table'):
                        found = list(retained)
                        for sub in windows(part, max(128, len(part.text) // 2)):
                            additions = await scan(sub, types, depth + 1, saved, recovery_budget)
                            found.extend(additions)
                            # Different source windows may contain another
                            # occurrence/new properties of the same key. Only
                            # exclude the retained parent prefix, whose missing
                            # properties are explicitly rescanned afterwards.
                        return found
                    mid = len(types) // 2
                    return retained + await scan(part, types[:mid], depth + 1, saved, recovery_budget) + await scan(part, types[mid:], depth + 1, saved, recovery_budget)
                if too_big:
                    local_attempts.append({"attempt": 0, "type": "input_budget",
                        "pass": pass_name, "chunk": chunk.chunk_id,
                        "message": "minimum source/schema window exceeds request budget", "terminal": True})
                return await finish(parsed, part) if parsed is not None else []
            if parsed is None:
                return []
            return await finish(parsed, part)

        grounded_entities = []
        for part in windows(chunk, window_chars(llm)):
            grounded_entities.extend(await scan(part, sorted(type_ids or plan.relevant_types(part.text)),
                                                accepted_keys=saved_keys))
        allowed = sorted(plan.relevant_types(chunk.text))
        represented = {row['type'] for row in grounded_entities}
        # A long multi-type source can receive one valid type and stop early
        # instead of visiting the other allowed type scopes. Check the
        # remaining type scopes once; no required count, no forced candidates.
        # Each child owns its diagnostics, avoiding cross-task recovery markers.
        type_audit_enabled = config_getenv('KG_EXTRACT_V31_TYPE_COMPLETION_AUDIT', 'false').strip().casefold() in {'1', 'true', 'yes'}
        if (type_audit_enabled and audit_types and len(chunk.text) >= 512 and len(allowed) > 1
                and len(represented) == 1 and not any(a.get('terminal') for a in local_attempts)
                and not document_expired(llm)):
            saved = {(row['type'], row['name']) for row in grounded_entities}
            async def remaining(type_id):
                return await run(chunk, type_ids=[type_id], saved_keys=saved, audit_types=False)
            checks = await ordered_tasks(remaining, [t for t in allowed if t not in represented],
                                         limit=max(1, max_workers))
            for _, additions, child_attempts, child_warnings, child_calls in checks:
                grounded_entities.extend(additions)
                local_attempts.extend(child_attempts)
                local_warnings.extend(child_warnings)
                local_call_metrics.extend(child_calls)
        return (
            chunk.chunk_id,
            grounded_entities,
            local_attempts,
            local_warnings,
            local_call_metrics,
        )

    pending_chunks = fair_source_windows(chunks, window_chars(llm))
    outputs = await ordered_tasks(run, pending_chunks, limit=max(1, max_workers) * 2)
    results: dict[str, list[dict[str, Any]]] = {chunk.chunk_id: [] for chunk in chunks}
    for chunk_id, entities, local_attempts, local_warnings, local_calls in outputs:
        attempts.extend(local_attempts)
        warnings.extend(local_warnings)
        if call_metrics is not None:
            for call in local_calls:
                _append_call(call_metrics, call)
        results[chunk_id].extend(entities)
    return list(results.items())


def _append_call(call_metrics, call):
    # Full messages are debugging data, not extraction output. Bound retention
    # across the request while retaining every call's timing/status/usage.
    limit = max(0, int(config_getenv("KG_EXTRACT_V31_TRACE_CALLS", "32")))
    if len(call_metrics) >= limit:
        call.pop("messages", None)
        call.pop("response_text", None)
        call["messages_omitted"] = True
        call["response_omitted"] = True
    call_metrics.append(call)


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
    _append_call(call_metrics,
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
            "input_bytes": message_bytes(messages),
            "output_characters": len(text),
            "usage": usage,
            "usage_unavailable": response_metadata.get("usage_unavailable"),
            "token_usage": usage,
            "thinking": deepcopy(response_metadata.get("thinking") or {}),
            "messages": [dict(message) for message in messages],
            **({"response_text": text} if response is not None else {}),
        }
    )


__all__ = [
    "bind_entity_evidence",
    "decode_json_object",
    "entity_pass",
    "generate_structured",
]
