"""The changed V3.1 inputs are a separate, traceable experiment."""
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'test_data/eval-v31-20261014'


def test_experimental_inputs_match_canonical_git_byte_manifest():
    manifest = json.loads((DATA / 'dataset-manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'experimental'
    assert manifest['baseline_workspace'] == 'test_data/eval'
    assert len(manifest['source_commit']) == 40
    for relative, expected in manifest['files_sha256'].items():
        path = DATA / relative
        assert path.resolve().is_relative_to(DATA.resolve())
        content = path.read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected, relative
        if path.suffix in {'.json', '.jsonl', '.md', '.puml', '.txt'}:
            assert b'\r\n' not in content, relative


def test_old_source_artifacts_remain_available_in_the_baseline():
    baseline = ROOT / 'test_data/eval'
    assert (baseline / 'official/official_02/document.md').is_file()
    assert (baseline / 'official/official_03/document.md').is_file()
    assert (baseline / 'official/official_03/parse').is_dir()
    assert not (DATA / '归档.zip').exists()


def test_new_trouble_labels_have_an_independent_verified_dataset():
    data = ROOT / 'test_data/eval-v31-20261014.2'
    manifest = json.loads((data / 'dataset-manifest.json').read_text(encoding='utf-8'))
    assert manifest['source_commit'] == '134ec99516220778a1cfab59ccbc459a12e55386'
    for relative, digest in manifest['files_sha256'].items():
        path = data / relative
        assert path.resolve().is_relative_to(data.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    old = DATA / 'trouble/trouble_01/extract/golden.json'
    new = data / 'trouble/trouble_01/extract/golden.json'
    assert old.read_bytes() != new.read_bytes()
