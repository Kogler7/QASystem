"""Resume regressions: retain input bytes and measured inference cost."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from eval import run


GOLDEN = {"entities": [{"type": "T", "name": "a"}], "relations": []}
ENVELOPE = {"data": {"graph": GOLDEN}}
CHUNKS = b'{"data": {"items": [{"text": "fixed", "range": [0, 5]}]}, "meta": "preserve"}\n'


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _chunks(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "chunks.json").write_bytes(CHUNKS)
    return directory


def _backend(out_dir, *, reuse_dir=None, reuse_extract_dir=None):
    return run.run_backend_case(
        base_url="http://fixture.invalid", ontology="entity T", sources=[], options={}, params={},
        golden=GOLDEN, out_dir=out_dir, score_ctx={"match_mode": "identity"}, modes=["extract"],
        reuse_dir=reuse_dir, reuse_extract_dir=reuse_extract_dir,
    )


def test_extract_only_run_keeps_original_chunk_bytes_for_next_rescore(tmp_path, monkeypatch):
    source = _chunks(tmp_path / "source")
    out = tmp_path / "out"
    request = Mock(return_value=ENVELOPE)
    monkeypatch.setattr(run, "extract_graphs", request)

    first = _backend(out, reuse_dir=source)
    second = _backend(out)

    assert (out / "chunks.json").read_bytes() == CHUNKS
    assert (source / "chunks.json").read_bytes() == CHUNKS
    assert first["modules"]["extract"]["status"] == "succeeded"
    assert second["modules"]["extract"]["status"] == "reused"
    request.assert_called_once()


@pytest.mark.parametrize('status', ['partial', 'failed'])
def test_incomplete_extraction_is_called_again_but_success_is_cached(tmp_path, monkeypatch, status):
    source = _chunks(tmp_path / 'source')
    out = tmp_path / 'out'
    incomplete = {'data': {'graph': GOLDEN, 'quality': {'status': status, 'error_code': 'LLM_RESPONSE_INCOMPLETE'}}}
    request = Mock(side_effect=[incomplete, ENVELOPE])
    monkeypatch.setattr(run, 'extract_graphs', request)
    first = _backend(out, reuse_dir=source)
    second = _backend(out)
    third = _backend(out)
    assert first['modules']['extract']['status'] == status
    assert second['modules']['extract']['status'] == 'succeeded'
    assert third['modules']['extract']['status'] == 'reused'
    assert request.call_count == 2
    assert (out / 'chunks.json').read_bytes() == CHUNKS


def test_resume_preserves_api_duration_and_excludes_judge_time(tmp_path, monkeypatch):
    source = _chunks(tmp_path / "source")
    out = tmp_path / "out"
    clock = [0.0]
    monkeypatch.setattr(run.time, "perf_counter", lambda: clock[0])
    original_score = run.maybe_score

    def extract(*_args):
        clock[0] += 100.0
        return ENVELOPE

    def score(*args):
        # The cost is durable before a potentially long judge call starts.
        assert json.loads((out / "backend_extract.timing.json").read_text())["elapsed_seconds"] == 100.0
        clock[0] += 300.0
        return original_score(*args)

    request = Mock(side_effect=extract)
    monkeypatch.setattr(run, "extract_graphs", request)
    monkeypatch.setattr(run, "maybe_score", score)
    first = _backend(out, reuse_dir=source)
    resumed = _backend(out)

    assert first["modules"]["extract"]["elapsed_seconds"] == 100.0
    assert first["modules"]["extract"]["score_elapsed_seconds"] == 300.0
    block = resumed["modules"]["extract"]
    assert block["elapsed_seconds"] == 100.0
    assert "source_timing_missing" not in block
    request.assert_called_once()
    gate = run.build_run_gate(
        {"cases": [{"group": "g", "case": "c", "scored": True, "status": "succeeded", "backend": resumed}]},
        min_f1=0, max_case_seconds=None, stage_budgets={"extract": 60},
    )
    assert gate["passed"] is False
    assert gate["budget_violations"] == [{"case": "g/c", "stage": "extract", "elapsed": 100.0, "budget": 60}]


def test_judge_exception_does_not_lose_completed_api_timing(tmp_path, monkeypatch):
    source = _chunks(tmp_path / "source")
    out = tmp_path / "out"
    clock = [0.0]
    monkeypatch.setattr(run.time, "perf_counter", lambda: clock[0])
    original_score = run.maybe_score

    def extract(*_args):
        clock[0] += 71.25
        return ENVELOPE

    def failed_score(*_args):
        clock[0] += 300
        raise RuntimeError("judge fixture failure")

    request = Mock(side_effect=extract)
    monkeypatch.setattr(run, "extract_graphs", request)
    monkeypatch.setattr(run, "maybe_score", failed_score)
    failed = _backend(out, reuse_dir=source)
    assert failed["modules"]["extract"]["status"] == "failed"
    monkeypatch.setattr(run, "maybe_score", original_score)

    resumed = _backend(out)

    assert resumed["modules"]["extract"]["status"] == "reused"
    assert resumed["modules"]["extract"]["elapsed_seconds"] == 71.25
    request.assert_called_once()


@pytest.mark.parametrize("timing", [None, "{broken", "[]", '{"elapsed_seconds": null}',
                                   '{"elapsed_seconds": -1}', '{"elapsed_seconds": true}',
                                   '{"elapsed_seconds": NaN}'])
def test_missing_or_invalid_local_timing_is_unknown_never_zero(tmp_path, monkeypatch, timing):
    out = _chunks(tmp_path / "out")
    _save(out / "backend_extract.json", ENVELOPE)
    if timing is not None:
        (out / "backend_extract.timing.json").write_text(timing, encoding="utf-8")
    request = Mock(side_effect=AssertionError("A saved result must not be silently re-extracted"))
    monkeypatch.setattr(run, "extract_graphs", request)

    block = _backend(out)["modules"]["extract"]

    assert block["status"] == "reused"
    assert block["elapsed_seconds"] is None
    assert block["source_timing_missing"] is True
    request.assert_not_called()


@pytest.mark.parametrize("elapsed", [100.0, None])
def test_cross_run_reuse_persists_original_scores_timing_for_local_resume(tmp_path, monkeypatch, elapsed):
    inputs = _chunks(tmp_path / "inputs")
    source = tmp_path / "old"
    _save(source / "backend_extract.json", ENVELOPE)
    _save(source / "scores.json", {"elapsed_seconds": 150, "backend": {"modules": {
        "extract": {"elapsed_seconds": elapsed, "score_elapsed_seconds": 20}}}})
    before = {path.name: path.read_bytes() for path in source.iterdir()}
    request = Mock(side_effect=AssertionError("Reuse must not call extraction"))
    monkeypatch.setattr(run, "extract_graphs", request)
    out = tmp_path / "new"

    _backend(out, reuse_dir=inputs, reuse_extract_dir=source)
    resumed = _backend(out)

    timing = json.loads((out / "backend_extract.timing.json").read_text())
    assert timing == {"elapsed_seconds": elapsed, "source": str(source / "scores.json")}
    assert resumed["modules"]["extract"]["elapsed_seconds"] == elapsed
    assert bool(resumed["modules"]["extract"].get("source_timing_missing")) is (elapsed is None)
    assert {path.name: path.read_bytes() for path in source.iterdir()} == before
    request.assert_not_called()
