"""Integrity checks for historical entity-stage count proxies, not human labels."""
import hashlib
import json
from pathlib import Path


DATA = Path(__file__).resolve().parents[2] / "test_data/chunk_density/entity_counts_v1"


def _rows(name):
    return [json.loads(line) for line in (DATA / name).read_text(encoding="utf-8").splitlines()]


def test_proxy_counts_are_traceable_and_not_conflicting():
    cases = _rows("cases.jsonl")
    review = _rows("review_queue.jsonl")
    assert cases and review
    assert len({row["text_family"] for row in cases}) == len(cases)
    assert not ({row["text_family"] for row in cases} & {row["text_family"] for row in review})
    for row in cases:
        identities = {(e["type"], e["name"]) for e in row["entities"]}
        count = len(identities)
        assert count == row["entity_count"] <= row["raw_count"]
        assert row["expected_bucket"] == ("le20" if count <= 20 else "21to40" if count <= 40 else "gt40")
        assert row["historical_buckets"] == [row["expected_bucket"]]
        assert row["text_sha256"] == hashlib.sha256(row["text"].encode()).hexdigest()
        assert row["label_kind"] == "historical_extraction_proxy"
        assert row["finish_reason"] in {"stop", "end_turn"}
        assert row["entity_stage_status"] == "succeeded"
        assert len(row["source_files"]) == 4
        assert all(len(source["sha256"]) == 64 for source in row["source_files"])
    for row in review:
        assert row["expected_bucket"] is None
        assert row["reason"] == "ambiguous_counts"
        assert len(row["historical_buckets"]) >= 2


def test_proxy_split_and_file_hashes():
    calibration = _rows("calibration.jsonl")
    test = _rows("test.jsonl")
    assert calibration and test
    assert not ({r["text_family"] for r in calibration} & {r["text_family"] for r in test})
    assert calibration + test == _rows("cases.jsonl")
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files_sha256"].items():
        assert hashlib.sha256((DATA / name).read_bytes()).hexdigest() == expected
    assert manifest["not_human_ground_truth"] is True
    assert manifest["counts"]["main_cases"] == len(calibration) + len(test)
