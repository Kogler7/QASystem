"""Integrity checks for historical entity-stage count proxies, not human labels."""
import hashlib
import json
from pathlib import Path


DATA = Path(__file__).resolve().parents[2] / "test_data/chunk_density/entity_counts_v1"
# Immutable evidence from the original 1.0.0 manifest at d6d65ea5. Keeping these
# independently frozen prevents a new content hash from hiding corpus changes.
ORIGINAL_CRLF_SHA256 = {
    "cases.jsonl": "19cfcfb318924b8324b3c060c96c5494bdc8205a9bde0dc2e181efc97cb95cfe",
    "calibration.jsonl": "bd0b721096eae49663fdb38e3c5f6eaf82af269593ff3b4f180e05008d278c7d",
    "test.jsonl": "7fcff56c9762b2676d3a390492ae8f6fb3fb4197735a6d8221c085194fc51cdc",
    "review_queue.jsonl": "726a60aa35467a1e83f65819f808d484c4871df9615a6ef6897144d85e77cdb5",
}


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
    assert set(manifest["files_sha256"]) == set(ORIGINAL_CRLF_SHA256)
    for name, expected in manifest["files_sha256"].items():
        assert hashlib.sha256((DATA / name).read_bytes()).hexdigest() == expected
    assert manifest["not_human_ground_truth"] is True
    assert manifest["counts"]["main_cases"] == len(calibration) + len(test)


def test_lf_checksum_migration_preserves_original_frozen_content():
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["serialization"] == {
        "encoding": "UTF-8", "record_separator": "LF", "final_newline": True, "hash_scope": "exact_bytes",
    }
    migration = manifest["checksum_migration"]
    assert migration["source_version"] == "1.0.0"
    assert migration["source_commit"] == "d6d65ea5e98adfb05138d94da7d49eb254de0ecf"
    assert migration["source_record_separator"] == "CRLF"
    assert migration["source_files_sha256"] == ORIGINAL_CRLF_SHA256
    for name, expected in ORIGINAL_CRLF_SHA256.items():
        raw = (DATA / name).read_bytes()
        assert raw.endswith(b"\n") and b"\r" not in raw
        assert not raw.startswith(b"\xef\xbb\xbf")
        # Only JSONL record separators are transformed. No JSON parsing or
        # reserialization can conceal changed text, labels, fields or order.
        assert hashlib.sha256(raw.replace(b"\n", b"\r\n")).hexdigest() == expected
