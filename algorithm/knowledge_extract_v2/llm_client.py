"""Live LLM 适配器：把 OpenAI 兼容端点封装成 ``llm(prompt) -> str``。

仅在调用 :func:`create_llm_client` 时才导入 openai，因此测试/无网络环境
不需要安装 openai 也能导入本模块与 split_extractor。
"""
from __future__ import annotations

from typing import Any, Callable

from .config import get_llm_config


def _unsupported_parameter(exc: Exception) -> bool:
    if isinstance(exc, TypeError):
        return True
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "thinking",
            "response_format",
            "json_object",
            "unsupported parameter",
            "unknown parameter",
        )
    )


def create_llm_client(
    *,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    max_tokens: int | None = None,
    disable_thinking: bool | None = None,
) -> Callable[[str], str]:
    """按 config 环境变量构建 OpenAI 兼容客户端，返回 ``prompt -> text``。"""
    from openai import OpenAI  # lazy import：仅 live 调用时才需要

    config = get_llm_config()
    client = OpenAI(
        base_url=base_url or config["base_url"],
        api_key=api_key or config["api_key"] or "not-needed",
    )
    resolved_model = model or config["model"]
    resolved_tokens = max_tokens or config["max_tokens"]
    thinking_disabled = config["disable_thinking"] if disable_thinking is None else disable_thinking

    def call(prompt: str) -> str:
        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": resolved_tokens,
        }
        try:
            if thinking_disabled:
                response = client.chat.completions.create(**kwargs, thinking={"type": "disabled"})
            else:
                response = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 后端不支持 thinking 时回退重试
            if thinking_disabled and _unsupported_parameter(exc):
                response = client.chat.completions.create(**kwargs)
            else:
                raise
        choices = getattr(response, "choices", None) or []
        return choices[0].message.content or "" if choices else ""

    return call


__all__ = ["create_llm_client"]
