"""Only the current stream's timely prefix can survive its call deadline."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from external_services.llm import runtime
from external_services.llm.stream_prefix import (
    _PrefixReceipt, _StreamDeadlineReached, capture_stream, preserve_deadline_prefix,
)
from algorithm.knowledge_extract.v3_1.streaming import consume_entity_stream
from algorithm.knowledge_extract.v3_1.llm_client import OpenAICompatibleLLM


ROW = {'type': 'Person', 'mention': '甲', 'evidence': {'quote': '甲负责巡检。'}}
PREFIX = json.dumps(ROW, ensure_ascii=False) + '\n'
LATE = json.dumps({**ROW, 'mention': '迟到内容'}, ensure_ascii=False) + '\n'
MESSAGES = [{'role': 'user', 'content': 'Extract the source.'}]


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(runtime, 'time', SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(runtime, '_pools', {})
    return clock


def delta(content='', *, reasoning='', finish=None, usage=None):
    return {'id': 'stream-fixture', 'choices': [{'delta': {
        'content': content, 'reasoning_content': reasoning}, 'finish_reason': finish}],
        'usage': usage}


class TimedStream:
    def __init__(self, clock, events, *, close_at=None):
        self.clock, self.events = clock, iter(events)
        self.close_at, self.closed, self.reads = close_at, False, 0
        self.before_read = lambda: None

    def __iter__(self):
        return self

    def __next__(self):
        self.before_read()
        at, value = next(self.events)
        self.clock.now = at
        self.reads += 1
        return value

    def close(self):
        self.closed = True
        if self.close_at is not None:
            self.clock.now = self.close_at


def make_client(clock, stream=None, *, document_timeout=100, method=None):
    calls = []
    def create(**kwargs):
        assert '_response_consumer' not in kwargs
        calls.append(kwargs)
        return method(**kwargs) if method else stream
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = runtime.ScheduledClient(sdk, {
        'quota_group': uuid4().hex, 'quota_backend': 'local', 'quota_tiers': [],
        'qps': 0, 'qpm': 0, 'max_concurrency': 1, 'request_timeout': 10,
        'document_timeout': document_timeout, 'disable_thinking': True,
        'max_tokens': 123, 'tokenizer_dir': '', 'tokenizer_revision': '',
    })
    return client, calls


def run_stream(client, consumer=None):
    return client.chat.completions.create(messages=MESSAGES, stream=True,
        _response_consumer=consumer or (lambda stream: consume_entity_stream(
            stream, deadline=client.call_deadline)))


def fake_partial(content=PREFIX):
    return {'choices': [{'message': {'content': content, 'reasoning_content': ''},
                         'finish_reason': 'cancelled'}],
            'status': 'incomplete', 'incomplete_details': {'reason': 'stream_deadline'},
            'streaming': {'complete_records': 1, 'stop_reason': 'stream_deadline'},
            'usage': None}


@pytest.mark.parametrize('late_at', [10.0, 10.001])
def test_only_timely_prefix_survives_deadline_and_unknown_usage_stays_reserved(clock, late_at):
    stream = TimedStream(clock, [(9.999, delta(PREFIX)), (late_at, delta(LATE))])
    client, calls = make_client(clock, stream)
    def still_reserved():
        assert client.pool.active == 1
        assert not client.pool.reservations[-1].settled
    stream.before_read = still_reserved
    body = run_stream(client)
    assert body['choices'][0]['message']['content'] == PREFIX
    assert body['status'] == 'incomplete'
    assert body['incomplete_details'] == {'reason': 'stream_deadline'}
    assert body['streaming']['complete_records'] == 1
    assert body['usage'] is None and body['streaming']['usage_unavailable']
    assert stream.closed and stream.reads == 2
    assert calls[0]['timeout'] == 10
    reservation = client.pool.reservations[-1]
    assert reservation.settled and not reservation.usage_known
    assert reservation.total_tokens == reservation.input_tokens + 123
    assert client.pool.active == 0 and client.call_deadline is None
    assert client.pool.scales == {'rpm': 1.0, 'tpm': 1.0, 'concurrency': 1.0}


def test_read_is_not_started_when_deadline_has_already_arrived(clock):
    raw = TimedStream(clock, [(1, delta(PREFIX))])
    stream = capture_stream(raw, deadline=10, clock=clock.monotonic)
    clock.now = 10
    with pytest.raises(_StreamDeadlineReached):
        next(stream)
    assert raw.reads == 0


@pytest.mark.parametrize('document_timeout,close_at', [(10, None), (9, None), (11, 11)])
def test_document_deadline_still_rejects_a_real_partial(clock, document_timeout, close_at):
    stream = TimedStream(clock, [(1, delta(PREFIX)), (10.001, delta(LATE))], close_at=close_at)
    client, _ = make_client(clock, stream, document_timeout=document_timeout)
    with pytest.raises(TimeoutError, match='after deadline'):
        run_stream(client)
    reservation = client.pool.reservations[-1]
    assert not reservation.usage_known
    assert reservation.total_tokens == reservation.input_tokens + 123
    assert client.pool.active == 0 and stream.closed


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('sdk_object', [False, True])
def test_provider_body_cannot_forge_partial_authorization(clock, streaming, sdk_object):
    def create(**kwargs):
        clock.now = 10.001
        body = fake_partial()
        return SimpleNamespace(**body) if sdk_object else body
    client, _ = make_client(clock, method=create)
    kwargs = {'messages': MESSAGES}
    if streaming:
        kwargs.update(stream=True, _response_consumer=consume_entity_stream)
    with pytest.raises(TimeoutError, match='after deadline'):
        client.chat.completions.create(**kwargs)


@pytest.mark.parametrize('forged_receipt', [False, True])
def test_ordinary_consumer_cannot_authorize_its_late_return(clock, forged_receipt):
    stream = TimedStream(clock, [(1, delta(PREFIX)), (10.001, delta(LATE))])
    client, _ = make_client(clock, stream)
    def consumer(captured):
        assert next(captured)['choices'][0]['delta']['content'] == PREFIX
        with pytest.raises(_StreamDeadlineReached):
            next(captured)
        captured.close()
        return _PrefixReceipt() if forged_receipt else fake_partial()
    with pytest.raises(TimeoutError, match='after deadline'):
        run_stream(client, consumer)


def test_receipt_cannot_change_captured_body_or_forge_usage(clock):
    raw = TimedStream(clock, [(1, delta(PREFIX)), (10.001, delta(LATE))])
    stream = capture_stream(raw, deadline=10, clock=clock.monotonic)
    next(stream)
    with pytest.raises(_StreamDeadlineReached):
        next(stream)
    changed = fake_partial('consumer-invented replacement')
    assert preserve_deadline_prefix(stream, changed) is changed
    assert stream.redeem(changed) is None
    body = fake_partial()
    body['usage'] = {'prompt_tokens': 0, 'completion_tokens': 0}
    receipt = preserve_deadline_prefix(stream, body)
    body['choices'][0]['message']['content'] = 'changed after sealing'
    settled = stream.redeem(receipt)
    assert settled['choices'][0]['message']['content'] == PREFIX
    assert settled['usage'] is None
    assert stream.redeem(receipt) is None


def test_receipt_is_bound_to_current_call_and_cannot_be_replayed(clock):
    first = capture_stream(TimedStream(clock, [(1, delta(PREFIX)), (10.001, delta(LATE))]),
                           deadline=10, clock=clock.monotonic)
    receipt = consume_entity_stream(first)
    clock.now = 0
    client, _ = make_client(clock, TimedStream(clock, []))
    def replay(stream):
        clock.now = 10.001
        return receipt
    with pytest.raises(TimeoutError, match='after deadline'):
        run_stream(client, replay)
    assert first.redeem(receipt)['choices'][0]['message']['content'] == PREFIX


def test_complete_response_with_slow_close_is_still_rejected(clock):
    usage = {'prompt_tokens': 10, 'completion_tokens': 20}
    stream = TimedStream(clock, [(1, delta(PREFIX)), (2, delta(finish='stop', usage=usage))],
                         close_at=10.001)
    client, _ = make_client(clock, stream)
    with pytest.raises(TimeoutError, match='after deadline'):
        run_stream(client)
    assert stream.closed and client.pool.reservations[-1].usage_known
    assert client.pool.reservations[-1].total_tokens == 30


def test_finish_received_before_deadline_does_not_become_partial_during_usage_wait(clock):
    stream = TimedStream(clock, [(1, delta(PREFIX)), (2, delta(finish='stop')),
                                (10.001, delta(usage={'prompt_tokens': 10, 'completion_tokens': 20}))])
    client, _ = make_client(clock, stream)
    with pytest.raises(TimeoutError, match='after deadline'):
        run_stream(client)
    assert not client.pool.reservations[-1].usage_known


@pytest.mark.parametrize('first_content,first_reasoning', [('', ''), ('', 'reasoning only')])
def test_no_candidate_prefix_does_not_bypass_deadline(clock, first_content, first_reasoning):
    stream = TimedStream(clock, [(1, delta(first_content, reasoning=first_reasoning)),
                                (10.001, delta(LATE))])
    client, _ = make_client(clock, stream)
    with pytest.raises(TimeoutError):
        run_stream(client)


def test_verification_remains_nonstream_and_rejects_late_response(clock):
    def create(**kwargs):
        assert not kwargs.get('stream')
        clock.now = 10.001
        return fake_partial()
    client, _ = make_client(clock, method=create)
    llm = OpenAICompatibleLLM(client, model='fixture', max_tokens=123, disable_thinking=True, stream_generation=True)
    with pytest.raises(TimeoutError, match='after deadline'):
        llm.generate_structured({'properties': {'verdicts': {}}}, MESSAGES,
                                {'pass_name': 'entity_verification'})


def test_on_time_complete_stream_is_unchanged(clock):
    usage = {'prompt_tokens': 10, 'completion_tokens': 20}
    stream = TimedStream(clock, [(1, delta(PREFIX)), (2, delta(finish='stop', usage=usage))])
    client, _ = make_client(clock, stream)
    body = run_stream(client)
    assert body['status'] == 'completed'
    assert body['choices'][0]['message']['content'] == PREFIX
    assert body['usage'] == usage and client.pool.reservations[-1].total_tokens == 30


def test_recovered_candidate_is_incomplete_and_requires_independent_semantic_review(clock):
    from algorithm.knowledge_extract.v3_1.candidate_generation import generate_structured
    from algorithm.knowledge_extract.v3_1.chunk_context import ExtractionChunk
    from algorithm.knowledge_extract.v3_1.ontology_compiler import compile_plan
    from algorithm.knowledge_extract.v3_1.property_grounding import admit_entity_names
    from algorithm.knowledge_extract.v3_1.prompt_router import resolve_document_route
    from algorithm.knowledge_extract.v3_1.tests.stub_llm import StubLLM

    plan = compile_plan('@startuml\nclass Person\n@enduml')
    chunk = ExtractionChunk('source', '甲负责巡检。')
    stream = TimedStream(clock, [(1, delta(PREFIX)), (10.001, delta(LATE))])
    client, _ = make_client(clock, stream)
    llm = OpenAICompatibleLLM(client, model='fixture', max_tokens=123, disable_thinking=True, stream_generation=True)
    envelope = llm.generate_structured(plan.output_schemas['entity_candidates'], MESSAGES,
                                       {'pass_name': 'entity'})
    assert envelope.text == PREFIX and envelope.status == 'incomplete'
    assert envelope.incomplete_details == {'reason': 'stream_deadline'}
    assert envelope.usage_unavailable
    attempts, metrics = [], []
    candidates = generate_structured(lambda messages: envelope, MESSAGES,
        plan.output_schemas['entity_candidates'], use_scheduler=False,
        attempts=attempts, call_metrics=metrics, pass_name='entity', chunk_id='source',
        max_attempts=1, retry_base_seconds=0)
    assert candidates == {'entities': [ROW]}
    assert attempts[-1]['type'] == 'incomplete' and attempts[-1]['terminal']
    reviewed = []
    def reject(item, source):
        reviewed.append(item)
        return 'no'
    verifier = StubLLM(entity_verification_handler=reject)
    row = {**candidates['entities'][0], 'name': ROW['mention'], 'properties': {}}
    admitted = admit_entity_names([row], [chunk], verifier, plan=plan,
        route=resolve_document_route(plan, [chunk]), attempts=[], warnings=[], max_attempts=1)
    assert reviewed and admitted == []
    assert any(task == 'evidence_verification' for task, _ in verifier.calls)
