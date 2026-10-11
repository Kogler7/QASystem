"""Local NanoJev decision client.

The published checkpoint is ``C-Tianyu/NanoJev`` (Qwen3-0.6B decision heads).
It answers a Jev-shaped ``state + questions`` request in one forward pass and
does not decode text.  Harness code in ``jev_harness`` decides what to ask.
"""
from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Sequence

from .config import get_jev_config


NANOJEV_REVISION = "unified-games-v1"
_PREDICTOR_RELATIVE = Path("source") / "scripts" / "predict_toy_decisions.py"
_TRAINER_RELATIVE = Path("source") / "scripts" / "train_toy_decisions.py"


def _binding_device() -> str:
    """Match embedding_runtime: CUDA only when this process is GPU-bound."""
    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    if visible and visible.lower() not in {"", "none", "no", "-1", "all"}:
        return "cuda"
    return "cpu"


def _has_native_triton() -> bool:
    """Released Torch versions may not include the predictor's optional API."""
    try:
        return importlib.util.find_spec("torch._native.triton_utils") is not None
    except ModuleNotFoundError as exc:
        if exc.name not in {"torch._native", "torch._native.triton_utils"}:
            raise
        return False


@dataclass(frozen=True)
class Judgment:
    """One normalized NanoJev answer."""

    choice: str
    probabilities: dict[str, float]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "choice": self.choice,
            "probabilities": dict(self.probabilities),
            "confidence": self.confidence,
        }


class JevUnavailableError(RuntimeError):
    """Raised when Jev is enabled but cannot be loaded or executed."""


class JevClient:
    """Protocol-like base.  ``states`` follows the NanoJev request item shape."""

    def predict(self, states: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Judgment]]:
        raise NotImplementedError


class CallableJevClient(JevClient):
    """Test double: inject the batched predict function."""

    def __init__(
        self,
        predict_fn: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Mapping[str, Judgment | Mapping[str, Any]]]],
    ) -> None:
        self._predict_fn = predict_fn

    def predict(self, states: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Judgment]]:
        raw = self._predict_fn(states)
        resolved: dict[str, dict[str, Judgment]] = {}
        for state_id, questions in raw.items():
            resolved[str(state_id)] = {
                str(question_id): _coerce_judgment(answer)
                for question_id, answer in questions.items()
            }
        return resolved


class NanoJevClient(JevClient):
    """Persistent ``DecisionPredictor``.  One ``predict`` batches every question."""

    def __init__(self, predictor: Any, *, batch_questions: int = 32) -> None:
        self._predictor = predictor
        self._batch_questions = batch_questions
        self._lock = RLock()

    @classmethod
    def from_pretrained(cls, model_path: str, *, batch_questions: int | None = None, precision: str | None = None) -> "NanoJevClient":
        root = Path(model_path)
        _require_bundle(root)
        if _binding_device() != "cuda":
            raise JevUnavailableError(
                "NanoJev 只能在已绑定 CUDA_VISIBLE_DEVICES 的 GPU worker 中加载"
            )
        predictor_cls = _load_predictor_class(root)
        config = get_jev_config()
        try:
            predictor = predictor_cls(
                str(root),
                device_name="cuda:0",
                precision=precision or config["precision"],
                disable_native_triton=_has_native_triton(),
            )
        except Exception as exc:  # noqa: BLE001 - surface checkpoint/CUDA failures as unavailable
            raise JevUnavailableError(f"NanoJev 加载失败: {exc}") from exc
        return cls(
            predictor,
            batch_questions=config["batch_questions"] if batch_questions is None else batch_questions,
        )

    def predict(self, states: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Judgment]]:
        payload_states = [dict(item) for item in states]
        if not payload_states:
            return {}
        with self._lock:
            raw = self._predictor.predict(
                {"states": payload_states},
                batch_questions=self._batch_questions,
                temperature=1.0,
            )
        return answers_from_nanojev(raw)


class RemoteNanoJevClient(JevClient):
    """Use one resident model shared by all queue workers."""

    def __init__(self):
        from external_services.nanojev import NanoJevServiceClient
        self.client = NanoJevServiceClient()

    def predict(self, states):
        resolved = {}
        for state in states:
            questions = {key: {**question, "type": "noul" if question["type"] == "boolean" else question["type"]}
                         for key, question in state["questions"].items()}
            body = self.client.systemone({"state": state["state"], "questions": questions})
            answers = {}
            for key, answer in body["answers"].items():
                if answer["type"] == "noul":
                    probability = answer["noul"]
                    answer = {"type": "boolean", "value": probability > .5,
                              "probabilities": {"true": probability, "false": 1 - probability}}
                answers[key] = _judgment_from_answer(answer)
            resolved[str(state["id"])] = answers
        return resolved


def answers_from_nanojev(raw: Mapping[str, Any]) -> dict[str, dict[str, Judgment]]:
    """Normalize a ``DecisionPredictor.predict`` payload into judgments."""
    states = raw.get("states")
    if not isinstance(states, list):
        raise JevUnavailableError("NanoJev 响应缺少 states")
    resolved: dict[str, dict[str, Judgment]] = {}
    for state in states:
        if not isinstance(state, dict):
            raise JevUnavailableError("NanoJev state 响应不是对象")
        state_id = str(state.get("id") or "")
        answers = state.get("answers")
        if not state_id or not isinstance(answers, dict):
            raise JevUnavailableError("NanoJev 响应缺少 state id 或 answers")
        resolved[state_id] = {
            str(question_id): _judgment_from_answer(answer)
            for question_id, answer in answers.items()
        }
    return resolved


def _judgment_from_answer(answer: Mapping[str, Any]) -> Judgment:
    probabilities = {
        str(key): float(value)
        for key, value in dict(answer.get("probabilities") or {}).items()
    }
    kind = str(answer.get("type") or "")
    if kind == "choice":
        choice = str(answer.get("choice") or answer.get("value") or "")
    elif kind == "boolean":
        choice = "true" if bool(answer.get("value")) else "false"
    else:
        raise JevUnavailableError(f"NanoJev 返回了未使用的题型: {kind or 'unknown'}")
    if choice not in probabilities and probabilities:
        raise JevUnavailableError(f"NanoJev 选择 {choice!r} 不在概率分布中")
    confidence = max(probabilities.values()) if probabilities else 0.0
    return Judgment(choice=choice, probabilities=probabilities, confidence=confidence)


def _coerce_judgment(raw: Judgment | Mapping[str, Any]) -> Judgment:
    if isinstance(raw, Judgment):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError(f"无法解析 Jev 判别结果: {type(raw)!r}")
    probabilities = {
        str(key): float(value)
        for key, value in dict(raw.get("probabilities") or {}).items()
    }
    choice = str(raw.get("choice") or "")
    if choice and probabilities and choice not in probabilities:
        probabilities[choice] = float(raw.get("confidence") or 0.0)
    confidence = float(raw.get("confidence") or (max(probabilities.values()) if probabilities else 0.0))
    return Judgment(choice=choice, probabilities=probabilities, confidence=confidence)


def jev_enabled() -> bool:
    return bool(get_jev_config()["enabled"])


def _require_bundle(root: Path) -> None:
    if not root.is_dir():
        raise JevUnavailableError(f"Jev 模型不存在: {root}")
    required = (
        root / "best.safetensors",
        root / "config.json",
        root / "backbone_config",
        root / "tokenizer",
        root / _PREDICTOR_RELATIVE,
        root / _TRAINER_RELATIVE,
    )
    missing = [str(path.relative_to(root)) for path in required if not path.exists()]
    if missing:
        raise JevUnavailableError(
            "NanoJev checkpoint 不完整，需要 best.safetensors、config.json、"
            f"backbone_config、tokenizer 和 source/scripts。缺少: {', '.join(missing)}"
        )


def _load_predictor_class(root: Path) -> type:
    script = root / _PREDICTOR_RELATIVE
    spec = importlib.util.spec_from_file_location("nanojev_predict_toy_decisions", script)
    if spec is None or spec.loader is None:
        raise JevUnavailableError(f"无法加载 NanoJev 推理脚本: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    predictor = getattr(module, "DecisionPredictor", None)
    if predictor is None:
        raise JevUnavailableError("NanoJev 推理脚本缺少 DecisionPredictor")
    return predictor


def create_jev_client(*, model_path: str | None = None) -> JevClient:
    """Load NanoJev.  Callers must only do this when Jev is enabled."""
    from algorithm.runtime_profile import is_remote_only

    if model_path is None and os.getenv("NANOJEV_SERVICE_URL", "").strip():
        return RemoteNanoJevClient()
    if is_remote_only():
        raise JevUnavailableError("remote-only 模式禁止加载本地 NanoJev")
    resolved_path = model_path or str(get_jev_config()["model_path"])
    if not Path(resolved_path).is_dir():
        raise JevUnavailableError(f"Jev 模型不存在: {resolved_path}")
    return NanoJevClient.from_pretrained(resolved_path)


@lru_cache(maxsize=1)
def _load_jev_client() -> JevClient:
    return create_jev_client()


def get_jev_client() -> JevClient | None:
    if not jev_enabled():
        return None
    return _load_jev_client()


def reset_jev_client_cache() -> None:
    _load_jev_client.cache_clear()


__all__ = [
    "CallableJevClient",
    "JevClient",
    "JevUnavailableError",
    "Judgment",
    "NANOJEV_REVISION",
    "NanoJevClient",
    "answers_from_nanojev",
    "create_jev_client",
    "get_jev_client",
    "jev_enabled",
    "reset_jev_client_cache",
]
