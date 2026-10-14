"""Coverage and verdict checks for the routing-only benchmark diagnostic."""
from copy import deepcopy

from eval.tools import eval_skill_routing as routing
from algorithm.knowledge_extract.v3_1.tests.stub_llm import StubLLM


def test_every_effective_benchmark_document_has_an_individual_request():
    requests = routing.discover_requests()
    actual = {(r["case"], r["document"]) for r in requests if r["scope"] == "document"}
    expected = set()
    for domain in routing.discover_types(routing.WORKSPACE):
        for case in routing.discover_cases(domain):
            expected.update((f"{domain.name}/{case.name}", s.name)
                            for s in routing.collect_source_files(case / "docs"))
    assert actual == expected
    assert len(actual) == sum(r["scope"] == "document" for r in requests)
    assert all(r["chunks"] and r["ontology"] for r in requests)
    assert not any(name.startswith("~$") for _, name in actual)


def test_expected_labels_and_source_paths_do_not_enter_selection_prompt(monkeypatch):
    request = next(r for r in routing.discover_requests() if r["case"] == "rule/rule_01")
    def select(user):
        assert "expected" not in user
        assert "case" not in user["task"]
        assert all(request[key] not in str(user) for key in ("case", "chunks_path"))
        refs = [s["skill_ref"] for s in user["task"]["skills"] if s["name"] == "rule"]
        return {"status": "selected", "skill_refs": refs, "reason": "本体中的条件到结论规则"}
    monkeypatch.setattr(routing, "create_llm_client", lambda **_: StubLLM(selection_handler=select))
    result = routing.evaluate(request, routing.SkillRegistry(), 1)
    assert result["matched"]
    assert all(result["stage_checks"].values())
    assert result["document_route"]["kind"] == "generic"
    # Changing evaluator expectation changes only the verdict, never the model inputs.
    changed = deepcopy(request)
    changed["expected"] = {"status": "selected", "skills": ["trouble"]}
    other = routing.evaluate(changed, routing.SkillRegistry(), 1)
    assert not other["matched"]
    assert other["actual_skills"] == ["rule"]


def test_dry_run_does_not_claim_accuracy_or_quality_gate():
    result = routing.summary([], routing.discover_requests(), executed=False)
    assert result["passed"] is None
    assert all(scope["accuracy"] is None for scope in result["scopes"].values())
    assert result["extraction_quality_gate"] is None
    assert not result["http_pipeline_verified"]


def test_failed_model_call_is_counted_without_being_a_valid_routing_decision():
    request = routing.discover_requests()[0]
    failed = {"id": request["id"], "repeat": 1, "scope": request["scope"],
        "case": request["case"], "document": request["document"], "actual_skills": [],
        "expected": request["expected"], "matched": False, "selection": {"status": "failed"}}
    result = routing.summary([failed], [request], executed=True)
    assert result["scopes"]["case"]["valid_decisions"] == 0
    assert result["scopes"]["case"]["runs"] == 1
    assert not result["passed"]


def test_partial_repeats_cannot_claim_batch_passed():
    request = routing.discover_requests()[0]
    result = {"scope": request["scope"], "matched": True, "selection": {"status": "selected"}}
    stats = routing.summary([result], [request], executed=True, repeats=3)
    assert stats["planned_runs"] == 3
    assert not stats["complete"]
    assert stats["passed"] is None
