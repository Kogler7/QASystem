"""Distinguish unstarted repair work from actual model output truncation."""
from types import SimpleNamespace

from algorithm.output_batching import generate_batched
from algorithm.knowledge_extract.v3.candidate_generation import _pre_parse_response_error
from algorithm.knowledge_extract.v3.run_state import ExtractionRun


def test_successful_responses_can_still_exhaust_initial_repair_batch_budget():
    attempts = []
    finishes = []

    def generate(llm, messages, schema, **kwargs):
        finishes.append("stop")
        return {"relations": [{"item": messages[0]["content"]}]}

    result = generate_batched(
        list(range(64)),
        lambda batch: ([{"role": "user", "content": batch[0]}], {}),
        generate,
        SimpleNamespace(),
        batch_size=1,
        attempts=attempts,
        chunk_id="repair-1",
        pass_name="repair_relation",
    )
    assert finishes == ["stop"] * 63
    assert len(result["relations"]) == 63
    assert attempts == [{
        "attempt": 1, "type": "incomplete", "pass": "repair_relation",
        "chunk": "repair-1.part-63", "terminal": True, "retryable": False,
        "message": "Output split call budget exhausted", "pending_items": 1,
    }]
    # This currently shares the error code with a genuine model length finish.
    run = ExtractionRun.create(run_id="budget-demo", ontology="", markdown="", model="test", prompt_version="test")
    run.absorb_attempts(attempts)
    assert run.primary_failure()[0] == "LLM_RESPONSE_INCOMPLETE"


def test_model_length_is_separately_observable_in_response_metadata():
    response = SimpleNamespace(text='{"entity_id":"entity-1"}', status="incomplete",
                               finish_reason="length", incomplete_details={})
    assert _pre_parse_response_error(response) == ("incomplete", "LLM 响应不完整: length")
    assert _pre_parse_response_error(SimpleNamespace(text=response.text, status="completed",
                                                   finish_reason="stop")) is None
