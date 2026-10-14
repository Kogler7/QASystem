"""Clef-flash 预判：关系封闭分类与证据真伪，失败时交回生成式模型。

服务不可用、超时、超长或低置信都不是负例。只有高置信 ``NONE`` 且两端名称
都出现在送入 Clef 的原文窗口里，才会跳过该关系的生成式调用。
"""
from __future__ import annotations

import inspect
import re
import threading
import time
from collections import OrderedDict
from typing import Any

from ._logging import get_step_logger
from .relation_resolver import pair_relation_ids


logger = get_step_logger(__name__)

_SENTENCE = re.compile(r"[^。！？\n]+[。！？\n]?")
_STAT_KEYS = (
    "relation_seen",
    "relation_pruned_no_cooccurrence",
    "relation_dropped",
    "relation_positive",
    "relation_uncertain",
    "relation_grounded_without_llm",
    "relation_sent_to_llm",
    "verification_items",
    "verification_by_clef",
    "verification_fallback_llm",
    "unavailable",
    "repair_required_relation_skipped",
    "relation_kept_anchor",
    "relation_kept_same_sentence",
    "relation_kept_same_unit",
    "relation_kept_adjacent_unit",
    "relation_kept_same_paragraph",
    "relation_kept_adjacent_paragraph",
)


def sentence_cooccurrence(text: str, left: str, right: str) -> bool:
    """两端名称是否出现在同一句（用于裁掉无局部证据的笛卡尔 pair）。"""
    return surface_cooccurrence(text, [left], [right])


def surface_cooccurrence(text: str, left_forms: list[str], right_forms: list[str]) -> bool:
    """任一表面形式是否出现在同一句。规范名未逐字出现时，原文 mention 仍可共现。"""
    if not text:
        return False
    left = [form for form in left_forms if form and form in text]
    right = [form for form in right_forms if form and form in text]
    if not left or not right:
        return False
    for sentence in _SENTENCE.findall(text):
        if any(form in sentence for form in left) and any(form in sentence for form in right):
            return True
    return False


def paragraph_spans(text: str) -> list[tuple[int, int]]:
    """按空行切出段落的字符区间，区间相对传入文本。"""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    end = 0
    offset = 0
    for raw in (text or "").splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        if line.strip():
            content_start = offset + (len(line) - len(line.lstrip()))
            content_end = offset + len(line.rstrip())
            if start is None:
                start = content_start
            end = content_end
        elif start is not None:
            spans.append((start, end))
            start = None
        offset += len(raw)
    if start is not None:
        spans.append((start, end))
    return spans


def _span_index(spans: list[tuple[int, int]], start: int | None, end: int | None) -> int | None:
    if start is None or end is None or start >= end:
        return None
    for index, (span_start, span_end) in enumerate(spans):
        if start < span_end and end > span_start:
            return index
    return None


# 没有空行的正文切片会变成一整段。超过该长度时，同段只保留证据位置接近的两端。
_LONG_PARAGRAPH_CHARS = 480
_LONG_PARAGRAPH_GAP = 120


def _span_gap(left: tuple[int, int], right: tuple[int, int]) -> int:
    return max(left[0], right[0]) - min(left[1], right[1])


def local_context_reason(
    text: str,
    left_span: tuple[int, int] | None,
    right_span: tuple[int, int] | None,
    unit_spans: list[tuple[int, int]] | None = None,
) -> str:
    """同结构单元或相邻段落可以进入关系分类；更远的共现不作为证据。"""
    if not left_span or not right_span:
        return ""
    units = sorted(unit_spans or [])
    left_unit = _span_index(units, *left_span)
    right_unit = _span_index(units, *right_span)
    if left_unit is not None and right_unit is not None:
        distance = abs(left_unit - right_unit)
        if distance == 0:
            return "same_unit"
        if distance == 1:
            return "adjacent_unit"
        # 两端已经落在不相邻的结构单元里。无空行的正文切片会被看成一整段，
        # 不能再据此把远处的单元配成同段候选。
        return ""
    paragraphs = paragraph_spans(text)
    left_paragraph = _span_index(paragraphs, *left_span)
    right_paragraph = _span_index(paragraphs, *right_span)
    if left_paragraph is None or right_paragraph is None:
        return ""
    distance = abs(left_paragraph - right_paragraph)
    if distance > 1:
        return ""
    involved = {left_paragraph, right_paragraph}
    if any(
        paragraphs[index][1] - paragraphs[index][0] > _LONG_PARAGRAPH_CHARS
        for index in involved
    ) and _span_gap(left_span, right_span) > _LONG_PARAGRAPH_GAP:
        return ""
    return "same_paragraph" if distance == 0 else "adjacent_paragraph"


def cooccurrence_quote(text: str, left: str, right: str, *, max_len: int = 400) -> str | None:
    """返回同时包含两个名称、且是原文子串的最短可用引文。"""
    if not text or not left or not right or left not in text or right not in text:
        return None
    for sentence in _SENTENCE.findall(text):
        quote = sentence.strip()
        if left in quote and right in quote and quote in text and len(quote) <= max_len:
            return quote
    start = min(text.find(left), text.find(right))
    end = max(text.find(left) + len(left), text.find(right) + len(right))
    quote = text[start:end]
    if quote and len(quote) <= max_len and quote in text:
        return quote
    return None


def _clip_around(text: str, left: str, right: str, limit: int) -> str:
    positions: list[int] = []
    for name in (left, right):
        if not name:
            continue
        index = text.find(name)
        if index >= 0:
            positions.extend((index, index + len(name)))
    if not positions:
        return text[:limit]
    start, end = min(positions), max(positions)
    if end - start >= limit:
        return text[start : start + limit]
    pad = limit - (end - start)
    start = max(0, start - pad // 2)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    return text[start:end]


def mention_window(text: str, left: str, right: str, limit: int) -> tuple[str, bool]:
    """截取包含两端名称的窗口。第二个返回值表示两个名称都在窗口内。"""
    text = text or ""
    limit = max(64, limit)
    visible = bool(left) and bool(right) and left in text and right in text
    if not visible:
        return text[:limit], False
    for sentence in _SENTENCE.findall(text):
        if left in sentence and right in sentence:
            window = sentence.strip() or sentence
            if len(window) > limit:
                window = _clip_around(sentence, left, right, limit)
            return window, left in window and right in window
    window = _clip_around(text, left, right, limit)
    return window, left in window and right in window


class ClefGate:
    """一次抽取共用的 Clef 客户端。

    默认最多 ``max_inflight`` 路并发（受服务 ``CLEF_MAX_QUEUE_SIZE=4`` 约束），
    避免打满单卡推理队列；设为 1 即完全串行。
    """

    def __init__(
        self,
        client: Any = None,
        *,
        enabled: bool = False,
        accept: float = 0.9,
        max_questions: int = 16,
        timeout: float = 10.0,
        max_calls: int = 512,
        state_chars: int = 800,
        max_inflight: int = 2,
    ) -> None:
        self.client = client
        self.enabled = bool(enabled and client is not None)
        self.accept = accept if 0.5 < accept <= 1.0 else 0.9
        self.max_questions = min(16, max(1, int(max_questions)))
        self.timeout = timeout if timeout > 0 else 10.0
        self.max_calls = max(1, int(max_calls))
        self.state_chars = max(64, int(state_chars))
        self.max_inflight = min(4, max(1, int(max_inflight)))
        self.disabled_reason = ""
        self._calls = 0
        self._lock = threading.Lock()
        self._inflight = threading.BoundedSemaphore(self.max_inflight)
        self._stats = {key: 0 for key in _STAT_KEYS}
        self._timeout_keyword = False
        if client is not None:
            try:
                parameters = inspect.signature(client.systemone).parameters.values()
                self._timeout_keyword = any(
                    parameter.name == "timeout" or parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                )
            except (TypeError, ValueError):
                self._timeout_keyword = False

    @classmethod
    def from_env(cls) -> "ClefGate":
        from .config import get_v22_clef_config

        config = get_v22_clef_config()
        if not config["enabled"]:
            return cls(enabled=False)
        try:
            from external_services.clef import ClefFlashClient

            client = ClefFlashClient(timeout=config["timeout"])
        except Exception:
            logger.warning("[步骤][Clef] 客户端初始化失败，本请求回退生成式模型")
            gate = cls(enabled=False)
            gate.disabled_reason = "client_init_failed"
            return gate
        if not client.base_url:
            gate = cls(enabled=False)
            gate.disabled_reason = "missing_service_url"
            return gate
        return cls(
            client=client,
            enabled=True,
            accept=config["accept"],
            max_questions=config["max_questions"],
            timeout=config["timeout"],
            max_calls=config["max_calls"],
            state_chars=config["state_chars"],
            max_inflight=config["max_inflight"],
        )

    def note(self, **counts: int) -> None:
        with self._lock:
            for key, value in counts.items():
                self._stats[key] = self._stats.get(key, 0) + int(value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            stats = dict(self._stats)
            calls = self._calls
        return {
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "calls": calls,
            "accept": self.accept,
            "max_questions": self.max_questions,
            "max_inflight": self.max_inflight,
            **stats,
        }

    def apply_relation_gate(
        self,
        text: str,
        pairs: list[dict[str, Any]],
        *,
        chunk_id: str,
        attempts: list[dict[str, Any]],
    ) -> tuple[list[tuple[dict[str, Any], str]], list[dict[str, Any]]]:
        """返回 (可直接落地的正例及引文, 仍需生成式模型的 pair)。"""
        if not self.enabled or not pairs:
            return [], list(pairs)
        grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        visible: dict[str, bool] = {}
        for pair in pairs:
            window, both = mention_window(
                text,
                str(pair.get("source_surface") or pair.get("source_name") or ""),
                str(pair.get("target_surface") or pair.get("target_name") or ""),
                self.state_chars,
            )
            grouped.setdefault(window, []).append(pair)
            visible[str(pair.get("pair_id") or "")] = both
        status: dict[str, tuple[str, str]] = {}
        for window, group in grouped.items():
            questions: dict[str, dict[str, Any]] = {}
            for pair in group:
                question = _relation_question(pair)
                pair_id = str(pair.get("pair_id") or "")
                if question is None or not pair_id:
                    status[pair_id] = ("uncertain", "")
                    continue
                questions[pair_id] = question
            answers = self._complete(
                window, questions, pass_name="clef_relation", chunk_id=chunk_id, attempts=attempts,
            )
            for pair in group:
                pair_id = str(pair.get("pair_id") or "")
                if pair_id in status:
                    continue
                status[pair_id] = _relation_status(
                    answers.get(pair_id),
                    relation_ids=pair_relation_ids(pair),
                    accept=self.accept,
                    both_names_visible=visible.get(pair_id, False),
                    preserve=bool(pair.get("preserve_on_none")),
                )
        grounded: list[tuple[dict[str, Any], str]] = []
        pending: list[dict[str, Any]] = []
        dropped = positive = uncertain = 0
        for pair in pairs:
            pair_id = str(pair.get("pair_id") or "")
            label, chosen = status.get(pair_id, ("uncertain", ""))
            relation_id = chosen or (pair_relation_ids(pair)[:1] or [""])[0]
            if label == "drop":
                dropped += 1
                continue
            if label == "positive":
                positive += 1
                pair = {**pair, "relation_id": relation_id}
                # RELATED_TO 过于宽泛，Clef 正例直接落地易灌假阳；交给生成式写证据。
                if _is_related_to(relation_id):
                    uncertain += 1
                    pending.append(pair)
                    continue
                quote = cooccurrence_quote(
                    text,
                    str(pair.get("source_surface") or pair.get("source_name") or ""),
                    str(pair.get("target_surface") or pair.get("target_name") or ""),
                )
                if quote:
                    grounded.append((pair, quote))
                    continue
            else:
                uncertain += 1
            pending.append(pair)
        self.note(
            relation_seen=len(pairs),
            relation_dropped=dropped,
            relation_positive=positive,
            relation_uncertain=uncertain,
        )
        logger.info(
            "[步骤][Clef] 关系预判 chunk=%s pairs=%d drop=%d positive=%d grounded=%d llm=%d",
            chunk_id,
            len(pairs),
            dropped,
            positive,
            len(grounded),
            len(pending),
        )
        return grounded, pending

    def verify_items(
        self,
        text: str,
        items: list[dict[str, Any]],
        *,
        chunk_id: str,
        attempts: list[dict[str, Any]],
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """返回 (已判定 item_id -> yes/no/uncertain, 需要回退生成式核验的项)。"""
        if not self.enabled or not items:
            return {}, list(items)
        grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        for item in items:
            quote = str(item.get("evidence_quote") or "")
            window, _ = mention_window(text, quote, quote, self.state_chars)
            grouped.setdefault(window, []).append(item)
        verdicts: dict[str, str] = {}
        for window, group in grouped.items():
            questions = {
                str(item.get("item_id") or ""): {
                    "type": "noul",
                    "instructions": _support_instruction(item),
                }
                for item in group
                if str(item.get("item_id") or "")
            }
            answers = self._complete(
                window, questions, pass_name="clef_verification", chunk_id=chunk_id, attempts=attempts,
            )
            for item in group:
                item_id = str(item.get("item_id") or "")
                verdict = _noul_status(answers.get(item_id), accept=self.accept)
                if verdict is not None:
                    verdicts[item_id] = verdict
        pending = [item for item in items if str(item.get("item_id") or "") not in verdicts]
        self.note(
            verification_items=len(items),
            verification_by_clef=len(verdicts),
            verification_fallback_llm=len(pending),
        )
        return verdicts, pending

    def _complete(
        self,
        state: str,
        questions: dict[str, dict[str, Any]],
        *,
        pass_name: str,
        chunk_id: str,
        attempts: list[dict[str, Any]],
        depth: int = 0,
    ) -> dict[str, Any]:
        if not questions:
            return {}
        if len(questions) > self.max_questions:
            merged: dict[str, Any] = {}
            keys = list(questions)
            for start in range(0, len(keys), self.max_questions):
                subset = {key: questions[key] for key in keys[start : start + self.max_questions]}
                merged.update(self._complete(
                    state, subset, pass_name=pass_name, chunk_id=chunk_id, attempts=attempts, depth=depth,
                ))
            return merged
        body, failure = self._invoke({"state": state, "questions": questions})
        if body is not None:
            answers = body.get("answers")
            if isinstance(answers, dict) and set(answers) == set(questions):
                _record_attempt(attempts, pass_name, chunk_id, "success", len(questions))
                return answers
            failure = "invalid_response"
        if failure == "input_too_long" and len(questions) > 1 and depth < 4:
            keys = list(questions)
            middle = len(keys) // 2
            left = {key: questions[key] for key in keys[:middle]}
            right = {key: questions[key] for key in keys[middle:]}
            merged = self._complete(
                state, left, pass_name=pass_name, chunk_id=chunk_id, attempts=attempts, depth=depth + 1,
            )
            merged.update(self._complete(
                state, right, pass_name=pass_name, chunk_id=chunk_id, attempts=attempts, depth=depth + 1,
            ))
            return merged
        self.note(unavailable=1)
        _record_attempt(attempts, pass_name, chunk_id, failure or "unavailable", len(questions))
        logger.info(
            "[步骤][Clef] 预判未完成 pass=%s chunk=%s questions=%d reason=%s",
            pass_name,
            chunk_id,
            len(questions),
            failure or "unavailable",
        )
        return {}

    def _reserve_call(self) -> bool:
        with self._lock:
            if self._calls >= self.max_calls:
                return False
            self._calls += 1
            return True

    def _invoke(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        if not self._reserve_call():
            return None, "call_budget_exhausted"
        kwargs = {"timeout": self.timeout} if self._timeout_keyword else {}
        self._inflight.acquire()
        try:
            try:
                body = self.client.systemone(payload, **kwargs)
            except Exception as exc:
                kind = str(getattr(exc, "error_kind", "") or "client_error")
                retryable = bool(getattr(exc, "retryable", False))
                retry_after = getattr(exc, "retry_after", None)
                if retryable and self._reserve_call():
                    delay = retry_after if isinstance(retry_after, (int, float)) and retry_after > 0 else 0.05
                    time.sleep(min(1.0, float(delay)))
                    try:
                        body = self.client.systemone(payload, **kwargs)
                    except Exception as retry_exc:
                        return None, str(getattr(retry_exc, "error_kind", "") or "client_error")
                else:
                    return None, kind
        finally:
            self._inflight.release()
        if not isinstance(body, dict):
            return None, "invalid_response"
        return body, None


def resolve_clef(clef: Any) -> ClefGate:
    """``None`` 读环境变量，``False`` 关闭，客户端对象则包成开启的 gate。"""
    if isinstance(clef, ClefGate):
        return clef
    if clef is None:
        return ClefGate.from_env()
    if clef is False:
        return ClefGate(enabled=False)
    if hasattr(clef, "systemone"):
        return ClefGate(client=clef, enabled=True)
    raise TypeError("clef 需要 ClefGate、带 systemone 的客户端，或 False")


def _relation_question(pair: dict[str, Any]) -> dict[str, Any] | None:
    relations = pair_relation_ids(pair)
    source = str(pair.get("source_name") or "")
    target = str(pair.get("target_name") or "")
    if not relations or not source or not target:
        return None
    if len(relations) == 1:
        relation = relations[0]
        instructions = f"只根据原文，{source}与{target}之间是否存在关系「{relation}」？"
        none_text = "原文不支持这条关系"
    else:
        listed = "、".join(relations)
        instructions = f"只根据原文，{source}与{target}之间应选择哪个关系：{listed}？"
        none_text = "原文不支持这些关系中的任何一个"
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": {
            **{relation: f"原文支持{source}与{target}的{relation}" for relation in relations},
            "NONE": none_text,
        },
    }


def _is_related_to(relation_id: str) -> bool:
    return "".join(ch for ch in str(relation_id).casefold() if ch.isalnum()) == "relatedto"


def _relation_status(
    answer: Any,
    *,
    relation_ids: list[str],
    accept: float,
    both_names_visible: bool,
    preserve: bool = False,
) -> tuple[str, str]:
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return "uncertain", ""
    choice = answer.get("choice")
    if not isinstance(choice, str):
        return "uncertain", ""
    confidence = answer.get("confidence")
    probabilities = answer.get("probabilities")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        if isinstance(probabilities, dict) and isinstance(probabilities.get(choice), (int, float)):
            confidence = probabilities[choice]
        else:
            return "uncertain", ""
    if float(confidence) < accept:
        return "uncertain", ""
    if choice in relation_ids:
        return "positive", choice
    # 文档锚点等保留 pair：Clef 高置信 NONE 仍回退生成式，避免误杀 APPLIES_TO 等公文关系。
    if choice == "NONE" and both_names_visible and not preserve:
        return "drop", "NONE"
    return "uncertain", ""


def _support_instruction(item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or "候选")
    name = str(item.get("mention") or item.get("entity") or "")
    quote = str(item.get("evidence_quote") or "")
    if len(quote) > 80:
        quote = quote[:80]
    return f"原文是否支持{kind}「{name}」？证据片段：{quote}"


def _noul_status(answer: Any, *, accept: float) -> str | None:
    if not isinstance(answer, dict) or answer.get("type") != "noul":
        return None
    score = answer.get("noul")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    if float(score) >= accept:
        return "yes"
    if float(score) <= 1.0 - accept:
        return "no"
    return "uncertain"


def _record_attempt(
    attempts: list[dict[str, Any]],
    pass_name: str,
    chunk_id: str,
    kind: str,
    questions: int,
) -> None:
    attempts.append({
        "attempt": 1,
        "type": kind,
        "pass": pass_name,
        "chunk": chunk_id,
        "terminal": True,
        "retryable": False,
        "message": f"questions={questions}",
    })
