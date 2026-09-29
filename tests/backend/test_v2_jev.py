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
    JevUnavailableError,
    Judgment,
    answers_from_nanojev,
    create_jev_client,
    get_jev_config,
    jev_enabled,
    reset_jev_client_cache,
)
from algorithm.knowledge_extract.v2.jev_harness import (
    default_entity_adjudicator,
    partition_relation_pairs,
    relation_facts,
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


def test_incomplete_checkpoint_fails_without_touching_cuda(monkeypatch, tmp_path):
    monkeypatch.setenv("KG_EXTRACT_JEV_ENABLED", "1")
    monkeypatch.setenv("KG_EXTRACT_JEV_MODEL_PATH", str(tmp_path))
    monkeypatch.setenv("ALGORITHM_RUNTIME_PROFILE", "full-runtime")
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    reset_jev_client_cache()
    with pytest.raises(JevUnavailableError, match="不完整"):
        create_jev_client()


def test_remote_only_refuses_local_nanojev(monkeypatch, tmp_path):
    monkeypatch.setenv("KG_EXTRACT_JEV_ENABLED", "1")
    monkeypatch.setenv("KG_EXTRACT_JEV_MODEL_PATH", str(tmp_path))
    monkeypatch.setenv("ALGORITHM_RUNTIME_PROFILE", "remote-only")
    reset_jev_client_cache()
    with pytest.raises(JevUnavailableError, match="remote-only"):
        create_jev_client()


def test_entity_adjudicator_keeps_uncertain_below_confidence(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_JEV_CHOICE_CONFIDENCE", "0.9")

    def predict(_states):
        return {
            "entity": {
                "verdict": Judgment(
                    choice="same_entity",
                    probabilities={"same_entity": 0.4, "different_entity": 0.3, "uncertain": 0.3},
                    confidence=0.4,
                )
            }
        }

    monkeypatch.setattr(
        "algorithm.knowledge_extract.v2.jev_harness.get_jev_client",
        lambda: CallableJevClient(predict),
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
    def predict(states):
        assert "left_name: 知识地图" in states[0]["state"]
        assert states[0]["questions"]["verdict"]["type"] == "choice"
        return {
            "entity": {
                "verdict": Judgment(
                    choice="same_entity",
                    probabilities={"same_entity": 0.97, "different_entity": 0.02, "uncertain": 0.01},
                    confidence=0.97,
                )
            }
        }

    monkeypatch.setattr(
        "algorithm.knowledge_extract.v2.jev_harness.get_jev_client",
        lambda: CallableJevClient(predict),
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
    chunk = "知识地图属于知识管理。办公室不得授权外单位。"
    pairs = [
        {
            "pair_id": "pair-reject",
            "relation_id": "AUTHORIZES",
            "source_name": "办公室",
            "source_type": "Org",
            "target_name": "外单位",
            "target_type": "Org",
        },
        {
            "pair_id": "pair-accept",
            "relation_id": "BELONGS_TO",
            "source_name": "知识地图",
            "source_type": "Term",
            "target_name": "知识管理",
            "target_type": "Domain",
        },
        {
            "pair_id": "pair-gray",
            "relation_id": "BELONGS_TO",
            "source_name": "知识地图",
            "source_type": "Term",
            "target_name": "知识管理",
            "target_type": "Domain",
        },
        {
            "pair_id": "pair-missing",
            "relation_id": "BELONGS_TO",
            "source_name": "不存在甲",
            "source_type": "Term",
            "target_name": "不存在乙",
            "target_type": "Domain",
        },
    ]

    def predict(states):
        ids = {item["id"] for item in states}
        assert "pair-missing" not in ids
        assert "pair-accept" in ids and "pair-gray" in ids
        accept_state = next(item for item in states if item["id"] == "pair-accept")
        question = accept_state["questions"]["label"]
        assert question["type"] == "choice"
        assert set(question["criteria"]) == {"BELONGS_TO", "NONE"}
        assert "window: 知识地图属于知识管理" in accept_state["state"]
        answers = {}
        for state in states:
            relation_id = "BELONGS_TO" if state["id"] != "pair-reject" else "AUTHORIZES"
            if state["id"] == "pair-accept":
                probabilities = {relation_id: 0.96, "NONE": 0.04}
                choice = relation_id
            elif state["id"] == "pair-reject":
                probabilities = {relation_id: 0.03, "NONE": 0.97}
                choice = "NONE"
            else:
                probabilities = {relation_id: 0.55, "NONE": 0.45}
                choice = relation_id
            answers[state["id"]] = {
                "label": Judgment(
                    choice=choice,
                    probabilities=probabilities,
                    confidence=max(probabilities.values()),
                )
            }
        return answers

    client = CallableJevClient(predict)
    accepted, rejected, gray = partition_relation_pairs(pairs, chunk_text=chunk, client=client)
    assert [item["pair_id"] for item in rejected] == ["pair-reject"]
    assert [item["pair_id"] for item in accepted] == ["pair-accept"]
    assert [item["pair_id"] for item in gray] == ["pair-missing", "pair-gray"]
    missing = next(item for item in gray if item["pair_id"] == "pair-missing")
    assert missing["judgment"]["reason"] == "endpoints_not_in_chunk"


def test_relation_facts_mark_same_sentence_window():
    facts = relation_facts(
        "前文。术语“应答器”属于通信设备。后文。",
        {"source_name": "应答器", "target_name": "通信设备", "relation_id": "BELONGS_TO"},
    )
    assert facts["complete"] is True
    assert facts["same_sentence"] is True
    assert "应答器" in facts["window"] and "通信设备" in facts["window"]
    assert "前文" not in facts["window"]


def test_answers_from_nanojev_reads_choice_distribution():
    parsed = answers_from_nanojev(
        {
            "states": [
                {
                    "id": "pair-1",
                    "answers": {
                        "label": {
                            "type": "choice",
                            "choice": "NONE",
                            "probabilities": {"BELONGS_TO": 0.1, "NONE": 0.9},
                        }
                    },
                }
            ]
        }
    )
    judgment = parsed["pair-1"]["label"]
    assert judgment.choice == "NONE"
    assert judgment.confidence == 0.9


def test_disabled_partition_leaves_every_pair_for_llm(monkeypatch):
    monkeypatch.delenv("KG_EXTRACT_JEV_ENABLED", raising=False)
    reset_jev_client_cache()
    pairs = [{"pair_id": "pair-1", "relation_id": "BELONGS_TO", "source_name": "甲", "target_name": "乙"}]
    accepted, rejected, gray = partition_relation_pairs(pairs, chunk_text="甲属于乙。", client=None)
    assert accepted == []
    assert rejected == []
    assert gray == pairs


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
