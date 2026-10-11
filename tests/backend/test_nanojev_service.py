from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clef_service.app import Settings, create_app, validate_response, InputTooLong
from nanojev_service.app import NanoRuntime


def test_nano_runtime_preserves_question_types_and_normalizes_answers():
    runtime = NanoRuntime(Settings())
    requests = []
    def predict(request):
        requests.append(request)
        return {"states": [{"answers": {
            "n": {"probabilities": {"true": .8, "false": .2}},
            "c": {"choice": "a", "probabilities": {"a": .8, "b": .2}},
            "s": {"probabilities": {"0": .2, "1": .8}},
        }}]}
    runtime.predictor = SimpleNamespace(tokenizer=None, predict=predict)
    runtime.torch = SimpleNamespace(cuda=SimpleNamespace(OutOfMemoryError=MemoryError))
    runtime.module = SimpleNamespace(prepare_examples=lambda *a: [{"leaf_tokens": [[1, 2, 3]]}])
    request = {"model": "nanojev", "state": {"test": "value"}, "questions": {
        "n": {"type": "noul"}, "c": {"type": "choice", "criteria": {"a": "A", "b": "B"}},
        "s": {"type": "score", "criteria": ["Low", "High"]}}}
    response = validate_response(runtime.infer(request), request, 2048)
    assert response["answers"]["n"]["noul"] == .8
    assert response["answers"]["s"]["score"] == .8
    questions = requests[0]["states"][0]["questions"]
    assert questions["n"]["type"] == "boolean"
    assert questions["s"]["type"] == "score"
    assert response["usage"]["input_tokens"] == 3


def test_nano_service_loads_once_and_rejects_wrong_model():
    events = []
    class Runtime:
        def __init__(self, settings): pass
        def load(self): events.append("load")
        def close(self): events.append("close")
        def infer(self, payload):
            events.append("infer")
            return {"model": "nanojev", "answers": {"q": {"type": "noul", "noul": .8}},
                    "usage": {"input_tokens": 10, "output_tokens": 0}}
    app = create_app(Settings(), Runtime, model_name="nanojev")
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ready", "model": "nanojev"}
        for _ in range(3):
            assert client.post("/v1/systemone", json={"model": "nanojev", "state": "test", "questions": {"q": {"type": "noul"}}}).status_code == 200
        assert client.post("/v1/systemone", json={"model": "clef-flash", "state": "test", "questions": {"q": {"type": "noul"}}}).status_code == 422
    assert events == ["load", "infer", "infer", "infer", "close"]


def test_nano_transport_has_separate_service_credentials(monkeypatch):
    from external_services.nanojev import NanoJevServiceClient
    monkeypatch.setenv("NANOJEV_SERVICE_API_KEY", "nano-key")
    monkeypatch.setenv("CLEF_SERVICE_API_KEY", "clef-key")
    client = NanoJevServiceClient()
    assert client.api_key == "nano-key"
    assert client.model == "nanojev"


def test_nano_loads_once_then_warms_before_ready(tmp_path, monkeypatch):
    import sys
    script = tmp_path / "source/scripts/predict_toy_decisions.py"
    script.parent.mkdir(parents=True)
    script.write_text("class DecisionPredictor:\n    def __init__(self, *args, **kwargs):\n        self.options = kwargs\n")
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(set_num_threads=lambda count: None))
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("NANOJEV_CUDA_VISIBLE_DEVICES", "GPU-test")
    runtime = NanoRuntime(Settings(model_dir=str(tmp_path)))
    warmups = []
    monkeypatch.setattr(runtime, "infer", lambda payload: warmups.append(payload))
    runtime.load()
    assert len(warmups) == 1
    assert warmups[0]["model"] == "nanojev"
    assert runtime.predictor.options["disable_native_triton"] is False
    assert not (script.parent / "__pycache__").exists()


def test_nano_token_overflow_fails_before_inference():
    runtime = NanoRuntime(Settings())
    def too_long(*args):
        raise ValueError("request:q 候选路径为 3000 token，超过 max_length=2048；未截断输入")
    runtime.module = SimpleNamespace(prepare_examples=too_long)
    runtime.predictor = SimpleNamespace(tokenizer=None, predict=lambda _: pytest.fail("must not infer"))
    runtime.torch = SimpleNamespace(cuda=SimpleNamespace(OutOfMemoryError=MemoryError))
    with pytest.raises(InputTooLong):
        runtime.infer({"state": "test", "questions": {"q": {"type": "noul"}}})
