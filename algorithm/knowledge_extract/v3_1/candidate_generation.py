"""Schema-bounded entity candidate generation.

This module owns bounded source windows, JSONL retries, local properties and
source binding. Exact type/name grouping belongs to property_enrichment.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from copy import deepcopy
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from typing import Any

from .chunk_context import ChunkContextManager, ExtractionChunk, ExtractionRequest
from .evidence import Evidence, bind_evidence, locate_quote
from .graph_utils import append_warning as _append_warning, canonical_name
from .ontology_compiler import ExtractionPlan
from .output_contracts import output_schema_for_types, validate_candidate_properties
from .request_budget import fits, input_limit, message_bytes, window_chars, windows, iter_windows, split_request_failure, recover_split_attempts, document_expired
from .ontology_schema import OntologySchema
from ._logging import get_step_logger
from .prompt_router import ChatMessages, DocumentRoute, _looks_like_record_table, build_chat_messages
from .prompt_templates import user_with_retry_feedback


logger = get_step_logger(__name__)
from .semantic_evidence import build_verification_messages, semantic_support
from .output_contracts import verification_schema, validate_verification_items
from .scheduler import get_llm_scheduler
from .structured_output import validate_json_schema

GLOBAL_RELATION_CATALOG_THRESHOLD = 80
MAX_LOCAL_RELATION_CATALOG = 160
# 批大小受输出上限约束：每条 pair 决策（pair_id+label+evidence.quote）约 60~90
# token，默认 max_tokens=16384 最多容纳约 150~250 条。超限会导致响应 JSON 截断、
# generate_structured 解析失败后整批丢弃（批越大丢得越多），故取 150。
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
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败: {exc.msg}"
    if not isinstance(parsed, dict):
        return None, "LLM 返回顶层必须是 JSON object"
    return parsed, None


def decode_jsonl_entities(response: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Decode complete JSONL only; never silently keep a truncated prefix."""
    text = _response_text(response).strip()
    if not text:
        return None, "LLM 返回为空；无实体时应返回单独一行 {}"
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            return None, f"JSONL 第 {number} 行解析失败: {exc.msg}"
        if not isinstance(row, dict) or "entities" in row:
            return None, f"JSONL 第 {number} 行必须是单个实体对象，不使用数组或 entities 包装"
        rows.append(row)
    if rows == [{}]:
        return {"entities": []}, None
    if any(not row for row in rows):
        return None, "空结果 {} 只能单独出现，不能与实体行混用"
    return {"entities": rows}, None


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
    validator=None,
    split_on_overflow: bool = False,
) -> dict[str, Any] | None:
    """Call one required pass with bounded retry and conservative prefix recovery."""
    messages, schema = deepcopy(messages), deepcopy(schema)
    attempt_messages = messages
    resolved_max_attempts = max(1, int(max_attempts))
    response_mode = "plain"
    collection_name = _array_collection_name(schema)
    recovered_items: list[dict[str, Any]] = []
    largest_invalid_response = 0
    record_table_flag = retry_empty_collection if record_table is None else bool(record_table)
    for attempt_number in range(1, resolved_max_attempts + 1):
        if document_expired(llm):
            attempts.append({'attempt': attempt_number, 'type': 'timeout', 'pass': pass_name,
                'chunk': chunk_id, 'message': 'document deadline exhausted',
                'retryable': False, 'terminal': True})
            return None
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
        if not fits(attempt_messages, llm, reserve=0):
            attempts.append({"attempt": attempt_number, "type": "input_budget", "pass": pass_name,
                "chunk": chunk_id, "message": f"request {message_bytes(attempt_messages)} bytes exceeds {input_limit(llm)} budget",
                "terminal": True})
            return None
        try:
            if use_scheduler:
                from .request_budget import document_deadline
                with get_llm_scheduler().request_slot(deadline=document_deadline(llm)):
                    if document_expired(llm):
                        raise TimeoutError('document deadline exhausted during scheduling')
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
            # A provider may ignore its timeout. A late result is not a successful attempt.
            if document_expired(llm):
                raise TimeoutError('document deadline exhausted during generation')
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
                parsed, parse_error = (decode_jsonl_entities(response) if collection_name == "entities"
                                       else decode_json_object(response))
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
                    if not schema_errors and validator is not None:
                        schema_errors = validator(parsed)
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
            # 以下为安全注释COSEC：供应商异常可能回显鉴权头、URL 或请求正文。
            # 仅保留控制重试所需的错误类别，不将原始异常写入日志或结果。
            raw_message = str(exc).lower()
            overflow = any(text in raw_message for text in
                ('context length', 'context_length', 'maximum context', 'token limit', 'too many tokens'))
            message = ('context length exceeded' if overflow else
                       'document deadline exhausted' if document_expired(llm) else
                       'LLM request timed out' if error_type == 'timeout' else 'LLM provider request failed')
            retryable = _is_retryable_exception(exc) and not document_expired(llm)

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
        if not retryable or attempt_number >= resolved_max_attempts or (split_on_overflow and split_request_failure(attempts)):
            attempts[-1]["terminal"] = True
            break
        if error_type in {"json", "schema", "empty_collection"}:
            response_size = len(_response_text(response))
            recovery_instruction = ""
            if collection_name and recovered_items:
                recovery_instruction = (
                    f"\n上一次前缀中检测到 {len(recovered_items)} 条完整候选（仅作诊断）："
                    f"{_recovery_summary(collection_name, recovered_items)}\n"
                    "本次必须重新输出该批次的完整 JSONL；不要依赖前缀恢复。"
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
                    "content": user_with_retry_feedback(
                        messages[1]["content"],
                        "## 上一次输出校验失败，请纠正后重新输出完整 JSONL\n"
                        f"错误类型：{error_type}\n错误：{message}\n"
                        f"上一次输出摘要：已省略原文，字符数 {response_size}\n"
                        f"{recovery_instruction}"
                        f"{empty_instruction}"
                        + ("不得解释错误，只输出 JSONL，每行一个符合 success_schema 的实体对象；无实体时只输出 {}。"
                         if collection_name == "entities" else "不得解释错误，只输出符合 schema 的 JSON 对象。")
                    ),
                },
            ]
        response_mode = next_response_mode
        if retry_base_seconds > 0:
            from .request_budget import document_deadline
            deadline = document_deadline(llm)
            delay = retry_base_seconds * (2 ** (attempt_number - 1))
            if deadline is not None:
                delay = min(delay, max(0, deadline - time.monotonic()))
            time.sleep(delay)
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
            if ChunkContextManager([chunk]).find_span(chunk.chunk_id, quote) is not None:
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


def entity_pass(
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
        from .property_grounding import ground_properties

        def scan(part, types, depth=0):
            if document_expired(llm):
                local_attempts.append({'attempt': 1, 'type': 'timeout',
                    'pass': 'repair_entity' if task == 'repair' else 'entity',
                    'chunk': chunk.chunk_id, 'message': 'document deadline exhausted',
                    'retryable': False, 'terminal': True})
                return []
            output_schema = output_schema_for_types(plan, "entity_candidates", types)
            messages = build_chat_messages(assembly_planner=assembly_planner,
                task=task, plan=plan, route=route, chunk=part, output_schema=output_schema,
                repair_issues=repair_issues)
            before = len(local_attempts)
            # Schema partitions are scheduling only: visit every allowed type.
            # Shrink before calling, including ontology, skills and metadata.
            too_big = not fits(messages, llm)
            parsed = None
            if not too_big:
                parsed = generate_structured(llm, messages, output_schema,
                    use_scheduler=use_scheduler, attempts=local_attempts,
                    pass_name="repair_entity" if task == "repair" else "entity", chunk_id=chunk.chunk_id,
                    max_attempts=max_attempts, retry_base_seconds=retry_base_seconds,
                    call_metrics=local_call_metrics,
                    split_on_overflow=depth < 12 and (len(types) > 1 or len(part.text) > 256),
                    validator=lambda payload: validate_candidate_properties(payload, plan, chunk=part))
            if parsed is None and document_expired(llm):
                return []
            if too_big or (parsed is None and split_request_failure(local_attempts[before:])):
                if depth < 12 and (len(types) > 1 or len(part.text) > 256):
                    parent_failures = local_attempts[before:]
                    recover_split_attempts(parent_failures)
                    if len(types) > 1 and too_big:
                        mid = len(types) // 2
                        return scan(part, types[:mid], depth + 1) + scan(part, types[mid:], depth + 1)
                    if len(part.text) > 256:
                        return [entity for sub in windows(part, max(128, len(part.text) // 2))
                                for entity in scan(sub, types, depth + 1)]
                    mid = len(types) // 2
                    return scan(part, types[:mid], depth + 1) + scan(part, types[mid:], depth + 1)
                if too_big:
                    local_attempts.append({"attempt": 0, "type": "input_budget",
                        "pass": "repair_entity" if task == "repair" else "entity", "chunk": chunk.chunk_id,
                        "message": "minimum source/schema window exceeds request budget", "terminal": True})
                return []
            if parsed is None:
                return []
            entities = _normalize_span_entities(parsed, chunk=part, schema=schema,
                target_types=request.target_types, warnings=local_warnings)
            return ground_properties(entities, [part], llm, attempts=local_attempts,
                warnings=local_warnings, use_scheduler=use_scheduler, max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds, call_metrics=local_call_metrics, plan=plan,
                route=route, assembly_planner=assembly_planner)

        grounded_entities = [entity for part in windows(chunk, window_chars(llm))
                             for entity in scan(part, sorted(plan.relevant_types(part.text)))]
        return (
            chunk.chunk_id,
            grounded_entities,
            local_attempts,
            local_warnings,
            local_call_metrics,
        )

    def workers():
        worker_count = max(1, max_workers)
        # Upstream may supply one enormous chunk. Schedule bounded windows, so
        # that case can still use the configured concurrency without an
        # unbounded queue or a separate executor per chunk.
        pending_chunks = (part for chunk in chunks
                          for part in iter_windows(chunk, window_chars(llm)))
        if worker_count == 1:
            yield from map(run, pending_chunks)
            return
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ke-entity") as executor:
            pending = deque(executor.submit(run, c) for c in islice(pending_chunks, worker_count * 2))
            while pending:
                yield pending.popleft().result()
                chunk = next(pending_chunks, None)
                if chunk is not None:
                    pending.append(executor.submit(run, chunk))
    results: dict[str, list[dict[str, Any]]] = {chunk.chunk_id: [] for chunk in chunks}
    for chunk_id, entities, local_attempts, local_warnings, local_calls in workers():
        attempts.extend(local_attempts)
        warnings.extend(local_warnings)
        if call_metrics is not None:
            for call in local_calls:
                _append_call(call_metrics, call)
        results[chunk_id].extend(entities)
    return list(results.items())


def _append_call(call_metrics, call):
    # Full prompts are debugging data, not extraction output. Bound retention
    # across the request while retaining every call's timing/status/usage.
    limit = max(0, int(os.getenv("KG_EXTRACT_V31_TRACE_CALLS", "32")))
    if len(call_metrics) >= limit:
        call.pop("messages", None)
        call["messages_omitted"] = True
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
            "messages": [dict(message) for message in messages],
        }
    )


__all__ = [
    "bind_entity_evidence",
    "decode_json_object",
    "entity_pass",
    "generate_structured",
]
