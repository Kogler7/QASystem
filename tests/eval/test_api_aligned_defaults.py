"""Default evaluations must exercise the same requests as API clients."""
import json
import sys
from unittest.mock import Mock

import pytest

from eval import run


@pytest.mark.parametrize("quality_status", ["succeeded", "partial", "failed"])
def test_default_cli_submits_each_file_once_without_application_overrides(
    tmp_path, monkeypatch, quality_status,
):
    workspace = tmp_path / "workspace"
    case = workspace / "g/c1"
    (case / "docs").mkdir(parents=True)
    for name in ("a.txt", "b.txt"):
        (case / "docs" / name).write_text(name, encoding="utf-8")
    ontology = workspace / "g/ontology"
    ontology.mkdir()
    (ontology / "onto.puml").write_text("@startuml\nentity T\n@enduml", encoding="utf-8")
    graph = {"nodes": [{"type": "T", "name": "n1"}], "relationships": []}
    (case / "extract").mkdir()
    (case / "extract/golden.json").write_text(json.dumps(graph), encoding="utf-8")
    requests = []

    def submit(url, **kwargs):
        requests.append((url, kwargs["data"]))
        return {"data": {"extraction": {
            "graph": graph,
            "quality": {"status": quality_status, "extract_version": "v3"},
        }}}

    monkeypatch.setattr(run, "submit_and_wait", submit)
    monkeypatch.setattr(run, "parse_document", Mock(side_effect=AssertionError("No separate parse")))
    monkeypatch.setattr(run, "chunk_document", Mock(side_effect=AssertionError("No separate chunk")))
    monkeypatch.setattr(run, "extract_graphs", Mock(side_effect=AssertionError("No separate extraction")))
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(sys, "argv", ["run.py", "--workspace", str(workspace),
                                     "--groups", "g", "--match-mode", "identity"])
    assert run.main() == (0 if quality_status == "succeeded" else 2)
    assert len(requests) == 2
    assert all(url.endswith(run.INGEST_PATH) for url, _ in requests)
    assert all(b'name="options"\r\n\r\n{}\r\n' in body for _, body in requests)
    directory = next((tmp_path / "runs").iterdir())
    resolved = json.loads((directory / "params.resolved.json").read_text())
    assert resolved["options"] == {}
    assert resolved["modes"] == ["pipeline"]
    assert resolved["case_attempts"] == resolved["request_attempts"] == 1
    record = json.loads((directory / "summary.json").read_text())["cases"][0]
    assert record["attempts"] == 1
    system = record["backend"]["system"]
    assert system["extract_version"] == "v3"
    assert system["status"] == quality_status
    assert [item["status"] for item in system["per_file"]] == [quality_status] * 2
    assert "elapsed_seconds" in system and "score_elapsed_seconds" in system


def test_default_options_remain_empty_and_quality_scoring_is_independent():
    args = run.parse_args([])
    params = run.params_from_args(run.parse_args([]))
    assert run.extraction_overrides(args, params) == {}
    assert params["eval"]["match_mode"] == "llm"
    assert params["eval"]["judge_model"] == "deepseek-flash"


def test_default_judge_uses_api_credentials_independent_of_extraction(monkeypatch):
    for key in ("QASYSTEM_JUDGE_MODEL", "QASYSTEM_JUDGE_BASE_URL", "QASYSTEM_JUDGE_ADAPTER",
                "QASYSTEM_JUDGE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "judge-fixture")
    monkeypatch.setenv("KG_EXTRACT_LLM_API_KEY", "different-extraction-fixture")
    monkeypatch.setenv("KG_EXTRACT_LLM_MODEL", "local-4b")
    client = run.build_judge_client(run.params_from_args(run.parse_args([])))
    assert client.model == "deepseek-flash"
    assert client.base_url == "https://api.deepseek.com/v1"
    assert client.adapter == "chat"
    assert client.api_key == "judge-fixture"
    clef = run.build_judge_client({"_judge_overrides": {"model": "clef-flash"}})
    assert clef.adapter == "systemone"


def test_missing_default_api_credential_fails_without_using_extraction_key(monkeypatch):
    for key in ("QASYSTEM_JUDGE_MODEL", "QASYSTEM_JUDGE_BASE_URL", "QASYSTEM_JUDGE_ADAPTER",
                "QASYSTEM_JUDGE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KG_EXTRACT_LLM_API_KEY", "different-provider-fixture")
    with pytest.raises(RuntimeError, match="DeepSeek API judge 未配置凭据"):
        run.build_judge_client({})


def test_default_ingestion_failure_is_not_submitted_again(tmp_path, monkeypatch):
    source = tmp_path / "a.txt"
    source.write_text("a")
    ingest = Mock(side_effect=RuntimeError("backend failed"))
    monkeypatch.setattr(run, "ingest_file", ingest)
    block = run._run_system("http://backend", [source], "ontology", {}, {}, None, tmp_path, None)
    ingest.assert_called_once()
    assert block["status"] == "failed"
    assert block["per_file"][0]["attempts"] == 1


def test_explicit_pipeline_retry_does_not_reuse_partial_graph(tmp_path, monkeypatch):
    source = tmp_path / "a.txt"
    source.write_text("a")
    graph = {"nodes": [{"type": "T", "name": "n1"}], "relationships": []}
    envelopes = [{"data": {"extraction": {"graph": graph, "quality": {"status": status}}}}
                 for status in ("partial", "succeeded")]
    ingest = Mock(side_effect=envelopes)
    monkeypatch.setattr(run, "ingest_file", ingest)
    monkeypatch.setattr(run, "RETRY_SLEEP_SECONDS", 0)
    backend, _, _, attempts, error = run.run_case_with_retries(
        label="case", run_backend=True, run_plugin=False, base_url="http://backend", mcp_url="",
        ontology="ontology", sources=[source], options={}, params={}, golden=None,
        out_dir=tmp_path, score_ctx=None, modes=["pipeline"], reuse_dir=None, max_attempts=3,
    )
    assert ingest.call_count == attempts == 2
    assert error is None
    assert backend["system"]["status"] == "succeeded"
