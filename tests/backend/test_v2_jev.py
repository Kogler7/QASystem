from types import SimpleNamespace

import pytest

from algorithm.knowledge_extract.v2.candidate_generation import (
    _deterministic_relation_quote,
    relation_pass,
)
from algorithm.knowledge_extract.v2.candidate_models import EntityCandidate
from algorithm.knowledge_extract.v2.chunk_context import (
    ChunkContextManager,
    ExtractionChunk,
    ExtractionRequest,
)
from algorithm.knowledge_extract.v2.evidence import Evidence
from algorithm.knowledge_extract.v2.jev_client import (
    CallableJevClient,
    ChoiceAnswer,
    JevUnavailableError,
    create_jev_client,
    default_entity_adjudicator,
    get_jev_config,
    jev_enabled,
    partition_relation_pairs,
    reset_jev_client_cache,
)
from algorithm.knowledge_extract.v2.ontology_compiler import compile_plan
from algorithm.knowledge_extract.v2.prompt_router import DocumentRoute


ONTOLOGY = """
@startuml
class Term
class Domain
Term --> Domain : BELONGS_TO
@enduml
"""


def _chunk() -> ExtractionChunk:
    return ExtractionChunk(
        chunk_id="chunk-0",
        text="知识地图属于知识管理领域。",
    )


def test_jev_disabled_by_default(monkeypatch):
    monkeypatch.delenv("KG_EXTRACT_JEV_ENABLED", raising=False)
    reset_jev_client_cache()
    assert jev_enabled() is False
    assert get_jev_config()["enabled"] is False


def test_jev_enabled_without_model_fails_explicitly(monkeypatch, tmp_path):
    monkeypatch.setenv("KG_EXTRACT_JEV_ENABLED", "1")
    monkeypatch.setenv("KG_EXTRACT_JEV_MODEL_PATH", str(tmp_path / "missing-jev"))
    monkeypatch.setenv("ALGORITHM_RUNTIME_PROFILE", "full-runtime")
    reset_jev_client_cache()
    with pytest.raises(JevUnavailableError, match="模型不存在"):
        create_jev_client()


def test_entity_adjudicator_keeps_uncertain_below_confidence(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_JEV_CHOICE_CONFIDENCE", "0.9")
    client = CallableJevClient(
        choice_fn=lambda *_args: ChoiceAnswer(
            choice="same_entity",
            probabilities={"same_entity": 0.4, "different_entity": 0.3, "uncertain": 0.3},
            confidence=0.4,
        )
    )
    monkeypatch.setattr(
        "algorithm.knowledge_extract.v2.jev_client.get_jev_client",
        lambda: client,
    )
    verdict = default_entity_adjudicator(
        {
            "type": "Term",
            "left": {"name": "知识地图"},
            "right": {"name": "知识图谱"},
            "allowed_verdicts": ["same_entity", "different_entity", "uncertain"],
        }
    )
    assert verdict == "uncertain"


def test_entity_adjudicator_accepts_high_confidence_same_entity(monkeypatch):
    client = CallableJevClient(
        choice_fn=lambda *_args: ChoiceAnswer(
            choice="same_entity",
            probabilities={"same_entity": 0.97, "different_entity": 0.02, "uncertain": 0.01},
            confidence=0.97,
        )
    )
    monkeypatch.setattr(
        "algorithm.knowledge_extract.v2.jev_client.get_jev_client",
        lambda: client,
    )
    verdict = default_entity_adjudicator(
        {
            "type": "Term",
            "left": {"name": "知识地图"},
            "right": {"name": "知识地图（KM）"},
            "allowed_verdicts": ["same_entity", "different_entity", "uncertain"],
        }
    )
    assert verdict == "same_entity"


def test_partition_relation_pairs_routes_gray_zone_to_llm():
    pairs = [
        {"pair_id": "pair-reject", "relation_id": "belongs_to", "source_name": "A", "target_name": "B"},
        {"pair_id": "pair-accept", "relation_id": "belongs_to", "source_name": "知识地图", "target_name": "知识管理"},
        {"pair_id": "pair-gray", "relation_id": "belongs_to", "source_name": "C", "target_name": "D"},
    ]
    scores = {"pair-reject": 0.02, "pair-accept": 0.96, "pair-gray": 0.4}

    def noul_fn(state: str, _question: str) -> float:
        if "source: 知识地图" in state:
            return scores["pair-accept"]
        if "source: A" in state:
            return scores["pair-reject"]
        return scores["pair-gray"]

    client = CallableJevClient(noul_fn=noul_fn)
    accepted, rejected, gray = partition_relation_pairs(pairs, chunk_text="知识地图属于知识管理。", client=client)
    assert [item["pair_id"] for item in rejected] == ["pair-reject"]
    assert [item["pair_id"] for item in accepted] == ["pair-accept"]
    assert [item["pair_id"] for item in gray] == ["pair-gray"]


def test_deterministic_quote_covers_both_endpoint_mentions():
    chunk = _chunk()
    manager = ChunkContextManager([chunk])
    source = EntityCandidate(
        candidate_id="e-1",
        proposed_type="Term",
        canonical_type="Term",
        name="知识地图",
        properties={},
        evidence=[
            Evidence(
                span_id="chunk-0:0:4",
                source_block_ids=["chunk-0"],
                quote="知识地图",
                start_char=0,
                end_char=4,
            )
        ],
        source="entity",
    )
    target = EntityCandidate(
        candidate_id="e-2",
        proposed_type="Domain",
        canonical_type="Domain",
        name="知识管理",
        properties={},
        evidence=[
            Evidence(
                span_id="chunk-0:6:10",
                source_block_ids=["chunk-0"],
                quote="知识管理",
                start_char=6,
                end_char=10,
            )
        ],
        source="entity",
    )
    pool = SimpleNamespace(accepted_entities=lambda: [source, target])
    evidence = _deterministic_relation_quote(
        {
            "source_id": "e-1",
            "target_id": "e-2",
            "source_name": "知识地图",
            "target_name": "知识管理",
        },
        pool,
        manager,
        "chunk-0",
    )
    assert evidence is not None
    assert "知识地图" in evidence.quote
    assert "知识管理" in evidence.quote


def test_relation_pass_does_not_call_llm_for_high_confidence_none(monkeypatch):
    plan = compile_plan(ONTOLOGY)
    chunk = _chunk()
    manager = ChunkContextManager([chunk])
    request = ExtractionRequest(chunks=[chunk])
    source = EntityCandidate(
        candidate_id="e-1",
        proposed_type="Term",
        canonical_type="Term",
        name="知识地图",
        properties={},
        evidence=[
            Evidence(
                span_id="s",
                source_block_ids=["chunk-0"],
                quote="知识地图",
                start_char=0,
                end_char=4,
            )
        ],
        source="entity",
    )
    target = EntityCandidate(
        candidate_id="e-2",
        proposed_type="Domain",
        canonical_type="Domain",
        name="知识管理",
        properties={},
        evidence=[
            Evidence(
                span_id="t",
                source_block_ids=["chunk-0"],
                quote="知识管理",
                start_char=6,
                end_char=10,
            )
        ],
        source="entity",
    )
    pool = SimpleNamespace(
        accepted_entities=lambda: [source, target],
        entity_catalog=lambda: [
            {"id": "e-1", "type": "Term", "name": "知识地图", "source_block_ids": ["chunk-0"]},
            {"id": "e-2", "type": "Domain", "name": "知识管理", "source_block_ids": ["chunk-0"]},
        ],
    )
    monkeypatch.setattr(
        "algorithm.knowledge_extract.v2.candidate_generation.partition_relation_pairs",
        lambda pairs, chunk_text, client=None: ([], list(pairs), []),
    )
    calls: list[object] = []

    def fake_generate(*_args, **_kwargs):
        calls.append(_kwargs)
        return {"decisions": []}

    monkeypatch.setattr(
        "algorithm.knowledge_extract.v2.candidate_generation.generate_structured",
        fake_generate,
    )
    results = relation_pass(
        plan,
        manager,
        request,
        llm=object(),
        pool=pool,
        route=DocumentRoute(
            kind="generic",
            source="fallback",
            scores=(("official_document", 0), ("standard", 0)),
            reasons=("test",),
        ),
        max_workers=1,
        use_scheduler=False,
        attempts=[],
        warnings=[],
        local_previous=0,
        local_next=0,
        same_section=True,
    )
    assert calls == []
    assert results == [("chunk-0", [])]


def test_relation_pass_sends_gray_pairs_to_llm(monkeypatch):
    plan = compile_plan(ONTOLOGY)
    chunk = _chunk()
    manager = ChunkContextManager([chunk])
    request = ExtractionRequest(chunks=[chunk])
    source = EntityCandidate(
        candidate_id="e-1",
        proposed_type="Term",
        canonical_type="Term",
        name="知识地图",
        properties={},
        evidence=[
            Evidence(
                span_id="s",
                source_block_ids=["chunk-0"],
                quote="知识地图",
                start_char=0,
                end_char=4,
            )
        ],
        source="entity",
    )
    target = EntityCandidate(
        candidate_id="e-2",
        proposed_type="Domain",
        canonical_type="Domain",
        name="知识管理",
        properties={},
        evidence=[
            Evidence(
                span_id="t",
                source_block_ids=["chunk-0"],
                quote="知识管理",
                start_char=6,
                end_char=10,
            )
        ],
        source="entity",
    )
    pool = SimpleNamespace(
        accepted_entities=lambda: [source, target],
        entity_catalog=lambda: [
            {"id": "e-1", "type": "Term", "name": "知识地图", "source_block_ids": ["chunk-0"]},
            {"id": "e-2", "type": "Domain", "name": "知识管理", "source_block_ids": ["chunk-0"]},
        ],
    )
    gray_holder: dict[str, list] = {}

    def fake_partition(pairs, chunk_text, client=None):
        gray_holder["pairs"] = list(pairs)
        return [], [], list(pairs)

    monkeypatch.setattr(
        "algorithm.knowledge_extract.v2.candidate_generation.partition_relation_pairs",
        fake_partition,
    )
    calls: list[object] = []

    def fake_generate(_llm, _messages, _schema, **kwargs):
        calls.append(kwargs)
        return {"decisions": []}

    monkeypatch.setattr(
        "algorithm.knowledge_extract.v2.candidate_generation.generate_structured",
        fake_generate,
    )
    relation_pass(
        plan,
        manager,
        request,
        llm=object(),
        pool=pool,
        route=DocumentRoute(
            kind="generic",
            source="fallback",
            scores=(("official_document", 0), ("standard", 0)),
            reasons=("test",),
        ),
        max_workers=1,
        use_scheduler=False,
        attempts=[],
        warnings=[],
        local_previous=0,
        local_next=0,
        same_section=True,
    )
    assert calls
    assert gray_holder["pairs"]
