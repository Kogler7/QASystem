import json
from unittest.mock import Mock

import pytest

from eval import run


def test_cli_overrides_yaml_without_changing_judge_or_source():
    params = {"options": {"extraction": {"model": "yaml-model", "version": "v2", "merge": False}},
              "eval": {"judge_model": "fixed-judge"}}
    args = run.parse_args(["--extract-version", "v3.1", "--extract-provider", "siliconflow",
                           "--extract-request-timeout", "300"])
    options = run.extraction_overrides(args, params)
    assert options["extraction"] == {"model": "yaml-model", "version": "v3.1", "merge": False,
                                      "provider": "siliconflow", "request_timeout": 300.0}
    assert params["options"]["extraction"]["version"] == "v2"
    assert params["eval"]["judge_model"] == "fixed-judge"


def test_absent_overrides_leave_backend_defaults():
    args = run.parse_args([])
    assert run.extraction_overrides(args, {}) == {"extraction": {}}


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_timeout_fails_before_http(value):
    args = run.parse_args(["--extract-request-timeout", value])
    with pytest.raises(ValueError):
        run.extraction_overrides(args, {})


@pytest.mark.parametrize("extra", [["--stage", "plugin"], ["--stage", "all"], ["--reuse-extract-from", "old"]])
def test_unsupported_path_never_silently_ignores_override(extra):
    args = run.parse_args(["--extract-version", "v3", *extra])
    with pytest.raises(ValueError):
        run.extraction_overrides(args, {})


def test_extract_http_payload_carries_overrides(monkeypatch):
    submit = Mock(return_value={})
    monkeypatch.setattr(run, "submit_and_wait", submit)
    args = run.parse_args(["--extract-version", "v3", "--extract-model", "test-model"])
    options = run.extraction_overrides(args, {})
    run.extract_graphs("http://backend", "ontology", [{"text": "input"}], options, {})
    body = json.loads(submit.call_args.kwargs["data"])
    assert body["options"] == {"version": "v3", "model": "test-model"}


def test_help_lists_new_options(capsys):
    with pytest.raises(SystemExit) as exit_info:
        run.parse_args(["--help"])
    assert exit_info.value.code == 0
    assert "--extract-provider" in capsys.readouterr().out


def test_model_override_cannot_be_misrepresented_by_reused_predictions():
    args = run.parse_args(["--extract-model", "new-model", "--reuse-extract-from", "old"])
    with pytest.raises(ValueError):
        run.extraction_overrides(args, {})
