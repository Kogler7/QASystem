"""V3.1 entrypoint stays independent of optional decision-model settings."""
from unittest.mock import Mock

from algorithm.knowledge_extract.v3_1 import extract_aggregate
from algorithm.knowledge_extract.v3_1 import harness_extractor


ONTOLOGY = '@startuml\nclass Person {\n name: String\n}\n@enduml'


def test_legacy_decision_settings_do_not_load_model(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_JEV_ENABLED", "1")
    monkeypatch.setenv("KG_EXTRACT_JEV_MODEL_PATH", "/nonexistent/decision-model")
    monkeypatch.setenv("KG_EXTRACT_JEV_ACCEPT", "invalid-unused-setting")
    llm = Mock(model="test-model")

    result = extract_aggregate(ONTOLOGY, [], llm=llm, use_scheduler=False)

    assert result["raw"] == {"entities": [], "relations": []}
    assert llm.mock_calls == []


def test_explicit_entity_adjudicator_is_forwarded(monkeypatch):
    adjudicator = Mock(return_value="uncertain")
    split = Mock(return_value={"sentinel": True})
    monkeypatch.setattr(harness_extractor, "extract_split", split)

    result = harness_extractor.extract_harness(
        ONTOLOGY, [], llm=Mock(model="test-model"), entity_adjudicator=adjudicator,
    )

    assert result == {"sentinel": True}
    assert split.call_args.kwargs["entity_adjudicator"] is adjudicator
