"""Local Jev-shaped judgment client (Choice / Noul).

Official TypeSafe Jev weights are not public.  This adapter loads a Hugging Face
encoder from ``KG_EXTRACT_JEV_MODEL_PATH`` (mounted at ``/models``) and exposes
the same two primitives the extraction pipeline needs: yes/no probability and a
closed-set choice with a confidence.  It never generates text.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Sequence

from .config import get_jev_config


def _binding_device() -> str:
    """Match embedding_runtime: only use CUDA when this process is GPU-bound."""
    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    if visible and visible.lower() not in {"", "none", "no", "-1", "all"}:
        return "cuda"
    return "cpu"


@dataclass(frozen=True)
class ChoiceAnswer:
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
    """Raised when Jev is enabled but cannot be loaded."""


class JevClient:
    """Protocol-like base for local or injected judgment backends."""

    def noul(self, state: str, question: str) -> float:
        raise NotImplementedError

    def choice(self, state: str, question: str, options: Sequence[str]) -> ChoiceAnswer:
        raise NotImplementedError


class CallableJevClient(JevClient):
    """Test double: inject noul/choice callables."""

    def __init__(
        self,
        *,
        noul_fn: Callable[[str, str], float] | None = None,
        choice_fn: Callable[[str, str, Sequence[str]], ChoiceAnswer | dict[str, Any] | str] | None = None,
    ) -> None:
        self._noul_fn = noul_fn
        self._choice_fn = choice_fn

    def noul(self, state: str, question: str) -> float:
        if self._noul_fn is None:
            raise JevUnavailableError("noul 未注入")
        return _clamp_probability(self._noul_fn(state, question))

    def choice(self, state: str, question: str, options: Sequence[str]) -> ChoiceAnswer:
        resolved = list(options)
        if not resolved:
            raise ValueError("choice 至少需要一个选项")
        if self._choice_fn is None:
            raise JevUnavailableError("choice 未注入")
        raw = self._choice_fn(state, question, resolved)
        return _coerce_choice(raw, resolved)


class LocalJevClient(JevClient):
    """Score options with a local HF encoder; one forward pass per option."""

    def __init__(
        self,
        *,
        scorer: Callable[[str], float] | None = None,
        model: Any = None,
        tokenizer: Any = None,
        device: str = "cpu",
    ) -> None:
        self._scorer = scorer
        self._model = model
        self._tokenizer = tokenizer
        self._device = device
        self._lock = RLock()

    @classmethod
    def from_pretrained(cls, model_path: str) -> "LocalJevClient":
        from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer

        path = Path(model_path)
        tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True)
        device = _binding_device()
        try:
            model = AutoModelForSequenceClassification.from_pretrained(str(path), local_files_only=True)
        except Exception:  # noqa: BLE001 - custom heads fall back to the encoder
            model = AutoModel.from_pretrained(str(path), local_files_only=True)
        model.eval()
        if device == "cuda":
            model = model.to(device)
        return cls(model=model, tokenizer=tokenizer, device=device)

    def noul(self, state: str, question: str) -> float:
        yes_logit = self._option_logit(_render(state, question, "yes"))
        no_logit = self._option_logit(_render(state, question, "no"))
        return _softmax_pair(yes_logit, no_logit)

    def choice(self, state: str, question: str, options: Sequence[str]) -> ChoiceAnswer:
        resolved = [str(item) for item in options]
        if not resolved:
            raise ValueError("choice 至少需要一个选项")
        logits = [self._option_logit(_render(state, question, option)) for option in resolved]
        probabilities = _softmax(logits)
        ranking = sorted(zip(resolved, probabilities), key=lambda item: item[1], reverse=True)
        winner, confidence = ranking[0]
        return ChoiceAnswer(
            choice=winner,
            probabilities={name: value for name, value in zip(resolved, probabilities)},
            confidence=confidence,
        )

    def _option_logit(self, text: str) -> float:
        if self._scorer is not None:
            return float(self._scorer(text))
        if self._model is None or self._tokenizer is None:
            raise JevUnavailableError("Jev 模型未加载")
        import torch

        encoded = self._tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=512,
        )
        encoded = {key: value.to(self._device) for key, value in encoded.items()}
        with self._lock:
            with torch.no_grad():
                output = self._model(**encoded)
        logits = getattr(output, "logits", None)
        if logits is not None:
            row = logits[0]
            if row.numel() == 1:
                return float(row.item())
            probs = torch.softmax(row.float(), dim=-1)
            return float(probs[-1].item())
        hidden = output.last_hidden_state[0, 0]
        return float(hidden.mean().item())


def _render(state: str, question: str, option: str) -> str:
    return f"State: {state}\nQuestion: {question}\nAnswer: {option}"


def _clamp_probability(value: Any) -> float:
    number = float(value)
    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return number


def _softmax(logits: Sequence[float]) -> list[float]:
    if not logits:
        return []
    peak = max(logits)
    exps = [math.exp(value - peak) for value in logits]
    total = sum(exps) or 1.0
    return [value / total for value in exps]


def _softmax_pair(yes_logit: float, no_logit: float) -> float:
    return _softmax([yes_logit, no_logit])[0]


def _coerce_choice(raw: Any, options: Sequence[str]) -> ChoiceAnswer:
    if isinstance(raw, ChoiceAnswer):
        return raw
    if isinstance(raw, str):
        label = raw if raw in options else options[0]
        probabilities = {item: (1.0 if item == label else 0.0) for item in options}
        return ChoiceAnswer(choice=label, probabilities=probabilities, confidence=probabilities[label])
    if isinstance(raw, dict):
        label = str(raw.get("choice") or raw.get("verdict") or "")
        if label not in options:
            label = options[0]
        raw_probs = raw.get("probabilities") or {}
        if isinstance(raw_probs, dict) and raw_probs:
            probabilities = {item: _clamp_probability(raw_probs.get(item, 0.0)) for item in options}
            total = sum(probabilities.values()) or 1.0
            probabilities = {item: value / total for item, value in probabilities.items()}
        else:
            probabilities = {item: (1.0 if item == label else 0.0) for item in options}
        confidence = _clamp_probability(raw.get("confidence", probabilities.get(label, 0.0)))
        return ChoiceAnswer(choice=label, probabilities=probabilities, confidence=confidence)
    raise TypeError(f"无法解析 Jev choice 结果: {type(raw)!r}")


def jev_enabled() -> bool:
    return bool(get_jev_config()["enabled"])


def _model_present(model_path: str) -> bool:
    path = Path(model_path)
    if path.is_file():
        return path.stat().st_size > 0
    if not path.is_dir():
        return False
    try:
        return any(path.iterdir())
    except OSError:
        return False


def create_jev_client(*, model_path: str | None = None) -> JevClient:
    """Load the local classifier.  Callers must only do this when Jev is enabled."""
    from algorithm.runtime_profile import is_remote_only

    config = get_jev_config()
    if is_remote_only():
        raise JevUnavailableError("remote-only 模式禁止加载本地 Jev 模型")
    resolved_path = model_path or str(config["model_path"])
    if not _model_present(resolved_path):
        raise JevUnavailableError(f"Jev 模型不存在: {resolved_path}")
    return LocalJevClient.from_pretrained(resolved_path)


@lru_cache(maxsize=1)
def _load_jev_client() -> JevClient:
    return create_jev_client()


def get_jev_client() -> JevClient | None:
    if not jev_enabled():
        return None
    return _load_jev_client()


def reset_jev_client_cache() -> None:
    _load_jev_client.cache_clear()


def default_entity_adjudicator(payload: dict[str, Any]) -> str:
    """Choice over same_entity / different_entity / uncertain for EntityResolver."""
    client = get_jev_client()
    if client is None:
        return "uncertain"
    config = get_jev_config()
    options = [str(item) for item in (payload.get("allowed_verdicts") or [])]
    if not options:
        options = ["same_entity", "different_entity", "uncertain"]
    state = json.dumps(
        {
            "type": payload.get("type"),
            "left": payload.get("left"),
            "right": payload.get("right"),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    answer = client.choice(
        state,
        "Are these two mentions the same entity of the given type?",
        options,
    )
    if answer.choice not in options:
        return "uncertain"
    if answer.choice == "uncertain" or answer.confidence < config["choice_confidence"]:
        return "uncertain"
    return answer.choice


def classify_relation_pair(
    client: JevClient,
    *,
    chunk_text: str,
    pair: dict[str, Any],
) -> dict[str, Any]:
    """Noul: does this chunk support the ontology-signed pair?"""
    config = get_jev_config()
    state = (
        f"chunk: {chunk_text}\n"
        f"source: {pair.get('source_name')} ({pair.get('source_type')})\n"
        f"target: {pair.get('target_name')} ({pair.get('target_type')})\n"
        f"relation: {pair.get('relation_id')}"
    )
    question = "Does the current chunk contain enough evidence for this relation?"
    p_yes = client.noul(state, question)
    if p_yes >= config["noul_accept"]:
        decision = "accept"
    elif p_yes <= config["noul_reject"]:
        decision = "reject"
    else:
        decision = "gray"
    return {
        "pair_id": pair.get("pair_id"),
        "p_yes": p_yes,
        "decision": decision,
        "source": "jev",
        "relation_id": pair.get("relation_id"),
    }


def partition_relation_pairs(
    pairs: Iterable[dict[str, Any]],
    *,
    chunk_text: str,
    client: JevClient | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split pairs into accepted / rejected / gray using Jev noul."""
    resolved_client = client if client is not None else get_jev_client()
    if resolved_client is None:
        return [], [], list(pairs)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    gray: list[dict[str, Any]] = []
    for pair in pairs:
        judgment = classify_relation_pair(resolved_client, chunk_text=chunk_text, pair=pair)
        labeled = {**pair, "judgment": judgment}
        decision = judgment["decision"]
        if decision == "accept":
            accepted.append(labeled)
        elif decision == "reject":
            rejected.append(labeled)
        else:
            gray.append(labeled)
    return accepted, rejected, gray


__all__ = [
    "CallableJevClient",
    "ChoiceAnswer",
    "JevClient",
    "JevUnavailableError",
    "LocalJevClient",
    "classify_relation_pair",
    "create_jev_client",
    "default_entity_adjudicator",
    "get_jev_client",
    "jev_enabled",
    "partition_relation_pairs",
    "reset_jev_client_cache",
]
