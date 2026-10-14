"""Work-conserving scheduling, deferred retries and request-isolated proof."""
import asyncio
import json
import threading

import pytest

from ..task_runtime import synchronous, current_runtime, ordered_tasks, document_tasks, invoke
from ..candidate_generation import generate_structured
from ..chunk_context import ExtractionChunk
from ..ontology_compiler import compile_plan
from ..property_grounding import ground_properties
from ..property_enrichment import enrich_entity_properties
from ..chunk_context import ChunkContextManager
from ..prompt_router import resolve_document_route
from ..split_extractor import extract_split
from .stub_llm import StubLLM, span_item


ONTOLOGY = '@startuml\nclass Person {\n duty: String\n}\n@enduml'


def test_exhausted_sync_adapter_fails_without_stranding_the_awaiting_request():
    from ..task_runtime import TaskRuntime
    exhausted = iter(())
    runtime = TaskRuntime(1, None)
    async def scenario():
        with pytest.raises(RuntimeError, match='model call ended without a result'):
            await asyncio.wait_for(
                runtime.call(lambda: next(exhausted), stage='entity', llm=None),
                timeout=2,
            )
        assert await runtime.call(lambda: 'next request', stage='entity', llm=None) == 'next request'
    asyncio.run(runtime.run(scenario()))


def test_slow_first_task_does_not_block_replacement_submission():
    third_started = threading.Event()
    def call(index):
        if index == 0:
            assert third_started.wait(2), 'idle worker never received the third task'
        if index == 2:
            third_started.set()
        return index
    @synchronous
    async def scenario(*, max_workers):
        async def task(index):
            return await current_runtime().call(lambda: call(index), stage='entity', llm=None)
        return await ordered_tasks(task, range(4), limit=2)
    assert scenario(max_workers=2) == [0, 1, 2, 3]


def test_legacy_ordered_map_refills_without_unbounded_result_buffer():
    from ..parallel import ordered_map
    third_started = threading.Event()
    def call(index):
        if index == 0:
            assert third_started.wait(2)
        if index == 2:
            third_started.set()
        return index
    assert list(ordered_map(call, range(20), max_workers=2)) == list(range(20))
    assert list(ordered_map(lambda x: x, [None, 1], max_workers=2)) == [None, 1]


def test_delayed_retry_releases_worker_and_discovery_queue_capacity():
    calls = []
    class TemporaryFailure(RuntimeError):
        status_code = 503
    def llm(messages):
        which = json.loads(messages[1]['content'])['which']
        calls.append(which)
        if which == 0 and calls.count(0) == 1:
            raise TemporaryFailure('temporary')
        return '{"ok":true}'
    schema = {'type': 'object', 'required': ['ok'], 'properties': {'ok': {'type': 'boolean'}},
              'additionalProperties': False}
    @synchronous
    async def scenario(*, max_workers):
        async def task(which):
            return await invoke(generate_structured, llm,
                [{'role': 'system', 'content': 'test'}, {'role': 'user', 'content': json.dumps({'which': which})}],
                schema, use_scheduler=False, attempts=[], pass_name='entity', chunk_id=str(which),
                max_attempts=2, retry_base_seconds=0.01)
        results = await ordered_tasks(task, [0, 1], limit=1)
        return results, current_runtime().metrics
    results, metrics = scenario(max_workers=1)
    assert results == [{'ok': True}, {'ok': True}]
    assert calls == [0, 1, 0]
    assert metrics['peak_active'] == 1
    assert metrics['retry_delays'] == 1


@pytest.mark.parametrize('best_effort,status', [(True, 'partial'), (False, 'failed')])
def test_call_budget_is_shared_by_generation_and_review(monkeypatch, best_effort, status):
    monkeypatch.setenv('KG_EXTRACT_V31_MAX_CALLS', '1')
    llm = StubLLM(entity_handler=lambda p, c: [{**span_item('Person', '甲', '甲负责巡检。'),
                                               'properties': {'duty': '巡检'}}])
    result = extract_split(ONTOLOGY, [ExtractionChunk('c', '甲负责巡检。')], llm=llm,
                           skill_refs=[], best_effort=best_effort, max_attempts=3, retry_base_seconds=0)
    assert result['status'] == status
    assert result['error_code'] == 'LLM_CALL_BUDGET_EXCEEDED'
    assert len(llm.calls) == 1
    assert result['raw']['entities'][0]['properties'] == {}
    assert any(a['type'] == 'call_budget' and a['terminal'] for a in result['attempts'])


def reviews(source_ids):
    plan = compile_plan(ONTOLOGY)
    llm = StubLLM(verification_handler=lambda item, chunk: 'yes')
    @document_tasks
    def request():
        for source_id in source_ids:
            row = {'type': 'Person', 'name': '甲', 'properties': {'duty': '巡检'},
                   'evidence': {'quote': '甲负责巡检。'}}
            result = ground_properties([row], [ExtractionChunk(source_id, '甲负责巡检。')], llm,
                        attempts=[], warnings=[], plan=plan)
            assert result[0]['property_evidence']['duty']['sources'][0]['source_block_ids'] == [source_id]
    request()
    return llm


def test_exact_review_is_reused_only_inside_one_request():
    assert len(reviews(['a', 'a']).calls) == 1
    assert len(reviews(['a', 'b']).calls) == 2
    # New requests execute their own review even when source text and keys agree.
    assert len(reviews(['a']).calls) == len(reviews(['a']).calls) == 1


def test_all_verified_fields_do_not_trigger_neighbour_rescan():
    text = '甲负责巡检。'
    llm = StubLLM(entity_handler=lambda p, c: [{**span_item('Person', '甲', text),
             'properties': {'duty': '巡检'}}] if c['text'] == text else [],
             verification_handler=lambda item, chunk: 'yes')
    result = extract_split(ONTOLOGY, [ExtractionChunk('a', text), ExtractionChunk('b', '其他说明。')],
                           llm=llm, skill_refs=[], max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert result['raw']['entities'][0]['properties'] == {'duty': '巡检'}
    assert not any(stage == 'property' for stage, _ in llm.calls)


def test_nested_review_batches_share_the_generation_worker_limit():
    barrier = threading.Barrier(2, timeout=2)
    lock = threading.Lock()
    seen = 0
    active = 0
    peak = 0
    def review(item, chunk):
        nonlocal active, peak, seen
        with lock:
            active += 1
            peak = max(peak, active)
            seen += 1
            first = seen <= 2
        try:
            if first:
                barrier.wait()
            return 'yes'
        finally:
            with lock:
                active -= 1
    text = ''.join(f'人员{i:02}负责巡检。' for i in range(30))
    llm = StubLLM(entity_handler=lambda p, c: [{**span_item('Person', f'人员{i:02}', f'人员{i:02}负责巡检。'),
                  'properties': {'duty': '巡检'}} for i in range(30)], verification_handler=review)
    result = extract_split(ONTOLOGY, [ExtractionChunk('a', text)], llm=llm, max_workers=2,
                           skill_refs=[], max_attempts=1, retry_base_seconds=0)
    assert result['status'] == 'succeeded'
    assert len(result['raw']['entities']) == 30
    assert peak == 2


def test_sync_entry_works_inside_existing_event_loop():
    async def scenario():
        return extract_split(ONTOLOGY, [ExtractionChunk('a', '甲负责巡检。')],
                             llm=StubLLM(entity_handler=lambda p, c: [span_item('Person', '甲')]),
                             skill_refs=[], max_attempts=1, retry_base_seconds=0)
    assert asyncio.run(scenario())['status'] == 'succeeded'


@pytest.mark.parametrize('out_of_scope', [False, True])
def test_conflict_gets_one_targeted_repair_and_must_stay_in_scope(out_of_scope):
    chunks = [ExtractionChunk('a', '甲原来负责巡检。'), ExtractionChunk('b', '甲最终负责审批。')]
    rows = [(c.chunk_id, [{'type': 'Person', 'name': '甲', 'properties': {'duty': duty},
                         'evidence': {'quote': c.text}, '_properties_scanned': True}])
            for c, duty in zip(chunks, ['巡检', '审批'])]
    targeted = []
    def llm(messages):
        user = json.loads(messages[1]['content'])
        task = user['task']
        if task['operation'] == 'evidence_verification':
            return '\n'.join(json.dumps({'item_id': item['item_id'], 'supported': 'yes', 'reason': '原文支持'})
                             for item in task['items'])
        known = task['known_entities'][0]
        props = {}
        if task['operation'] == 'repair':
            targeted.append(user)
            assert known['requested_properties'] == ['duty']
            props = {'unknown': '审批'} if out_of_scope else {'duty': '审批'}
        return json.dumps({'type': known['type'], 'name': known['name'], 'properties': props,
                           'evidence': {'quote': chunks[1].text}}, ensure_ascii=False)
    plan = compile_plan(ONTOLOGY)
    attempts, warnings = [], []
    result, stats = enrich_entity_properties(rows, ChunkContextManager(chunks), plan, llm,
        route=resolve_document_route(plan, chunks), attempts=attempts, warnings=warnings,
        max_attempts=1, retry_base_seconds=0, local_previous=0, local_next=0, max_workers=2)
    assert len(targeted) == 1
    assert all(c.text in targeted[0]['documents'] for c in chunks)
    if out_of_scope:
        assert result[0]['properties'] == {}
        assert any(a['terminal'] for a in attempts)
        assert any(w['code'] == 'PROPERTY_CONFLICT_UNRESOLVED' for w in warnings)
    else:
        assert result[0]['properties'] == {'duty': '审批'}
        assert result[0]['property_evidence']['duty']['support'] == 'llm'
        assert not any(a['terminal'] for a in attempts)
