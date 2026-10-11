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


def test_chunk_cli_overrides_yaml_and_reaches_http(monkeypatch):
    params = {"options": {"chunking": {"chunk_overlap": 0, "size": 4000}}}
    options = run.extraction_overrides(run.parse_args(["--chunk-overlap", "80", "--chunk-size", "1000"]), params)
    submit = Mock(return_value={})
    monkeypatch.setattr(run, "submit_and_wait", submit)
    run.chunk_document("http://backend", "text", "test.md", options, {})
    assert json.loads(submit.call_args.kwargs["data"])["options"] == {"chunk_overlap": 80, "size": 1000}
    assert params["options"]["chunking"]["chunk_overlap"] == 0


@pytest.mark.parametrize("extra", [["--chunk-overlap", "-1"], ["--chunk-size", "50"],
    ["--chunk-overlap", "100", "--chunk-size", "100"],
    ["--chunk-overlap", "50", "--mode", "extract"],
    ["--chunk-overlap", "50", "--reuse-extract-from", "old"]])
def test_chunk_invalid_or_ineffective_overrides_rejected(extra):
    with pytest.raises(ValueError):
        run.extraction_overrides(run.parse_args(extra), {})


def test_chunk_overlap_default_and_explicit_disable():
    from backend.app.schemas import ChunkOptions
    assert ChunkOptions().chunk_overlap == 50
    assert ChunkOptions(chunk_overlap=0).chunk_overlap == 0
    assert run.load_params(run.KIT_DIR / "params.yaml")["options"]["chunking"]["chunk_overlap"] == 50
    assert run.extraction_overrides(run.parse_args(["--chunk-overlap", "0"]), {})["chunking"]["chunk_overlap"] == 0


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


def test_extract_http_keeps_frozen_source_ranges_and_table_metadata(monkeypatch):
    submit = Mock(return_value={})
    monkeypatch.setattr(run, "submit_and_wait", submit)
    metadata = {"table_format": "jsonl", "section_title": "Sheet-2", "table_data_row_start": 21}
    source = [{"text": '{"设备":"泵"}', "range": [120, 130], "metadata": metadata,
               "unused": "not an extraction contract field"}]
    frozen = run.extraction_item_payloads(source)
    run.extract_graphs("http://backend", "ontology", frozen, {}, {})
    items = json.loads(submit.call_args.kwargs["data"])["items"]
    assert items == [{"text": '{"设备":"泵"}', "ranges": [120, 130], "metadata": metadata}]
    frozen[0]["metadata"]["section_title"] = "changed"
    assert source[0]["metadata"]["section_title"] == "Sheet-2"


def test_help_lists_new_options(capsys):
    with pytest.raises(SystemExit) as exit_info:
        run.parse_args(["--help"])
    assert exit_info.value.code == 0
    assert "--extract-provider" in capsys.readouterr().out


def test_model_override_cannot_be_misrepresented_by_reused_predictions():
    args = run.parse_args(["--extract-model", "new-model", "--reuse-extract-from", "old"])
    with pytest.raises(ValueError):
        run.extraction_overrides(args, {})
