"""Consume generation streams while retaining complete, independently checked JSONL.

Stopping a local stream does not assert that the provider cancelled generation or
that an unknown usage is zero. The transport owns the reservation throughout.
"""
from __future__ import annotations

import json
import time


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


class RootObjectKeys:
    """Incrementally detect repeated root members, without repairing JSON.

    Keys are decoded only once their closing quote and colon have arrived.
    String escapes and nesting survive delta boundaries. Nested objects have
    their own scopes; complete adjacent root objects start a fresh key set.
    An unrecognized prefix disables this guard instead of guessing boundaries.
    """

    def __init__(self):
        self.stack = []
        self.keys = set()
        self.state = 'start'
        self.string = self.escaped = self.disabled = False
        self.key_text = self.pending_key = self.primitive = None

    def _disable(self):
        self.disabled = True
        self.key_text = self.pending_key = self.primitive = None

    def _character(self, char):
        if self.string:
            if self.key_text is not None:
                self.key_text.append(char)
            if self.escaped:
                self.escaped = False
            elif char == '\\':
                self.escaped = True
            elif char == '"':
                self.string = False
                if self.key_text is not None:
                    try:
                        self.pending_key = json.loads(''.join(self.key_text))
                    except ValueError:
                        self._disable()
                        return
                    self.key_text = None
                    self.state = 'colon'
                elif len(self.stack) == 1:
                    self.state = 'after_value'
            return

        if self.primitive is not None:
            if char not in ' \t\r\n,}':
                self.primitive.append(char)
                return
            try:
                json.loads(''.join(self.primitive))
            except ValueError:
                self._disable()
                return
            self.primitive = None
            self.state = 'after_value'
            # The delimiter still belongs to the root object below.

        if char in ' \t\r\n':
            return
        if len(self.stack) > 1:
            if char == '"':
                self.string = True
            elif char in '{[':
                self.stack.append(char)
            elif char in '}]':
                if self.stack[-1] != ('{' if char == '}' else '['):
                    self._disable()
                    return
                self.stack.pop()
                if len(self.stack) == 1:
                    self.state = 'after_value'
            return

        if self.state == 'start':
            if char != '{':
                self._disable()
                return
            self.stack.append(char)
            self.keys.clear()
            self.state = 'first_key'
        elif self.state in {'first_key', 'key'}:
            if char == '"':
                self.string = True
                self.key_text = ['"']
            elif char == '}' and self.state == 'first_key':
                self.stack.pop()
                self.state = 'start'
            else:
                self._disable()
        elif self.state == 'colon':
            if char != ':':
                self._disable()
                return
            if self.pending_key in self.keys:
                return 'repetitive_malformed_object'
            self.keys.add(self.pending_key)
            self.pending_key = None
            self.state = 'value'
        elif self.state == 'value':
            if char == '"':
                self.string = True
            elif char in '{[':
                self.stack.append(char)
                self.state = 'nested'
            elif char in '-0123456789tfn':
                self.primitive = [char]
            else:
                self._disable()
        elif self.state == 'after_value':
            if char == ',':
                self.state = 'key'
            elif char == '}':
                self.stack.pop()
                self.state = 'start'
            else:
                self._disable()

    def feed(self, text):
        for char in text:
            if self.disabled:
                break
            reason = self._character(char)
            if reason:
                return reason
        return None


class EntityProgress:
    """Detect full-record cycles, not repeated entity names or ordinary tokens."""

    def __init__(self):
        self.seen = set()
        self.repeated = self.empty_quotes = self.records = 0

    def line(self, text):
        try:
            record = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(record, dict) or not record.get('type') or not record.get('mention'):
            return None
        self.records += 1
        signature = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        self.repeated = self.repeated + 1 if signature in self.seen else 0
        self.seen.add(signature)
        evidence = record.get('evidence')
        quote = evidence.get('quote') if isinstance(evidence, dict) else None
        self.empty_quotes = self.empty_quotes + 1 if not isinstance(quote, str) or not quote.strip() else 0
        if self.repeated >= 12:
            return 'repetitive_entity_output'
        if self.empty_quotes >= 8:
            return 'consecutive_empty_evidence'
        return None


def consume_entity_stream(stream, *, deadline=None, character_limit=1048576):
    """Aggregate original text/reasoning and usage; interrupted output is incomplete."""
    # Compatible servers occasionally ignore stream=true and return a full body.
    if isinstance(stream, dict) or _field(stream, 'choices') is not None:
        return stream
    content, reasoning = [], []
    tail = ''
    count = 0
    progress = EntityProgress()
    root_keys = RootObjectKeys()
    response_id = finish = usage = stop_reason = None
    received_finish = False
    try:
        for chunk in stream:
            if deadline is not None and time.monotonic() >= deadline:
                stop_reason = 'stream_deadline'
                break
            response_id = _field(chunk, 'id') or response_id
            raw_usage = _field(chunk, 'usage')
            if raw_usage is not None:
                usage = raw_usage if isinstance(raw_usage, dict) else raw_usage.model_dump()
            choices = _field(chunk, 'choices', []) or []
            if not choices:
                continue
            choice = choices[0]
            finish = _field(choice, 'finish_reason') or finish
            received_finish = received_finish or finish is not None
            delta = _field(choice, 'delta', {})
            text = _field(delta, 'content', '') or ''
            thought = _field(delta, 'reasoning_content', '') or ''
            if not isinstance(text, str) or not isinstance(thought, str):
                stop_reason = 'invalid_stream_delta'
                break
            content.append(text)
            reasoning.append(thought)
            count += len(text) + len(thought)
            if count > character_limit:
                stop_reason = 'stream_character_budget'
                break
            tail += text
            while '\n' in tail:
                line, tail = tail.split('\n', 1)
                stop_reason = progress.line(line)
                if stop_reason:
                    break
            stop_reason = stop_reason or root_keys.feed(text)
            if stop_reason:
                break
        if not stop_reason and not received_finish:
            stop_reason = 'stream_ended_without_finish'
    except Exception:
        if not content and not reasoning:
            raise
        # Preserve only received output, never exception messages/headers.
        stop_reason = 'stream_interrupted'
    finally:
        close = getattr(stream, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                # Closing a broken socket must not erase received output or
                # replace the original transport failure with credential text.
                if content or reasoning:
                    stop_reason = stop_reason or 'stream_close_failed'
    return {'id': response_id, 'choices': [{'message': {
        'content': ''.join(content), 'reasoning_content': ''.join(reasoning)},
        'finish_reason': finish if not stop_reason else 'cancelled'}],
        'usage': usage, 'status': 'incomplete' if stop_reason else 'completed',
        'incomplete_details': {'reason': stop_reason} if stop_reason else {},
        'streaming': {'received_characters': count, 'complete_records': progress.records,
                      'stop_reason': stop_reason, 'usage_unavailable': usage is None}}
