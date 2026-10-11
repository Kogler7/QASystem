import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from algorithm.knowledge_extract.v2 import jev_client
from docker import download_models


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("available", [True, False])
def test_nanojev_only_disables_native_triton_when_available(monkeypatch, available):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setattr(jev_client, "_require_bundle", lambda root: None)
    def find_spec(name):
        assert name == "torch._native.triton_utils"
        if not available:
            raise ModuleNotFoundError(name="torch._native")
        return object()
    monkeypatch.setattr(jev_client.importlib.util, "find_spec", find_spec)
    calls = []
    monkeypatch.setattr(jev_client, "_load_predictor_class", lambda root: lambda *a, **kw: calls.append(kw))
    jev_client.NanoJevClient.from_pretrained("/models/nanojev")
    assert calls[0]["disable_native_triton"] is available


def test_native_triton_probe_does_not_hide_unrelated_import_failure(monkeypatch):
    def fail(name):
        raise ModuleNotFoundError(name="unrelated_dependency")
    monkeypatch.setattr(jev_client.importlib.util, "find_spec", fail)
    with pytest.raises(ModuleNotFoundError):
        jev_client._has_native_triton()


def test_clef_deployment_uses_shared_image_and_read_only_external_assets():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["clef"]
    assert service["image"] == compose["services"]["backend"]["image"]
    assert service["profiles"] == ["decisions"]
    assert "${QASYSTEM_SOURCE_DIR:-.}:/app:ro" in service["volumes"]
    assert "${HOST_MODELS_DIR:-./deploy/models}:/models:ro" in service["volumes"]
    assert "ports" not in service
    assert "exec python -m clef_service" in service["command"][-1]
    assert "clef_service" in (ROOT / "package-deploy.ps1").read_text(encoding="utf-8")
    nano = compose["services"]["nanojev"]
    assert nano["image"] == service["image"]
    assert nano["volumes"] == service["volumes"]
    assert nano["command"] == "python -m nanojev_service"


def test_decision_runtime_reuses_backend_dependency_versions():
    from packaging.requirements import Requirement
    def requirements(path):
        lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
        return {r.name: r for line in lines if line and not line.startswith("#")
                for r in [Requirement(line)]}
    backend = requirements(ROOT / "backend/requirements.txt")
    decisions = requirements(ROOT / "clef_service/requirements.txt")
    for name in backend.keys() & decisions.keys():
        assert decisions[name].specifier == backend[name].specifier, name
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "clef-venv" not in dockerfile
    assert "python -m pip install" in dockerfile


@pytest.mark.parametrize("missing_shard", [False, True])
def test_clef_download_pins_revision_and_checks_backbone(monkeypatch, tmp_path, missing_shard):
    def download(**kwargs):
        assert kwargs["revision"] == download_models.CLEF_REVISION
        assert kwargs["repo_id"] == "Cloudflare/clef-flash"
        for name in ("config.json", "joint_schema_model.py", "joint_head.safetensors",
                     "joint_head_config.json", "tokenizer.json", "tokenizer_config.json", "processor_config.json"):
            (tmp_path / name).touch()
        (tmp_path / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"x": "shard.safetensors"}}))
        if not missing_shard:
            (tmp_path / "shard.safetensors").touch()
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=download))
    monkeypatch.setattr(download_models, "CLEF_MODEL", ("Cloudflare/clef-flash", tmp_path))
    if missing_shard:
        with pytest.raises(RuntimeError, match="backbone shards"):
            download_models._download_clef_model()
    else:
        download_models._download_clef_model()


def test_nanojev_download_rejects_missing_trainer_and_tokenizer(monkeypatch, tmp_path):
    def download(**kwargs):
        assert kwargs["revision"] == download_models.JEV_REVISION
        (tmp_path / "best.safetensors").touch()
        (tmp_path / "source/scripts").mkdir(parents=True)
        (tmp_path / "source/scripts/predict_toy_decisions.py").touch()
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=download))
    monkeypatch.setattr(download_models, "JEV_MODEL", ("C-Tianyu/NanoJev", tmp_path))
    with pytest.raises(RuntimeError, match="train_toy_decisions.py"):
        download_models._download_jev_model()


def test_remote_nanojev_reuses_service_without_loading_checkpoint(monkeypatch):
    monkeypatch.setenv("NANOJEV_SERVICE_URL", "http://nanojev:8189")
    monkeypatch.setenv("ALGORITHM_RUNTIME_PROFILE", "remote-only")
    client = jev_client.create_jev_client()
    assert isinstance(client, jev_client.RemoteNanoJevClient)
    calls = []
    def infer(payload):
        calls.append(payload)
        return {"answers": {"q": {"type": "choice", "choice": "yes", "probabilities": {"yes": .9, "no": .1}}}}
    monkeypatch.setattr(client.client, "systemone", infer)
    answer = client.predict([{"id": "s", "state": "test", "questions": {"q": {"type": "choice", "criteria": {"yes": "Yes", "no": "No"}}}}])
    assert answer["s"]["q"].choice == "yes"
    assert len(calls) == 1


def test_reserved_gpus_are_not_reintroduced_by_discovery(monkeypatch):
    from backend.app.execution import resources, supervisor
    monkeypatch.setenv("ALGORITHM_RUNTIME_PROFILE", "full-runtime")
    monkeypatch.delenv("EXECUTION_GPU_DEVICE_IDS", raising=False)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setenv("GPU_DEVICE_IDS", "4,5,6,7")
    monkeypatch.setenv("EXECUTION_GPU_RESERVED_DEVICE_IDS", "7")
    monkeypatch.setattr(resources, "_query_nvidia_smi", lambda ids: [])
    assert supervisor._gpu_ids() == ["4", "5", "6"]
    monkeypatch.setenv("EXECUTION_GPU_RESERVED_DEVICE_IDS", "4,5,6,7")
    assert supervisor._gpu_ids() == []
    assert resources.GpuInventory(cache_seconds=0).snapshots() == []


def test_reserved_gpu_uuid_filters_physical_inventory(monkeypatch):
    from backend.app.execution import resources
    monkeypatch.delenv("EXECUTION_GPU_DEVICE_IDS", raising=False)
    monkeypatch.setenv("GPU_DEVICE_IDS", "4,7")
    monkeypatch.setenv("EXECUTION_GPU_RESERVED_DEVICE_IDS", "GPU-seven")
    monkeypatch.setattr(resources, "_query_nvidia_smi", lambda ids: [
        resources.GpuSnapshot("4", "GPU-four", 48000, 0, 0, "test"),
        resources.GpuSnapshot("7", "GPU-seven", 48000, 0, 0, "test")])
    assert [x.physical_index for x in resources.GpuInventory().snapshots()] == ["4"]
    monkeypatch.setattr(resources, "_query_nvidia_smi", lambda ids: [])
    assert resources.GpuInventory().snapshots() == []


def test_remote_nanojev_preserves_boolean_client_contract(monkeypatch):
    client = jev_client.RemoteNanoJevClient()
    def infer(payload):
        assert payload["questions"]["q"]["type"] == "noul"
        return {"answers": {"q": {"type": "noul", "noul": .8}}}
    monkeypatch.setattr(client.client, "systemone", infer)
    result = client.predict([{"id": "s", "state": "test", "questions": {"q": {"type": "boolean"}}}])
    assert result["s"]["q"].choice == "true"
    assert result["s"]["q"].probabilities["true"] == .8
