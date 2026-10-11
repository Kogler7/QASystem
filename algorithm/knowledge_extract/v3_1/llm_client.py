"""One explicit HTTP request, independently configured from earlier versions."""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

from ..v2.llm_client import OpenAICompatibleLLM, create_llm_client


class SingleRequestLLM(OpenAICompatibleLLM):
    """单次 HTTP 调用适配：不因 provider 能力差异做隐式重试或降级。

    该类原随实验版本 ``v3_2`` 提供，``v3_2`` 下线后移入本包，行为保持不变。
    """

    def _create(self, messages: Any, *, response_format: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = {
            "model": self.model, "messages": messages,
            "temperature": 0, "max_tokens": self.max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if self.disable_thinking:
            hostname = urlsplit(self.base_url).hostname
            payload["extra_body"] = (
                {"enable_thinking": False}
                if hostname in {"api.siliconflow.cn", "api.siliconflow.com"}
                else {"thinking": {"type": "disabled"}}
            )
        return self.client.chat.completions.create(**payload)


def thinking_disabled(value: bool | None = None) -> bool:
    if value is not None:
        return bool(value)
    raw = os.getenv("KG_EXTRACT_V31_DISABLE_THINKING", "1").strip().lower()
    if raw not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ValueError("KG_EXTRACT_V31_DISABLE_THINKING must be boolean")
    return raw in {"1", "true", "yes", "on"}


def create_client(*, disable_thinking: bool | None = None, **kwargs: Any) -> SingleRequestLLM:
    configured = create_llm_client(disable_thinking=thinking_disabled(disable_thinking), **kwargs)
    return SingleRequestLLM(configured.client, model=configured.model, max_tokens=configured.max_tokens,
                            disable_thinking=configured.disable_thinking, base_url=configured.base_url)
