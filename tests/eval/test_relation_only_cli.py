"""CLI contract for --relation-only-from-golden (no Backend extract POST)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from eval import run


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    case = workspace / "trouble" / "one"
    (case / "docs").mkdir(parents=True)
    (case / "docs" / "sample.md").write_text("张三就职于示例单位。", encoding="utf-8")
    ontology = workspace / "trouble" / "ontology"
    ontology.mkdir()
    (ontology / "schema.puml").write_text(
        "@startuml\nclass Person { name: String }\nclass Organization { name: String }\n"
        "Person --> Organization : worksAt\n@enduml\n",
        encoding="utf-8",
    )
    (case / "extract").mkdir()
    golden = {
        "entities": [
            {"type": "Person", "name": "张三", "properties": {}},
            {"type": "Organization", "name": "示例单位", "properties": {}},
        ],
        "relations": [
            {
                "from_type": "Person",
                "from_name": "张三",
                "rel_type": "worksAt",
                "to_type": "Organization",
                "to_name": "示例单位",
            }
        ],
    }
    (case / "extract" / "golden.json").write_text(json.dumps(golden), encoding="utf-8")
    return workspace


def test_relation_only_requires_from_run(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    argv = [
        "--workspace",
        str(workspace),
        "--mode",
        "extract",
        "--extract-version",
        "v2.2-v31",
        "--match-mode",
        "identity",
        "--relation-only-from-golden",
        "--runs-retention-days",
        "0",
        "trouble/one",
    ]
    with patch("sys.argv", ["eval/run.py", *argv]), patch.object(run, "log"):
        assert run.main() == 1


def test_relation_only_offline_skips_backend_extract(tmp_path: Path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    frozen = tmp_path / "frozen"
    case_dir = frozen / "trouble" / "one"
    case_dir.mkdir(parents=True)
    chunks = {
        "code": 0,
        "data": {
            "items": [{"text": "张三就职于示例单位。", "ranges": [0, 10], "metadata": {}}],
            "count": 1,
        },
    }
    (case_dir / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")

    fake_envelope = {
        "code": 0,
        "info": "fixture",
        "data": {
            "graph": {
                "nodes": [
                    {"uid": "Person::张三", "label": "Person", "name": "张三", "properties": {"name": "张三"}},
                    {
                        "uid": "Organization::示例单位",
                        "label": "Organization",
                        "name": "示例单位",
                        "properties": {"name": "示例单位"},
                    },
                ],
                "relationships": [
                    {
                        "from_uid": "Person::张三",
                        "to_uid": "Organization::示例单位",
                        "type": "worksAt",
                    }
                ],
            },
            "quality": {
                "extract_version": "v2.2-v31",
                "status": "succeeded",
                "relation_only_from_golden": True,
                "deterministic_enrichment": "disabled",
            },
        },
    }

    posts: list[str] = []

    def boom_extract(*_args, **_kwargs):
        posts.append("extract")
        raise AssertionError("Backend extract must not be called in relation-only mode")

    monkeypatch.setattr(run, "_relation_only_offline_envelope", lambda *_a, **_k: fake_envelope)
    monkeypatch.setattr(run, "extract_graphs", boom_extract)
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    (tmp_path / "runs").mkdir()

    argv = [
        "--workspace",
        str(workspace),
        "--backend-url",
        "http://127.0.0.1:9",
        "--mode",
        "extract",
        "--from-run",
        str(frozen),
        "--extract-version",
        "v2.2-v31",
        "--relation-only-from-golden",
        "--match-mode",
        "identity",
        "--runs-retention-days",
        "0",
        "trouble/one",
    ]
    with patch("sys.argv", ["eval/run.py", *argv]), patch.object(run, "log"):
        code = run.main()
    assert code == 0
    assert posts == []
