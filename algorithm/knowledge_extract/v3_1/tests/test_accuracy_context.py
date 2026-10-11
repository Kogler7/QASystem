"""Source/schema boundaries for the semantic accuracy review."""
from copy import deepcopy

from algorithm.knowledge_extract.v3_1 import ExtractionChunk, extract_aggregate
from algorithm.knowledge_extract.v3_1.tests.test_semantic_contracts import (
    ScriptedLLM, SemanticDecider, _entity, _empty, _relation,
)


def test_generation_gets_compiled_signatures_with_exact_compatible_ids():
    text = "The author wrote the document."
    ontology = "@startuml\nclass Author\nclass Document\nAuthor --> Document : wrote\n' Author -[:OWNS]-> Document (future feature)\n@enduml"
    observed = []

    def model(stage, payload, _context):
        observed.append((stage, deepcopy(payload)))
        if stage == "entities":
            return {"entities": [_entity("a", "Author", "author", text),
                                 _entity("d", "Document", "document", text)]}
        if stage == "relations":
            return {"relations": [_relation(payload, "author", "wrote", "document", text,
                                             fact="The author wrote this document.")]}
        return _empty(stage)

    inspected = []

    def decide(task):
        if task.kind == "relation_support":
            inspected.append(deepcopy(task.state))
        return "supported"

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)],
                               llm=ScriptedLLM(model), decider=SemanticDecider(decide),
                               use_scheduler=False, max_repair_rounds=0)
    assert result["status"] == "succeeded"
    payload = next(payload for stage, payload in observed if stage == "relations")
    signatures = payload["schema_context"]["relation_signatures"]
    assert len(signatures) == 1 and signatures[0]["rel_type"] == "wrote"
    ids = {obj["name"]: obj["candidate_id"] for obj in payload["entities"]}
    assert signatures[0]["source_candidate_ids"] == [ids["author"]]
    assert signatures[0]["target_candidate_ids"] == [ids["document"]]
    assert payload["ontology"] == ontology
    task = next(task for task in result["run_trace"]["tasks"] if task["kind"] == "relation_support")
    assert task["claim"]["fact"] == "The author wrote this document."
    assert task["evidence"][0]["quote"] == text
    assert any(any(claim.get("fact") == task["claim"]["fact"] for claim in state["claims"].values())
               for state in inspected)


def test_discovery_sees_contiguous_record_context_without_mixing_documents():
    ontology = "@startuml\nclass Record {\nname: String\ndetails: String\n}\n' @identity Record: name\n@enduml"
    chunks = [ExtractionChunk("a", "Record alpha: ", document_id="one"),
              ExtractionChunk("b", "description continues here.", document_id="one"),
              ExtractionChunk("c", "Other document.", document_id="two")]
    seen = {}

    def model(stage, payload, context):
        if stage == "entities":
            seen[context["chunk_id"]] = payload
        return _empty(stage)

    extract_aggregate(ontology, chunks, llm=ScriptedLLM(model), decider=SemanticDecider(),
                      use_scheduler=False, max_repair_rounds=0)
    assert [source["chunk_id"] for source in seen["a"]["sources"]] == ["a", "b"]
    assert seen["a"]["anchor_chunk_id"] == "a"
    assert {source["chunk_id"] for source in seen["b"]["sources"]} == {"a", "b"}
    assert [source["chunk_id"] for source in seen["c"]["sources"]] == ["c"]


def test_mapping_attribute_is_preserved_as_an_object_end_to_end():
    text = "Record alpha has dimensions region=east and period=monthly."
    ontology = "@startuml\nclass Record {\ncoordinates: Map<Dimension.Scale>\n}\n@enduml"

    def model(stage, payload, _context):
        if stage == "entities":
            return {"entities": [_entity("a", "Record", "alpha", text,
                properties={"coordinates": {"region": "east", "period": "monthly"}})]}
        return _empty(stage)

    result = extract_aggregate(ontology, [ExtractionChunk("source-A", text)],
                               llm=ScriptedLLM(model), decider=SemanticDecider(),
                               use_scheduler=False, max_repair_rounds=0)
    assert result["status"] == "succeeded"
    assert result["raw"]["entities"][0]["properties"]["coordinates"] == {"region": "east", "period": "monthly"}
