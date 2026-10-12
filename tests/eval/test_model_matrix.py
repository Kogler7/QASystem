import json
from pathlib import Path

import pytest

from eval import model_matrix as matrix


def test_extract_and_rescore_commands_keep_models_independent(tmp_path):
    extractor = {"id": "sf", "provider": "siliconflow", "model": "extract-model", "version": "v3"}
    judge = {"id": "clef", "adapter": "systemone", "model": "clef-flash", "base_url": "http://clef:8188"}
    command = matrix.build_command(tmp_path, tmp_path, ["official/test"], matrix.run.params_from_args(matrix.run.parse_args([])), extractor, judge)
    assert command[command.index("--extract-model") + 1] == "extract-model"
    assert command[command.index("--judge-model") + 1] == "clef-flash"
    command = matrix.build_command(tmp_path, tmp_path, ["official/test"], matrix.run.params_from_args(matrix.run.parse_args([])), extractor, judge, tmp_path / "old")
    assert "--extract-model" not in command
    assert "--extract-provider" not in command
    assert "--reuse-extract-from" in command
    assert "--from-run" in command


@pytest.mark.parametrize("chunk_status", ["succeeded", "partial"])
def test_preparation_freezes_ontology_and_chunks_or_rejects_fallback(tmp_path, monkeypatch, chunk_status):
    workspace = tmp_path / "source"
    case = workspace / "official/test"
    case.mkdir(parents=True)
    (case / "document.md").write_text("frozen content", encoding="utf-8")
    ontology = workspace / "official/ontology"
    ontology.mkdir()
    (ontology / "types.puml").write_text("class Entity", encoding="utf-8")

    def chunk(base_url, markdown, name, options, params, output):
        assert markdown == "frozen content"
        (output / "chunks.json").write_text(json.dumps({"data": {"items": [{"text": markdown}]}}), encoding="utf-8")
        return [{"text": markdown}], {"status": chunk_status}

    monkeypatch.setattr(matrix.run, "_chunk_markdown", chunk)
    destination = tmp_path / "tmp/inputs"
    if chunk_status == "partial":
        with pytest.raises(RuntimeError, match="Incomplete chunking"):
            matrix.prepare_inputs(workspace, [case], destination, {"backend_url": "http://backend"})
    else:
        records = matrix.prepare_inputs(workspace, [case], destination, {"backend_url": "http://backend"})
        assert records[0]["chunk_count"] == 1
        assert len(records[0]["chunks_sha256"]) == 64
        assert (destination / "official/test/ontology/types.puml").read_text() == "class Entity"
        with pytest.raises(ValueError, match="already exists"):
            matrix.prepare_inputs(workspace, [case], destination, {"backend_url": "http://backend"})
