"""Chat request construction and narrowly scoped provider compatibility."""
from __future__ import annotations

import re
from typing import Any, Callable


class ThinkingParameterUnsupportedError(RuntimeError):
    """A safe compatibility signal; provider response bodies stay private."""

    def __init__(self, status_code: int) -> None:
        super().__init__("thinking parameter unsupported")
        self.status_code = status_code


def thinking_unsupported(exc: Exception) -> bool:
    """Retry only an explicit thinking-parameter rejection, never HTTP outages."""
    status = getattr(exc, "status_code", None)
    if status is not None and status not in {400, 422}:
        return False
    message = str(exc).casefold()
    unsupported = (
        r"(unsupported|unknown|unrecognized|not[- ]supported|not available|"
        r"does not support|unavailable|extra[- ]input|extra input|extra inputs|not permitted)"
    )
    return bool(
        re.search(rf"thinking[^.;\n]{{0,120}}{unsupported}", message)
        or re.search(rf"{unsupported}[^.;\n]{{0,120}}thinking", message)
    )


def create_chat_completion(
    create: Callable[..., Any],
    *,
    model: str,
    messages: list,
    max_tokens: int,
    disable_thinking: bool,
    response_format: dict[str, Any] | None = None,
    thinking_parameter: str = "thinking",
    temperature: float = 0,
) -> Any:
    """Use an injected SDK/HTTP adapter; each retry enters its scheduler again."""
    kwargs: dict[str, Any] = {
        "model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    extra = {}
    if disable_thinking:
        if thinking_parameter == "thinking":
            extra = {"thinking": {"type": "disabled"}}
        elif thinking_parameter == "enable_thinking":
            extra = {"enable_thinking": False}
        elif thinking_parameter != "none":
            raise ValueError("Unknown thinking parameter")
    try:
        return create(**kwargs, **({"extra_body": extra} if extra else {}))
    except Exception as exc:
        if not extra or not thinking_unsupported(exc):
            raise
        return create(**kwargs)
