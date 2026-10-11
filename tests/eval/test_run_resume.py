from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from eval import run as eval_run


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(eval_run, "RETRY_SLEEP_SECONDS", 0)


def _touch_sources(root: Path, names: list[str]) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in names:
        path = root / name
        path.write_text(name, encoding="utf-8")
        paths.append(path)
    return paths


def test_call_with_retries_succeeds_on_third_try():
    hits = {"n": 0}

    def boom():
        hits["n"] += 1
        if hits["n"] < 3:
            raise RuntimeError(f"fail-{hits['n']}")
        return "ok"

    value, attempts = eval_run.call_with_retries(boom, label="unit")
    assert value == "ok"
    assert attempts == 3
    assert hits["n"] == 3


def test_parse_retries_then_skips_middle_file(tmp_path: Path):
    sources = _touch_sources(tmp_path / "docs", ["a.txt", "b.txt", "c.txt"])
    calls: list[str] = []

    def fake_parse(_base_url, source, _options, _params):
        calls.append(source.name)
        if source.name == "b.txt":
            raise RuntimeError("bad-file")
        return {"data": {"markdown": f"# {source.name}\nbody"}}

    with patch.object(eval_run, "parse_document", side_effect=fake_parse):
        markdown, block = eval_run._parse_sources("http://x", sources, {}, {}, tmp_path / "out")

    assert "a.txt" in markdown and "c.txt" in markdown
    assert "b.txt" not in markdown
    assert block["status"] == "partial"
    assert calls == ["a.txt", "b.txt", "b.txt", "b.txt", "c.txt"]
    by_name = {item["file"]: item for item in block["per_file"]}
    assert by_name["a.txt"]["status"] == "succeeded"
    assert by_name["a.txt"]["attempts"] == 1
    assert by_name["b.txt"]["status"] == "failed"
    assert by_name["b.txt"]["attempts"] == 3
    assert by_name["c.txt"]["status"] == "succeeded"
    assert (tmp_path / "out" / "parse_b.skipped.json").is_file()


def test_ingest_retries_then_skips_middle_file(tmp_path: Path):
    sources = _touch_sources(tmp_path / "docs", ["a.txt", "b.txt", "c.txt"])
    calls: list[str] = []

    def fake_ingest(_base_url, source, _ontology, _options, _params):
        calls.append(source.name)
        if source.name == "b.txt":
            raise RuntimeError("ingest-bad")
        return {
            "data": {
                "graph": {
                    "nodes": [{"id": source.stem, "type": "Doc", "name": source.name}],
                    "relationships": [],
                }
            }
        }

    with patch.object(eval_run, "ingest_file", side_effect=fake_ingest):
        block = eval_run._run_system("http://x", sources, "onto", {}, {}, None, tmp_path / "out", None)

    assert block["status"] == "partial"
    assert calls == ["a.txt", "b.txt", "b.txt", "b.txt", "c.txt"]
    by_name = {item["file"]: item for item in block["per_file"]}
    assert by_name["b.txt"]["status"] == "failed"
    assert by_name["b.txt"]["attempts"] == 3
    assert by_name["a.txt"]["status"] == "succeeded"
    assert by_name["c.txt"]["status"] == "succeeded"


def test_case_retries_then_continues_to_next(tmp_path: Path):
    calls: list[str] = []

    def fake_backend(*, out_dir, **_kwargs):
        name = out_dir.name
        calls.append(name)
        if name == "mid":
            raise RuntimeError("explode")
        return {
            "system": {"status": "skipped"},
            "modules": {"extract": {"status": "succeeded"}},
            "markdown": f"# {name}",
        }

    results = []
    with patch.object(eval_run, "run_backend_case", side_effect=fake_backend):
        for case in ("first", "mid", "third"):
            backend, plugin, markdown, attempts, error = eval_run.run_case_with_retries(
                label=f"g/{case}",
                run_backend=True,
                run_plugin=False,
                base_url="http://x",
                mcp_url="http://x/mcp",
                ontology="onto",
                sources=[tmp_path / f"{case}.txt"],
                options={},
                params={},
                golden=None,
                out_dir=tmp_path / case,
                score_ctx=None,
                modes=["chunk-extract"],
                reuse_dir=None,
            )
            results.append((case, backend, plugin, markdown, attempts, error))

    assert calls == ["first", "mid", "mid", "mid", "third"]
    assert results[0][4] == 1 and results[0][5] is None
    assert results[0][0] == "first" and (results[0][1] or {})["modules"]["extract"]["status"] == "succeeded"
    assert results[1][4] == 3 and results[1][5]
    assert results[1][1]["status"] == "failed"
    assert results[2][4] == 1 and results[2][5] is None
    assert results[2][1]["modules"]["extract"]["status"] == "succeeded"


def test_uncaught_exception_does_not_leave_run_case_with_retries(tmp_path: Path):
    def fake_backend(**_kwargs):
        raise KeyError("unexpected")

    with patch.object(eval_run, "run_backend_case", side_effect=fake_backend):
        backend, _plugin, _md, attempts, error = eval_run.run_case_with_retries(
            label="g/x",
            run_backend=True,
            run_plugin=False,
            base_url="http://x",
            mcp_url="http://x/mcp",
            ontology="onto",
            sources=[tmp_path / "a.txt"],
            options={},
            params={},
            golden=None,
            out_dir=tmp_path / "x",
            score_ctx=None,
            modes=["chunk-extract"],
            reuse_dir=None,
        )
    assert attempts == 3
    assert "unexpected" in (error or "")
    assert backend["status"] == "failed"


def test_successful_parse_is_reused_on_second_call(tmp_path: Path):
    sources = _touch_sources(tmp_path / "docs", ["a.txt"])
    calls = {"n": 0}

    def fake_parse(_base_url, source, _options, _params):
        calls["n"] += 1
        return {"data": {"markdown": f"# {source.name}"}}

    out = tmp_path / "out"
    with patch.object(eval_run, "parse_document", side_effect=fake_parse):
        eval_run._parse_sources("http://x", sources, {}, {}, out)
        markdown, block = eval_run._parse_sources("http://x", sources, {}, {}, out)
    assert calls["n"] == 1
    assert block["per_file"][0]["status"] == "reused"
    assert markdown.startswith("# a.txt")


def test_default_params_come_from_eval_kit(tmp_path: Path):
    path = eval_run.default_params_path(tmp_path)
    assert path == eval_run.KIT_DIR / "params.yaml"
    assert path.is_file()
    override = tmp_path / "params.yaml"
    override.write_text("backend_url: http://override\n", encoding="utf-8")
    assert eval_run.default_params_path(tmp_path) == override


def test_real_workspace_inventory():
    workspace = eval_run.DEFAULT_WORKSPACE
    types = {item.name for item in eval_run.discover_types(workspace)}
    # 2026-10-11 评测集精简：standard 全域与 official_06–10 已下线，workspace 只保留
    # official_01–05、rule/rule_01、simple/Simple_故障规则的诊断 三类共 7 个 case。
    assert types == {"official", "rule", "simple"}
    assert len(eval_run.discover_cases(workspace / "official")) == 5
    assert len(eval_run.discover_cases(workspace / "rule")) == 1
    assert len(eval_run.discover_cases(workspace / "simple")) == 1
    first = workspace / "official" / "official_01"
    assert (first / "docs").is_dir()
    assert (first / "extract" / "golden.json").is_file()


def test_persist_summary_writes_before_finished(tmp_path: Path):
    summary = {"cases": [{"case": "a", "status": "succeeded"}], "succeeded": 1, "failed": 0}
    running = {
        "backend_system": {"entities": {}, "relations": {}},
        "backend_extract": {"entities": {}, "relations": {}},
        "plugin_extract": {"entities": {}, "relations": {}},
    }
    eval_run.persist_run_summary(
        tmp_path,
        summary,
        running,
        running,
        match_mode="identity",
        extract_versions=set(),
        run_started=0.0,
    )
    payload = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert payload["cases"][0]["case"] == "a"
    assert "finished_at" not in payload
    assert "interrupted" not in payload
