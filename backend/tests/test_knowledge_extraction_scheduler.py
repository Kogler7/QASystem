from __future__ import annotations

import threading
import time

from algorithm.knowledge_extract.batch import deduplicate_chunk_graphs, extract_chunks
from algorithm.knowledge_extract.scheduler import LLMRateLimits, LLMRequestScheduler


def test_extract_chunks_runs_concurrently_and_preserves_index_order():
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def extractor(*, text, **_kwargs):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.02 if text == "slow" else 0.005)
        with lock:
            active -= 1
        return {
            "raw": {"text": text},
            "graph": {
                "nodes": [{"uid": f"Term::{text}", "label": "Term", "properties": {"name": text}}],
                "relationships": [],
            },
            "attempts": [],
        }

    results = extract_chunks(
        "class Term",
        ["slow", "fast-1", "fast-2"],
        extractor=extractor,
        max_workers=3,
    )

    assert maximum_active > 1
    assert [item["index"] for item in results] == [0, 1, 2]
    assert [item["text"] for item in results] == ["slow", "fast-1", "fast-2"]


def test_deduplicate_chunk_graphs_deduplicates_nodes_and_relationships():
    relationship = {"from_uid": "Term::a", "to_uid": "Term::b", "type": "RELATED"}
    items = [
        {
            "graph": {
                "nodes": [{"uid": "Term::a", "label": "Term", "properties": {"name": "A"}}],
                "relationships": [relationship],
            }
        },
        {
            "graph": {
                "nodes": [
                    {"uid": "Term::a", "label": "Term", "properties": {"description": "merged"}},
                    {"uid": "Term::b", "label": "Term", "properties": {"name": "B"}},
                ],
                "relationships": [relationship],
            }
        },
    ]

    graph = deduplicate_chunk_graphs(items)

    assert len(graph["nodes"]) == 2
    assert len(graph["relationships"]) == 1
    assert graph["nodes"][0]["properties"] == {"name": "A", "description": "merged"}


def test_request_scheduler_limits_in_flight_requests():
    scheduler = LLMRequestScheduler(LLMRateLimits(max_concurrency=2))
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def request():
        nonlocal active, maximum_active
        with scheduler.request_slot():
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.01)
            with lock:
                active -= 1

    threads = [threading.Thread(target=request) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert maximum_active == 2


def test_request_scheduler_applies_rolling_qps_limit():
    scheduler = LLMRequestScheduler(LLMRateLimits(max_concurrency=3, qps=2))
    started: list[float] = []
    lock = threading.Lock()

    def request():
        with scheduler.request_slot():
            with lock:
                started.append(time.monotonic())

    threads = [threading.Thread(target=request) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    started.sort()
    assert len(started) == 3
    assert started[2] - started[0] >= 0.9
