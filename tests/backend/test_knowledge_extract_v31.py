"""V3.1 keeps the public API, version routing and per-source failure boundary."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from algorithm.knowledge_extract import v3_1
from algorithm.knowledge_extract.v2.chunk_context import ExtractionChunk
from algorithm.knowledge_extract.v2.graph_assembler import GraphAssembler
from algorithm.knowledge_extract.v3_1.decisions import ClefDecider
from backend.app.services import knowledge_extract_adapter as adapter


ONTOLOGY = """@startuml
class Assignment
@enduml"""


class SupportingClef:
    def systemone(self, request, *, timeout=None):
        return {"answers": {
            key: {"type": "choice", "choice": "supported", "probabilities": {
                choice: 0.99 if choice == "supported" else 0.005
                for choice in question["criteria"]
            }} for key, question in request["questions"].items()
        }}


def _result(*, status="succeeded", failed_chunk=None):
    """An accepted semantic name is not required to appear verbatim in a chunk."""
    objects = [{
        "id": "accepted-1", "type": "Assignment", "identity": "夜间巡检职责",
        "display_name": "夜间巡检职责", "properties": {},
        "evidence": [{"source_block_ids": ["chunk-0000"], "quote": "甲组每晚检查机房。"}],
    }]
    assembler = GraphAssembler()
    result = {
        "status": status,
        "error_code": "LLM_TIMEOUT" if failed_chunk else None,
        "_error": "one source did not complete" if failed_chunk else None,
        "graph": assembler.build_graph(objects, []),
        "raw": assembler.compat_raw(objects, []),
        "intermediate_model": {"objects": objects, "relations": []},
        "verification": {"valid": status == "succeeded"},
        "attempts": [], "warnings": [], "profile": {},
    }
    if failed_chunk:
        result["attempts"] = [{
            "attempt": 1, "type": "timeout", "pass": "entity",
            "chunk": failed_chunk, "terminal": True, "message": "request timed out",
        }]
    return result


@pytest.mark.parametrize("entrypoint", ["chunks", "single_document"])
def test_v31_backend_selects_own_aggregate_and_v2_input(monkeypatch, entrypoint):
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "v3.1")
    captured = []

    def own_runner(**kwargs):
        captured.append(kwargs)
        assert all(type(chunk) is ExtractionChunk for chunk in kwargs["chunks"])
        return _result()

    def unrelated_runner(**_kwargs):
        raise AssertionError("V3.1 selected an unrelated extraction implementation")

    monkeypatch.setattr(v3_1, "extract_aggregate", own_runner)
    for package in (adapter.v2, adapter.v3, adapter.latest):
        monkeypatch.setattr(package, "extract_aggregate", unrelated_runner)

    text = "甲组每晚检查机房。"
    if entrypoint == "chunks":
        result = adapter.extract_chunks(
            ONTOLOGY, [text], ranges=[[40, 49]], metadata=[{"document_id": "source-A"}],
        )[0]
        assert result["error"] is None
        assert captured[0]["chunks"][0].start_offset == 40
        assert captured[0]["chunks"][0].document_id == "source-A"
        assert result["graph"]["nodes"][0]["chunk_indices"] == [0]
    else:
        result = adapter.run_single_document(ONTOLOGY, text)
        assert "_error" not in result
    assert len(captured) == 1
    assert captured[0]["chunks"][0].text == text
    assert result["quality"]["extract_version"] == "v3.1"
    assert result["quality"]["status"] == "succeeded"
    assert result["graph"]["nodes"][0]["properties"]["name"] == "夜间巡检职责"


def test_v31_partial_attempt_keeps_its_original_source_chunk(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "v3.1")
    result = _result(status="partial", failed_chunk="chunk-0001::decision-window-2")
    result["profile"]["chunk_source_ids"] = {
        "chunk-0001::decision-window-2": "chunk-0001",
    }
    monkeypatch.setattr(v3_1, "extract_aggregate", lambda **_kwargs: deepcopy(result))

    items = adapter.extract_chunks(ONTOLOGY, ["甲组每晚检查机房。", "乙组每早检查泵房。"])

    assert len(items) == 2
    assert items[0]["error"] is None
    assert len(items[0]["graph"]["nodes"]) == 1
    assert items[0]["attempts"] == []
    assert items[1]["error_code"] == "LLM_TIMEOUT"
    assert items[1]["graph"] == {"nodes": [], "relationships": []}
    assert items[1]["attempts"][0]["chunk"] == "chunk-0001::decision-window-2"
    assert all(item["quality"]["status"] == "partial" for item in items)
    assert all(item["quality"]["extract_version"] == "v3.1" for item in items)


def test_v31_recovered_provider_failure_remains_diagnostic_without_failing_result(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "v3.1")
    result = _result()
    result["attempts"] = [
        {"attempt": 1, "type": "request", "pass": "entity", "chunk": "chunk-0000",
         "terminal": False, "message": "Clef unavailable; semantic fallback completed"},
        {"attempt": 2, "type": "success", "pass": "entity", "chunk": "chunk-0000",
         "terminal": False, "response_status": "completed"},
    ]
    monkeypatch.setattr(v3_1, "extract_aggregate", lambda **_kwargs: deepcopy(result))

    item = adapter.extract_chunks(ONTOLOGY, ["甲组每晚检查机房。"])[0]

    assert item["error"] is None and item["error_code"] is None
    assert item["quality"]["status"] == "succeeded"
    assert item["quality"]["error_code"] is None
    assert item["quality"]["graph_publishable"] is True
    assert [attempt["type"] for attempt in item["attempts"]] == ["request", "success"]
    assert len(item["graph"]["nodes"]) == 1


def test_v31_real_harness_retains_healthy_chunk_when_another_source_cannot_complete(monkeypatch):
    """Exercise generation, decisions, assembly and Backend projection together."""
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "v3.1")
    texts = ["甲组每晚检查机房。", "乙组每早检查泵房。"]

    class Generation:
        def generate_structured(self, *, schema, messages, run_context=None):
            task = json.loads(messages[-1]["content"])["task"]
            assert run_context["response_mode"] == "plain"
            if task == "entities":
                if run_context["chunk_id"] == "chunk-0001":
                    raise TimeoutError("second original source timed out")
                return {"entities": [{
                    "candidate_id": "duty", "type": "Assignment", "name": "夜间巡检职责",
                    "properties": {}, "evidence": [{"chunk_id": "chunk-0000", "quote": texts[0]}],
                }]}
            return {"entities": [], "updates": [], "relations": [], "decisions": [],
                    "alias_pairs": [], "issues": [], "resolved_issue_ids": [], "context_requests": []}

    real_runner = v3_1.extract_aggregate
    monkeypatch.setattr(v3_1, "extract_aggregate", lambda **kwargs: real_runner(
        **kwargs, llm=Generation(), decider=ClefDecider(SupportingClef()), use_scheduler=False,
    ))

    items = adapter.extract_chunks(ONTOLOGY, texts)

    assert len(items) == 2
    assert items[0]["error"] is None
    assert items[0]["graph"]["nodes"][0]["properties"]["name"] == "夜间巡检职责"
    assert items[0]["graph"]["nodes"][0]["chunk_indices"] == [0]
    assert items[1]["error"]
    assert items[1]["error_code"]
    assert items[1]["graph"] == {"nodes": [], "relationships": []}
    assert all(item["quality"]["status"] == "partial" for item in items)


def test_v31_real_relation_projects_by_its_evidence_without_relabeling_endpoint_provenance(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "v3.1")
    ontology = "@startuml\nclass Team\nclass Work\nTeam --> Work : performs\n@enduml"
    texts = ["甲组已经设立。", "甲组承担巡检。"]

    class Generation:
        def generate_structured(self, *, schema, messages, run_context=None):
            request = json.loads(messages[-1]["content"])
            stage, payload = request["task"], request["input"]
            index = int(run_context["chunk_id"].split("-")[-1])
            if stage == "entities":
                kind, name = ("Team", "甲组") if index == 0 else ("Work", "巡检")
                return {"entities": [{"candidate_id": "local", "type": kind, "name": name,
                                      "properties": {}, "evidence": [{
                                          "chunk_id": run_context["chunk_id"], "quote": texts[index],
                                      }]}]}
            if stage == "relations" and index == 1:
                ids = {entity["type"]: entity["candidate_id"] for entity in payload["entities"]}
                return {"relations": [{"from_id": ids["Team"], "rel_type": "performs",
                                        "to_id": ids["Work"], "evidence": [{
                                            "chunk_id": "chunk-0001", "quote": texts[1],
                                        }]}]}
            return {"entities": [], "updates": [], "relations": [], "issues": []}

    real_runner = v3_1.extract_aggregate
    monkeypatch.setattr(v3_1, "extract_aggregate", lambda **kwargs: real_runner(
        **kwargs, llm=Generation(), decider=ClefDecider(SupportingClef()), use_scheduler=False,
    ))

    items = adapter.extract_chunks(ontology, texts)

    assert all(item["error"] is None for item in items)
    assert all(item["quality"]["status"] == "succeeded" for item in items)
    assert items[0]["graph"]["relationships"] == []
    relationship = items[1]["graph"]["relationships"][0]
    assert relationship["chunk_indices"] == [1]
    assert relationship["provenance_status"] == "evidence"
    nodes = {node["label"]: node for node in items[1]["graph"]["nodes"]}
    assert nodes["Team"]["chunk_indices"] == [0]
    assert nodes["Work"]["chunk_indices"] == [1]
    assert {relationship["from_uid"], relationship["to_uid"]} == {node["uid"] for node in nodes.values()}
