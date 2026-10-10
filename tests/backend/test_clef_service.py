"""HTTP contracts for the isolated service; no model weights or torch required."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from clef_service.app import InputTooLong, LocalRuntime, ModelOutOfMemory, Settings, create_app


def payload():
    return {
        "model": "clef-flash",
        "state": {"ticket": "Checkout is broken"},
        "questions": {
            "outage": {"type": "noul", "instructions": "Is the service down?"},
            "department": {"type": "choice", "criteria": {"billing": "Payments", "technical": "Bugs"}},
            "urgency": {"type": "score", "criteria": ["Later", "Today"]},
        },
    }


class FakeRuntime:
    def __init__(self):
        self.loads = 0
        self.closes = 0
        self.calls = []
        self.error = None

    def load(self):
        self.loads += 1

    def close(self):
        self.closes += 1

    def infer(self, body):
        self.calls.append(body)
        if self.error is not None:
            raise self.error
        answers = {}
        for key, question in body["questions"].items():
            if question["type"] == "noul":
                answers[key] = {"type": "noul", "noul": 0.9}
            else:
                criteria = question["criteria"]
                options = list(criteria) if question["type"] == "choice" else [str(i) for i in range(len(criteria))]
                probabilities = dict.fromkeys(options, 1 / len(options))
                answers[key] = {"type": question["type"], "confidence": probabilities[options[0]], "probabilities": probabilities}
                if question["type"] == "choice":
                    answers[key]["choice"] = options[0]
                else:
                    answers[key]["score"] = (len(criteria) - 1) / 2
                    answers[key]["legend"] = dict(zip(options, criteria))
        return {"model": "clef-flash", "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 0}}


def service(runtime=None, **settings):
    runtime = runtime or FakeRuntime()
    app = create_app(Settings(**settings), lambda _: runtime)
    return app, runtime


def test_load_once_health_and_official_request_contract():
    app, runtime = service(model_dir="/private/model/location")
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ready", "model": "clef-flash"}
        assert "private" not in health.text
        for _ in range(2):
            response = client.post("/v1/systemone", json=payload())
            assert response.status_code == 200
            assert set(response.json()["answers"]) == {"outage", "department", "urgency"}
        assert runtime.loads == 1
    assert runtime.closes == 1


@pytest.mark.parametrize("state", [None, "hello", {"a": 1}, [1, "x"], True, 1])
def test_json_state_including_null_is_preserved(state):
    app, runtime = service()
    body = payload()
    body["state"] = state
    with TestClient(app) as client:
        assert client.post("/v1/systemone", json=body).status_code == 200
    assert runtime.calls[0]["state"] == state


@pytest.mark.parametrize("field,value", [("images", []), ("videos", ["https://private"]), ("media_kwargs", {}), ("model", "remote/model")])
def test_rejects_media_extra_fields_and_other_models(field, value):
    app, runtime = service()
    body = payload()
    body[field] = value
    with TestClient(app) as client:
        response = client.post("/v1/systemone", json=body)
    assert response.status_code == 422
    assert "private" not in response.text
    assert not runtime.calls


@pytest.mark.parametrize("question", [
    {"type": "choice", "criteria": []},
    {"type": "choice", "criteria": {}},
    {"type": "score", "criteria": {"a": "b"}},
    {"type": "score", "criteria": []},
    {"type": "noul", "criteria": {"yes": "secret-state"}},
    {"type": "other"},
])
def test_question_shape_validation_sanitizes_errors(question):
    app, runtime = service()
    body = payload()
    body["questions"] = {"secret-question": question}
    with TestClient(app) as client:
        response = client.post("/v1/systemone", json=body)
    assert response.status_code == 422
    assert "secret" not in response.text
    assert not runtime.calls


@pytest.mark.parametrize("settings", [{"max_questions": 2}, {"max_options": 1}, {"max_total_options": 5}])
def test_question_and_option_limits(settings):
    app, runtime = service(**settings)
    with TestClient(app) as client:
        assert client.post("/v1/systemone", json=payload()).status_code == 422
    assert not runtime.calls


def test_body_limit_and_invalid_json_do_not_echo_input():
    app, runtime = service(max_request_bytes=1024)
    with TestClient(app) as client:
        response = client.post("/v1/systemone", content=b"secret" * 200)
        assert response.status_code == 413
        invalid = client.post("/v1/systemone", content=b'{"secret":')
        assert invalid.status_code == 422
        assert "secret" not in invalid.text
    assert not runtime.calls


def test_streamed_body_size_limit_without_content_length():
    app, runtime = service(max_request_bytes=32)

    async def run():
        async def chunks():
            yield b"a" * 20
            yield b"b" * 20
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/v1/systemone", content=chunks())
                assert response.status_code == 413
    asyncio.run(run())
    assert not runtime.calls


def test_bearer_auth_and_health_dont_leak_secret(caplog):
    app, runtime = service(api_key="private-api-key")
    assert "private-api-key" not in repr(Settings(api_key="private-api-key"))
    with caplog.at_level(logging.INFO), TestClient(app) as client:
        assert client.get("/health").status_code == 200
        for header in [None, "Bearer wrong-secret", "Basic private-api-key"]:
            response = client.post("/v1/systemone", json=payload(), headers={"Authorization": header} if header else {})
            assert response.status_code == 401
            assert "secret" not in response.text and "private" not in response.text
        assert client.post("/v1/systemone", json=payload(), headers={"Authorization": "Bearer private-api-key"}).status_code == 200
    assert "private-api-key" not in caplog.text and "Checkout" not in caplog.text
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("error,status", [(InputTooLong(), 422), (ModelOutOfMemory(), 503), (RuntimeError("private-state and Authorization"), 503)])
def test_inference_errors_are_sanitized(error, status, caplog):
    app, runtime = service()
    runtime.error = error
    with caplog.at_level(logging.INFO), TestClient(app) as client:
        response = client.post("/v1/systemone", json=payload())
        assert response.status_code == status
        assert "private" not in response.text and "Authorization" not in response.text
        assert client.get("/health").status_code == 200
    assert "private" not in caplog.text and "Authorization" not in caplog.text


@pytest.mark.parametrize("mutation", [
    lambda result: result["answers"]["outage"].update(noul=float("nan")),
    lambda result: result["answers"]["outage"].update(noul=float("inf")),
    lambda result: result.update(debug="private-local-path"),
    lambda result: result["answers"]["department"].update(choice="nonexistent"),
    lambda result: result["answers"]["department"]["probabilities"].update(billing=0.9),
    lambda result: result["usage"].update(input_tokens=2049),
])
def test_invalid_or_nonfinite_outputs_fail_closed(mutation):
    runtime = FakeRuntime()
    original = runtime.infer
    def infer(body):
        result = original(body)
        mutation(result)
        return result
    runtime.infer = infer
    app, _ = service(runtime)
    with TestClient(app) as client:
        response = client.post("/v1/systemone", json=payload())
    assert response.status_code == 503
    assert "private" not in response.text


def test_queue_is_bounded_and_serial_and_health_remains_responsive():
    runtime = FakeRuntime()
    started, release = threading.Event(), threading.Event()
    original = runtime.infer
    def infer(body):
        started.set()
        assert release.wait(5)
        return original(body)
    runtime.infer = infer
    app, _ = service(runtime, max_queue_size=1)

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.post("/v1/systemone", json=payload()))
                assert await asyncio.to_thread(started.wait, 5)
                second = asyncio.create_task(client.post("/v1/systemone", json=payload()))
                for _ in range(100):
                    if app.state.queue.qsize() == 1:
                        break
                    await asyncio.sleep(0.001)
                try:
                    overloaded = await client.post("/v1/systemone", json=payload())
                    assert overloaded.status_code == 429
                    assert overloaded.headers["Retry-After"] == "1"
                    assert (await client.get("/health")).status_code == 200
                    assert len(runtime.calls) == 0
                finally:
                    release.set()
                results = await asyncio.gather(first, second)
                assert [result.status_code for result in results] == [200, 200]
    asyncio.run(run())
    assert len(runtime.calls) == 2


def test_unready_service():
    app, _ = service()
    with TestClient(app) as client:
        app.state.ready = False
        assert client.get("/health").status_code == 503
        assert client.post("/v1/systemone", json=payload()).status_code == 503


def test_preflight_counts_full_tokens_before_official_truncation():
    runtime = LocalRuntime(Settings(max_length=2048))
    calls = []
    def encode(*args, **kwargs):
        assert kwargs["max_length"] == sys.maxsize
        return types.SimpleNamespace(input_ids=range(2049))
    runtime.module = types.SimpleNamespace(encode_record=encode, systemone=lambda *a, **kw: calls.append(kw))
    runtime.processor = types.SimpleNamespace(tokenizer=object())
    with pytest.raises(InputTooLong):
        runtime.infer(payload())
    assert not calls


def test_runtime_oom_releases_cache(monkeypatch):
    class FakeOOM(Exception):
        pass
    cleaned = []
    runtime = LocalRuntime(Settings())
    runtime.torch = types.SimpleNamespace(cuda=types.SimpleNamespace(OutOfMemoryError=FakeOOM, empty_cache=lambda: cleaned.append(True)))
    runtime.model = object()
    runtime.processor = types.SimpleNamespace(tokenizer=object())
    def infer(*args, **kwargs):
        raise FakeOOM("secret")
    runtime.module = types.SimpleNamespace(encode_record=lambda *a, **kw: types.SimpleNamespace(input_ids=[1]), systemone=infer)
    with pytest.raises(ModelOutOfMemory):
        runtime.infer(payload())
    assert cleaned == [True]


def test_loader_requires_local_directory_before_importing_model(tmp_path):
    runtime = LocalRuntime(Settings(model_dir=str(tmp_path / "missing")))
    with pytest.raises(RuntimeError, match="local Clef release"):
        runtime.load()


def test_loader_is_local_only_and_applies_cuda_before_torch(tmp_path, monkeypatch):
    # A miniature external loader proves no source is copied or downloaded.
    (tmp_path / "joint_schema_model.py").write_text(
        "import os\n"
        "def load_release_model(path, **kwargs):\n"
        "    assert os.environ['CUDA_VISIBLE_DEVICES'] == 'GPU-unit-test'\n"
        "    assert os.environ['HF_HUB_OFFLINE'] == '1'\n"
        "    assert os.environ['TRANSFORMERS_OFFLINE'] == '1'\n"
        "    assert kwargs['local_files_only'] is True\n"
        "    assert kwargs['device'] == 'cuda:0'\n"
        "    return 'loaded-model', 'loaded-processor'\n"
    )
    for key in ["HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER"]:
        monkeypatch.setenv(key, os.environ.get(key, ""))
    monkeypatch.setenv("CLEF_CUDA_VISIBLE_DEVICES", "GPU-unit-test")
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(bfloat16="bf16"))
    monkeypatch.setitem(sys.modules, "_clef_release_model", None)
    runtime = LocalRuntime(Settings(model_dir=str(tmp_path)))
    runtime.load()
    assert runtime.model == "loaded-model"
    assert runtime.processor == "loaded-processor"
    assert not (tmp_path / "__pycache__").exists()


def test_settings_from_environment(monkeypatch):
    monkeypatch.setenv("CLEF_MAX_LENGTH", "1024")
    monkeypatch.setenv("CLEF_MODEL_DIR", "/models/clef-flash")
    assert Settings.from_env().max_length == 1024
    monkeypatch.setenv("CLEF_MAX_LENGTH", "0")
    with pytest.raises(ValueError, match="positive"):
        Settings.from_env()
