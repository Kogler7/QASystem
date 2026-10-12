"""Bounded text/JSON HTTP adapter around the locally supplied official loader."""

from __future__ import annotations
from docker.configuration import getenv as config_getenv, required as config_required

import asyncio
import gc
import importlib.util
import json
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

LOGGER = logging.getLogger("clef_service")


@dataclass(frozen=True)
class Settings:
    model_dir: str = ""
    device: str = "cuda:0"
    dtype: str = "bfloat16"
    max_length: int = 2048
    max_request_bytes: int = 65536
    max_questions: int = 16
    max_options: int = 32
    max_total_options: int = 128
    max_queue_size: int = 4
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, prefix: str = "CLEF_") -> "Settings":
        values: dict[str, Any] = {}
        for name in cls.__dataclass_fields__:
            value = config_getenv(prefix + name.upper())
            if value is not None:
                if name.startswith("max_"):
                    try:
                        value = int(value)
                    except ValueError:
                        raise ValueError(f"{prefix}{name.upper()} must be an integer") from None
                values[name] = value
        settings = cls(**values)
        if any(getattr(settings, name) < 1 for name in cls.__dataclass_fields__ if name.startswith("max_")):
            raise ValueError("CLEF limits must be positive")
        if settings.dtype not in {"bfloat16", "float16", "float32"}:
            raise ValueError("CLEF_DTYPE must be bfloat16, float16, or float32")
        if not (settings.device == "cpu" or settings.device == "cuda" or settings.device.startswith("cuda:")):
            raise ValueError("CLEF_DEVICE must select one CPU or CUDA device")
        return settings


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Question(StrictModel):
    type: Literal["noul", "choice", "score"]
    instructions: str | None = None
    criteria: dict[str, str | None] | list[str | None] | None = None

    @model_validator(mode="after")
    def valid_criteria(self) -> "Question":
        if self.type == "noul":
            if self.criteria is not None and (
                not isinstance(self.criteria, dict) or not set(self.criteria) <= {"true", "false"}
            ):
                raise ValueError("noul criteria must use true and false keys")
        elif self.type == "choice":
            if not isinstance(self.criteria, dict) or not self.criteria or any(not key for key in self.criteria):
                raise ValueError("choice requires a nonempty option mapping")
        elif not isinstance(self.criteria, list) or not self.criteria:
            raise ValueError("score requires a nonempty option list")
        return self

    @property
    def option_count(self) -> int:
        return 2 if self.type == "noul" else len(self.criteria or ())


class SystemOneRequest(StrictModel):
    model: Literal["clef-flash", "nanojev"] = "clef-flash"
    state: JsonValue
    questions: dict[str, Question] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_json(self) -> "SystemOneRequest":
        # JsonValue permits nonfinite Python floats when validation starts from a dict.
        json.dumps(self.state, allow_nan=False)
        if any(not key for key in self.questions):
            raise ValueError("question IDs must be nonempty")
        return self


Probability = Annotated[float, Field(ge=0, le=1)]


class NoulAnswer(StrictModel):
    type: Literal["noul"]
    noul: Probability


class ChoiceAnswer(StrictModel):
    type: Literal["choice"]
    choice: str
    confidence: Probability
    probabilities: dict[str, Probability]


class ScoreAnswer(StrictModel):
    type: Literal["score"]
    score: Annotated[float, Field(ge=0)]
    confidence: Probability
    legend: dict[str, str | None]
    probabilities: dict[str, Probability]


class Usage(StrictModel):
    input_tokens: Annotated[int, Field(ge=1)]
    output_tokens: Literal[0]


class SystemOneResponse(StrictModel):
    model: Literal["clef-flash", "nanojev"]
    answers: dict[str, Annotated[NoulAnswer | ChoiceAnswer | ScoreAnswer, Field(discriminator="type")]]
    usage: Usage


class InputTooLong(Exception):
    """The complete encoded request exceeds the configured token budget."""


class ModelOutOfMemory(Exception):
    """An inference allocation failed; no request details are exposed."""


class LocalRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def load(self) -> None:
        directory = Path(self.settings.model_dir)
        if not self.settings.model_dir or not directory.is_dir() or not (directory / "joint_schema_model.py").is_file():
            raise RuntimeError("CLEF_MODEL_DIR must contain a local Clef release")
        # Apply isolation before importing torch, transformers, or the model module.
        if config_getenv("CLEF_CUDA_VISIBLE_DEVICES"):
            os.environ["CUDA_VISIBLE_DEVICES"] = config_required('CLEF_CUDA_VISIBLE_DEVICES')
            os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        # Importing the supplied loader must not create __pycache__ in the model directory.
        sys.dont_write_bytecode = True
        import torch

        self.torch = torch
        spec = importlib.util.spec_from_file_location("_clef_release_model", directory / "joint_schema_model.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("Unable to load the local Clef module")
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        spec.loader.exec_module(self.module)
        self.model, self.processor = self.module.load_release_model(
            directory, device=self.settings.device, dtype=getattr(torch, self.settings.dtype), local_files_only=True
        )
        self.infer({"model": "clef-flash", "state": "The sky is blue.", "questions": {
            "color": {"type": "choice", "instructions": "What color is the sky?",
                      "criteria": {"blue": "Blue", "red": "Red"}}}})

    def infer(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Official encode_record silently truncates state. Encode once without a
        # practical token cap to measure the COMPLETE input before allocating GPU tensors.
        encoded = self.module.encode_record(
            self.processor.tokenizer, payload, max_length=sys.maxsize, processor=self.processor
        )
        if len(encoded.input_ids) > self.settings.max_length:
            raise InputTooLong()
        out_of_memory = False
        try:
            return self.module.systemone(self.model, self.processor, payload, max_length=self.settings.max_length)
        except self.torch.cuda.OutOfMemoryError:
            out_of_memory = True
        # Leave the exception handler first so its traceback releases GPU tensors.
        if out_of_memory:
            gc.collect()
            self.torch.cuda.empty_cache()
            raise ModelOutOfMemory() from None
        raise RuntimeError("No inference result")

    def close(self) -> None:
        if hasattr(self, "model"):
            del self.model
        gc.collect()
        if hasattr(self, "torch") and self.torch.cuda.is_available():
            self.torch.cuda.empty_cache()


class BoundedBodyMiddleware:
    """Enforce authentication and byte limits before FastAPI parses request JSON."""

    def __init__(self, app: Any, settings: Settings) -> None:
        self.app, self.settings = app, settings

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] != "/v1/systemone":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        if self.settings.api_key:
            auth = [value for key, value in headers if key.lower() == b"authorization"]
            expected = ("Bearer " + self.settings.api_key).encode("utf-8")
            if len(auth) != 1 or not secrets.compare_digest(auth[0], expected):
                return await JSONResponse({"detail": "Unauthorized"}, status_code=401)(scope, receive, send)
        lengths = [value for key, value in headers if key.lower() == b"content-length"]
        if lengths:
            try:
                length = int(lengths[0])
            except ValueError:
                return await JSONResponse({"detail": "Invalid content length"}, status_code=400)(scope, receive, send)
            if length < 0 or len(lengths) != 1:
                return await JSONResponse({"detail": "Invalid content length"}, status_code=400)(scope, receive, send)
            if length > self.settings.max_request_bytes:
                return await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.settings.max_request_bytes:
                return await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        sent = False

        async def bounded_receive() -> dict[str, Any]:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return await self.app(scope, bounded_receive, send)


def validate_response(result: Any, payload: dict[str, Any], max_length: int) -> dict[str, Any]:
    response = SystemOneResponse.model_validate(result)
    if response.model != payload.get("model", "clef-flash") or set(response.answers) != set(payload["questions"]) or response.usage.input_tokens > max_length:
        raise ValueError("Invalid model response")
    for name, question in payload["questions"].items():
        answer = response.answers[name]
        if answer.type != question["type"]:
            raise ValueError("Invalid answer type")
        if isinstance(answer, (ChoiceAnswer, ScoreAnswer)):
            expected = set(question["criteria"]) if isinstance(answer, ChoiceAnswer) else {
                str(index) for index in range(len(question["criteria"]))
            }
            if set(answer.probabilities) != expected:
                raise ValueError("Invalid response options")
            if isinstance(answer, ChoiceAnswer) and answer.choice not in expected:
                raise ValueError("Invalid choice")
            if isinstance(answer, ScoreAnswer) and (
                answer.score > len(expected) - 1
                or answer.legend != {str(index): value for index, value in enumerate(question["criteria"])}
            ):
                raise ValueError("Invalid score")
            if abs(sum(answer.probabilities.values()) - 1.0) > max(0.001, len(expected) * 0.0001):
                raise ValueError("Invalid probabilities")
    return response.model_dump()


def create_app(
    settings: Settings | None = None, runtime_factory: Callable[[Settings], Any] = LocalRuntime,
    *, model_name: str = "clef-flash",
) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = runtime_factory(settings)
        app.state.ready = False
        app.state.queue = asyncio.Queue(maxsize=settings.max_queue_size)
        app.state.active = False
        LOGGER.info("Loading local %s model", model_name)
        try:
            await asyncio.to_thread(runtime.load)
        except Exception:
            LOGGER.error("%s model initialization failed", model_name)
            raise RuntimeError(f"{model_name} model initialization failed; verify the local release and environment") from None

        async def consume() -> None:
            while True:
                payload, future = await app.state.queue.get()
                try:
                    if future.cancelled():
                        continue
                    app.state.active = True
                    try:
                        result = await asyncio.to_thread(runtime.infer, payload)
                        result = validate_response(result, payload, settings.max_length)
                    except InputTooLong:
                        result = HTTPException(422, "Complete input exceeds CLEF_MAX_LENGTH; shorten state or questions")
                    except ModelOutOfMemory:
                        LOGGER.warning("%s inference ran out of GPU memory", model_name)
                        result = HTTPException(503, "Model memory capacity exceeded; shorten the request")
                    except Exception:
                        LOGGER.error("%s inference failed", model_name)
                        result = HTTPException(503, "Model inference unavailable")
                    if not future.done():
                        future.set_result(result)
                finally:
                    app.state.active = False
                    app.state.queue.task_done()

        worker = asyncio.create_task(consume())
        app.state.ready = True
        LOGGER.info("%s model ready", model_name)
        try:
            yield
        finally:
            app.state.ready = False
            await app.state.queue.join()
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            await asyncio.to_thread(runtime.close)

    app = FastAPI(title=f"{model_name} local inference", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ready = False
    app.add_middleware(BoundedBodyMiddleware, settings=settings)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        # Pydantic's default errors include the rejected input; never reflect user data.
        return JSONResponse({"detail": "Invalid text/JSON SystemOne request; images and videos are unsupported"}, status_code=422)

    @app.get("/health")
    async def health() -> JSONResponse:
        ready = app.state.ready
        return JSONResponse({"status": "ready" if ready else "unavailable", "model": model_name}, status_code=200 if ready else 503)

    @app.post("/v1/systemone")
    async def systemone(request: SystemOneRequest) -> dict[str, Any]:
        if request.model != model_name:
            raise HTTPException(422, "Model does not match this service")
        if not app.state.ready:
            raise HTTPException(503, "Model not ready")
        if len(request.questions) > settings.max_questions:
            raise HTTPException(422, "Too many questions")
        counts = [question.option_count for question in request.questions.values()]
        if model_name == "nanojev" and (min(counts) < 2 or any(
            q.type == "score" and q.option_count > 10 for q in request.questions.values()
        )):
            raise HTTPException(422, "NanoJev requires at least two options and at most ten score levels")
        if max(counts) > settings.max_options or sum(counts) > settings.max_total_options:
            raise HTTPException(422, "Too many options")
        future = asyncio.get_running_loop().create_future()
        try:
            app.state.queue.put_nowait((request.model_dump(), future))
        except asyncio.QueueFull:
            raise HTTPException(429, "Inference queue is full", headers={"Retry-After": "1"}) from None
        result = await future
        if isinstance(result, HTTPException):
            raise result
        return result

    return app


app = create_app()
