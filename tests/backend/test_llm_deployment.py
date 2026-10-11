import hashlib
from pathlib import Path

import pytest
import yaml

from docker.download_llm_models import verify

ROOT = Path(__file__).resolve().parents[2]


def test_llm_uses_shared_image_readonly_models_and_explicit_gpu():
    service = yaml.safe_load((ROOT / "docker/docker-compose.llm.yml").read_text())["services"]["llm"]
    backend = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["backend"]
    assert service["image"] == backend["image"]
    assert service["volumes"] == ["${HOST_MODELS_DIR:-./deploy/models}:/models:ro"]
    assert ":?" in service["environment"]["CUDA_VISIBLE_DEVICES"]
    assert service["environment"]["VLLM_NO_USAGE_STATS"] == "1"
    assert service["environment"]["DO_NOT_TRACK"] == "1"
    assert "exec vllm serve" in service["command"][-1]
    assert "venv" not in service["command"][-1]


@pytest.mark.parametrize("data", [None, b"short", b"wrong!!", b"weights"])
def test_download_verification_rejects_missing_truncated_and_corrupt_weights(tmp_path, data):
    entry = {"rfilename": "model.safetensors", "size": 7,
             "lfs": {"sha256": hashlib.sha256(b"weights").hexdigest()}}
    if data is not None:
        (tmp_path / entry["rfilename"]).write_bytes(data)
    if data == b"weights":
        verify(tmp_path, [entry])
    else:
        with pytest.raises(RuntimeError):
            verify(tmp_path, [entry])


def test_config_checksum_rejects_same_size_revision_drift(tmp_path):
    expected = b'{"a":1}'
    entry = {"rfilename": "config.json", "size": len(expected),
             "blobId": hashlib.sha1(b"blob 7\0" + expected).hexdigest()}
    path = tmp_path / "config.json"
    path.write_bytes(expected)
    verify(tmp_path, [entry])
    path.write_bytes(b'{"a":2}')
    with pytest.raises(RuntimeError, match="Git blob checksum"):
        verify(tmp_path, [entry])
