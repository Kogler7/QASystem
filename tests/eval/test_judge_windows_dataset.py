"""Reviewed edges remain partial labels inside authentic graph windows."""
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "test_data/judge_windows/v1"


def test_frozen_windows_use_canonical_lf_bytes_without_label_changes():
    for filename, manifest_name in (
        ('windows.jsonl', 'manifest.json'),
        ('small_reviewed.jsonl', 'small_review_manifest.json'),
    ):
        assert b'\r\n' not in (DATA / filename).read_bytes()
        manifest = json.loads((DATA / manifest_name).read_text(encoding='utf-8'))
        assert manifest['version'] == '1.0.1'
        assert manifest['byte_revision']['semantic_content_changed'] is False
        assert manifest['byte_revision']['encoding'] == 'UTF-8, LF'


def test_window_labels_retain_exact_reviewed_objects():
    pairs = {p["id"]: p for p in map(json.loads, (ROOT / "test_data/judge/v1/pairs.jsonl").read_text(encoding="utf-8").splitlines())}
    rows = list(map(json.loads, (DATA / "windows.jsonl").read_text(encoding="utf-8").splitlines()))
    assert 12 <= len(rows) <= 16
    assert len({w["document_id"] for w in rows}) >= 3
    assert any(len(w["golden_items"]) == 40 and len(w["predicted_items"]) == 20 for w in rows)
    for window in rows:
        assert not window["complete_labels"]
        assert "expected_pairs" not in window
        assert len(window["golden_items"]) <= 40
        assert len(window["predicted_items"]) <= 20
        assert window["labels"]
        for label in window["labels"]:
            pair = pairs[label["pair_id"]]
            assert pair["document_id"] == window["document_id"]
            assert window["golden_items"][label["golden_index"]] == pair["golden"]
            assert window["predicted_items"][label["predicted_index"]] == pair["predicted"]
            assert label["expected_match"] is pair["expected_match"]
            assert label["label_source"] == pair["label"]


def test_window_manifest_hash_and_partial_counts():
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    rows = list(map(json.loads, (DATA / "windows.jsonl").read_text(encoding="utf-8").splitlines()))
    assert manifest["files_sha256"]["windows.jsonl"] == hashlib.sha256((DATA / "windows.jsonl").read_bytes()).hexdigest()
    assert manifest["summary"]["label_instances"] == sum(len(w["labels"]) for w in rows)
    assert manifest["summary"]["windows"] == len(rows)


def test_complete_small_windows_cover_every_cell_without_using_type_contract_as_semantic_quality():
    from eval import run
    path=DATA/'small_reviewed.jsonl'
    manifest=json.loads((DATA/'small_review_manifest.json').read_text(encoding='utf8'))
    assert manifest['sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    rows=[json.loads(x) for x in path.read_text(encoding='utf8').splitlines()]
    assert len(rows)==6
    for row in rows:
        semantic={(x['golden_index'],x['predicted_index']):x for x in row['labels']}
        contracts={(x['golden_index'],x['predicted_index']):x for x in row['contract_labels']}
        assert not set(semantic)&set(contracts)
        assert set(semantic)|set(contracts)=={(g,p) for g in range(4) for p in range(4)}
        assert set(map(tuple,row['expected_pairs']))=={pair for pair,label in semantic.items() if label['expected_match']}
        for (g,p) in semantic:
            assert run._compatible_types(row['golden_items'][g],row['predicted_items'][p],relation=row['collection']=='relation_matches')
        assert all(not label['expected_match'] for label in contracts.values())
