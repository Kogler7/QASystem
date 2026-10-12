import pytest

from eval import run
from eval.configure import Configuration


def test_runtime_values_are_only_current_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("QASYSTEM_BACKEND_URL", "http://obsolete")
    args = run.parse_args(["--backend-url", "http://current:8000", "--judge-timeout-seconds", "12",
                           "--judge-max-tokens", "1024", "--judge-batch-size", "3", "--judge-golden-batch-size", "7",
                           "--judge-max-concurrency", "2", "--no-judge-disable-thinking", "--poll-timeout-seconds", "0",
                           "--adaptive-entity-count", "--entity-count-model", "nanojev", "--chunk-size", "500", "--chunk-overlap", "20"])
    params = run.params_from_args(args)
    assert params["backend_url"] == "http://current:8000"
    assert params["poll_timeout_seconds"] == 0
    assert params["eval"]["judge_batch_size"] == 3
    assert params["eval"]["judge_golden_batch_size"] == 7
    assert params["eval"]["judge_disable_thinking"] is False
    assert params["_judge_overrides"]["max_tokens"] == 1024
    assert params["options"]["chunking"] == {"adaptive_entity_count": True, "entity_count_model": "nanojev", "size": 500, "chunk_overlap": 20}
    fresh = run.params_from_args(run.parse_args([]))
    assert fresh["options"] == {}
    assert fresh["eval"]["judge_batch_size"] == 20


@pytest.mark.parametrize("flags", [["--judge-max-tokens", "0"], ["--judge-batch-size", "0"],
                                    ["--poll-timeout-seconds", "-1"], ["--poll-interval-seconds", "nan"],
                                    ["--chunk-size", "20", "--chunk-overlap", "30"]])
def test_bad_runtime_values_fail_before_execution(flags):
    with pytest.raises(ValueError): run.params_from_args(run.parse_args(flags))


def test_runtime_cli_round_trip_has_no_files():
    params = run.params_from_args(run.parse_args(["--judge-max-tokens", "2048", "--no-persist-graph", "--adaptive-entity-count"]))
    forwarded = run.runtime_cli_args(params)
    assert "--params" not in forwarded
    actual = run.params_from_args(run.parse_args(forwarded))
    assert actual["options"] == params["options"]
    assert actual["eval"] == params["eval"]


def test_judge_explicit_cli_tokens_win_over_hot(monkeypatch):
    monkeypatch.setattr("docker.runtime_config.read_settings", lambda: {"QASYSTEM_JUDGE_MAX_TOKENS": "9999"})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    params = run.params_from_args(run.parse_args(["--judge-max-tokens", "2048", "--judge-timeout-seconds", "90"]))
    client = run.build_judge_client(params)
    assert client.max_tokens == 2048
    assert client.timeout == 90


def test_tui_every_session_is_blank():
    one = Configuration()
    one.cli["judge_max_tokens"] = 1234
    two = Configuration()
    assert two.cli == {}
    assert "--params" not in one.arguments()
    assert not hasattr(one, "profile")
    assert not hasattr(one, "save")
