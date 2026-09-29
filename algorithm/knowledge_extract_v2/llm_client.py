"""Live LLM adapter with native Structured Outputs when available.

仅在调用 :func:`create_llm_client` 时才导入 openai，因此测试/无网络环境
不需要安装 openai 也能导入本模块与 split_extractor。
"""
from __future__ import annotations

import re
from typing import Any

from ._logging import get_step_logger
from .config import get_llm_config
from .prompt_router import ChatMessages


logger = get_step_logger(__name__)


# DeepSeek's Responses strict-schema implementation intentionally supports a
# smaller JSON Schema subset than the generic OpenAI-compatible surface.  Do
# this check before making the request so an ordinary provider error is never
# mistaken for a capability downgrade.
_DEEPSEEK_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {"minLength", "maxLength", "minItems", "maxItems"}
)


def _deepseek_strict_schema_compatible(schema: Any) -> bool:
    """Return whether *schema* is expressible by DeepSeek strict outputs."""
    if not isinstance(schema, dict):
        return False
    if any(key in schema for key in _DEEPSEEK_UNSUPPORTED_SCHEMA_KEYWORDS):
        return False

    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties")
        # Strict mode cannot express a free-form/empty object.
        if not isinstance(properties, dict) or not properties:
            return False
        if schema.get("additionalProperties") is not False:
            return False
        required = schema.get("required")
        if not isinstance(required, list) or set(required) != set(properties):
            return False
        return all(_deepseek_strict_schema_compatible(value) for value in properties.values())

    if schema_type == "array":
        return _deepseek_strict_schema_compatible(schema.get("items"))

    # Traverse composition/conditional branches when present.  This keeps the
    # gate generic without treating scalar schemas as incompatible.
    for key in ("anyOf", "allOf", "oneOf"):
        branches = schema.get(key)
        if branches is not None and (
            not isinstance(branches, list)
            or not all(_deepseek_strict_schema_compatible(branch) for branch in branches)
        ):
            return False
    return True


def _is_deepseek_model(model: str) -> bool:
    return model.casefold().startswith("deepseek")


def _is_deepseek_v4_flash(model: str) -> bool:
    """MaaS 上的 deepseek-v4-flash / -0731 在 Responses 路径上输出不稳定。"""
    return model.casefold().startswith("deepseek-v4-flash")


def _unsupported_parameter(exc: Exception) -> bool:
    message = str(exc).casefold()
    parameter = r"(thinking|response_format|json_schema|json_object|responses? API|client\.responses)"
    unsupported = (
        r"(unsupported|unknown|unrecognized|not[- ]supported|"
        r"unavailable|extra[- ]input|extra input|extra inputs|not permitted)"
    )
    return bool(
        re.search(rf"{parameter}[^.;\n]{{0,120}}{unsupported}", message)
        or re.search(rf"{unsupported}[^.;\n]{{0,120}}{parameter}", message)
    )


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read an SDK field from either a model object or a plain mapping."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class StructuredLLMResponse:
    """Internal structured response envelope with string-like compatibility."""

    def __init__(
        self,
        *,
        text: str,
        api: str,
        response_mode: str,
        status: str,
        finish_reason: str | None = None,
        incomplete_details: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        usage_unavailable: bool = False,
    ) -> None:
        self.text = text or ""
        self.api = api
        self.response_mode = response_mode
        self.status = status
        self.finish_reason = finish_reason
        self.incomplete_details = dict(incomplete_details or {})
        self.usage = dict(usage or {}) if usage is not None else None
        self.usage_unavailable = usage is None or usage_unavailable

    def __str__(self) -> str:
        return self.text

    def __len__(self) -> int:
        return len(self.text)

    def __bool__(self) -> bool:
        return bool(self.text)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.text == other
        return super().__eq__(other)

    def strip(self, *args: Any, **kwargs: Any) -> str:
        return self.text.strip(*args, **kwargs)

    def to_trace(self) -> dict[str, Any]:
        return {
            "text_characters": len(self.text),
            "api": self.api,
            "response_mode": self.response_mode,
            "status": self.status,
            "finish_reason": self.finish_reason,
            "incomplete_details": dict(self.incomplete_details),
            "usage": self.usage,
            "usage_unavailable": self.usage_unavailable,
        }


def _usage_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return dict(usage)
    model_dump = getattr(usage, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict):
            return dumped
    result: dict[str, Any] = {}
    for name in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
    ):
        value = getattr(usage, name, None)
        if value is not None:
            result[name] = value
    return result or None


def _incomplete_details_dict(details: Any) -> dict[str, Any] | None:
    if details is None:
        return None
    if isinstance(details, dict):
        return dict(details)
    model_dump = getattr(details, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict):
            return dumped
    result: dict[str, Any] = {}
    for name in ("reason", "message"):
        value = getattr(details, name, None)
        if value is not None:
            result[name] = value
    return result or None


class OpenAICompatibleLLM:
    """OpenAI-compatible chat adapter plus ``generate_structured``."""

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        max_tokens: int,
        disable_thinking: bool,
    ) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.disable_thinking = disable_thinking

    def _create(self, messages: ChatMessages, *, response_format: dict[str, Any] | None = None) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        logger.info(
            "[步骤][LLM-Client] 请求: model=%s messages=%d prompt=%d字符 max_tokens=%s response_format=%s thinking=%s",
            self.model,
            len(messages),
            sum(len(str(item.get("content") or "")) for item in messages),
            self.max_tokens,
            (response_format or {}).get("type") if isinstance(response_format, dict) else None,
            "disabled" if self.disable_thinking else "default",
        )
        try:
            if self.disable_thinking:
                response = self.client.chat.completions.create(
                    **kwargs,
                    # OpenAI-compatible SDKs forward provider-specific fields
                    # through ``extra_body``.  DeepSeek V4 documents the
                    # thinking toggle in this location rather than as a
                    # top-level SDK argument.
                    extra_body={"thinking": {"type": "disabled"}},
                )
            else:
                response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - compatible providers vary
            if not self.disable_thinking or not _unsupported_parameter(exc):
                raise
            logger.info("[步骤][LLM-Client] thinking 参数不被支持，改用默认思考模式重试")
            response = self.client.chat.completions.create(**kwargs)
        logger.info(
            "[步骤][LLM-Client] 响应: id=%s finish_reason=%s usage=%s 文本=%d字符",
            _field(response, "id"),
            self._chat_finish_reason(response),
            _usage_dict(_field(response, "usage")),
            len(self._chat_text(response) or ""),
        )
        return response

    @staticmethod
    def _chat_text(response: Any) -> str:
        choices = _field(response, "choices") or []
        if not choices:
            return ""
        message = _field(choices[0], "message")
        content = _field(message, "content")
        return content or ""

    @staticmethod
    def _chat_finish_reason(response: Any) -> str | None:
        choices = _field(response, "choices") or []
        if not choices:
            return None
        return _field(choices[0], "finish_reason")

    @staticmethod
    def _responses_text(response: Any) -> str:
        output_text = _field(response, "output_text")
        if isinstance(output_text, str):
            return output_text
        chunks: list[str] = []
        for output in _field(response, "output") or []:
            for content in _field(output, "content") or []:
                text = _field(content, "text")
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks)

    @staticmethod
    def _response_status(response: Any, *, text: str, finish_reason: str | None = None) -> str:
        status = str(_field(response, "status", "") or "").casefold()
        if status == "incomplete" or finish_reason == "length":
            return "incomplete"
        if not text:
            return "empty"
        return status or "completed"

    def _responses_create(
        self,
        messages: ChatMessages,
        *,
        response_format: dict[str, Any],
    ) -> Any:
        responses = getattr(self.client, "responses", None)
        create = getattr(responses, "create", None)
        if not callable(create):
            raise RuntimeError("responses API is unavailable: client.responses.create missing")
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": messages,
            "temperature": 0,
            "max_output_tokens": self.max_tokens,
            "text": {"format": response_format},
        }
        # disable_thinking=True 时直接省略 reasoning 字段即可关闭思考：
        # 部分 OpenAI 兼容的 Responses 实现会拒绝 reasoning 参数，
        # 不下发它比先下发再回退更稳妥。
        return create(**kwargs)

    def _chat_structured(
        self,
        messages: ChatMessages,
        *,
        response_mode: str,
        response_format: dict[str, Any] | None = None,
    ) -> StructuredLLMResponse:
        response = self._create(messages, response_format=response_format)
        text = self._chat_text(response)
        finish_reason = self._chat_finish_reason(response)
        return StructuredLLMResponse(
            text=text,
            api="chat.completions",
            response_mode=response_mode,
            status=self._response_status(response, text=text, finish_reason=finish_reason),
            finish_reason=finish_reason,
            usage=_usage_dict(_field(response, "usage")),
        )

    def _responses_structured(
        self,
        messages: ChatMessages,
        *,
        response_format: dict[str, Any],
    ) -> StructuredLLMResponse:
        response = self._responses_create(messages, response_format=response_format)
        text = self._responses_text(response)
        incomplete_details = _incomplete_details_dict(_field(response, "incomplete_details"))
        return StructuredLLMResponse(
            text=text,
            api="responses",
            response_mode="json_schema",
            status=self._response_status(response, text=text),
            finish_reason=str(_field(response, "finish_reason", "") or "") or None,
            incomplete_details=incomplete_details,
            usage=_usage_dict(_field(response, "usage")),
        )

    def __call__(self, messages: ChatMessages) -> str:
        return self._chat_text(self._create(messages))

    def generate_structured(
        self,
        schema: dict[str, Any],
        messages: ChatMessages,
        run_context: dict[str, Any] | None = None,
    ) -> StructuredLLMResponse:
        """Generate JSON matching ``schema``, falling back for older providers."""
        context = run_context or {}
        response_mode = str(context.get("response_mode") or "json_schema")
        if response_mode not in {"json_schema", "json_object", "plain"}:
            raise ValueError(f"response_mode 仅支持 json_schema/json_object/plain，收到 {response_mode!r}")
        schema_name = str(context.get("schema_name") or context.get("pass_name") or "extraction")
        schema_name = "".join(char if char.isalnum() or char in "_-" else "_" for char in schema_name)[:64]
        response_format = {
            "type": "json_schema",
            "name": schema_name or "extraction",
            "strict": True,
            "schema": schema,
        }
        if response_mode == "plain":
            return self._chat_structured(messages, response_mode="plain")
        if response_mode == "json_object":
            try:
                return self._chat_structured(
                    messages,
                    response_mode="json_object",
                    response_format={"type": "json_object"},
                )
            except Exception as exc:  # noqa: BLE001 - older providers may reject all JSON modes
                if not _unsupported_parameter(exc):
                    raise
            return self._chat_structured(messages, response_mode="plain")
        if _is_deepseek_model(self.model) and not _deepseek_strict_schema_compatible(schema):
            return self._chat_structured(
                messages,
                response_mode="json_object",
                response_format={"type": "json_object"},
            )
        if _is_deepseek_v4_flash(self.model) and (
            bool(context.get("record_table"))
            or bool(context.get("prefer_chat_json_schema"))
        ):
            # 该模型在 MaaS 的 Responses 路径上高频返回空数组/截断 JSON；
            # chat.completions 标准 json_schema + thinking disabled 实测稳定且更快。
            try:
                return self._chat_structured(
                    messages,
                    response_mode="json_schema",
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name or "extraction",
                            "strict": True,
                            "schema": schema,
                        },
                    },
                )
            except Exception:  # noqa: BLE001 - 兼容性回退，继续走原有链路
                pass
        try:
            return self._responses_structured(messages, response_format=response_format)
        except Exception as exc:  # noqa: BLE001 - only capability mismatch falls back
            if not _unsupported_parameter(exc):
                raise
        try:
            return self._chat_structured(
                messages,
                response_mode="json_object",
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 - older providers may reject all JSON modes
            if not _unsupported_parameter(exc):
                raise
        return self._chat_structured(messages, response_mode="plain")


def create_llm_client(
    *,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    max_tokens: int | None = None,
    disable_thinking: bool | None = None,
    request_timeout: float | None = None,
) -> OpenAICompatibleLLM:
    """Build an OpenAI-compatible live client without importing OpenAI at module import."""
    from openai import OpenAI  # lazy import：仅 live 调用时才需要

    config = get_llm_config()
    client = OpenAI(
        base_url=base_url or config["base_url"],
        api_key=api_key or config["api_key"] or "not-needed",
        timeout=max(
            1.0,
            float(
                config["request_timeout"]
                if request_timeout is None
                else request_timeout
            ),
        ),
        # Retry accounting belongs to generate_structured/run_state.  Keeping
        # SDK retries enabled multiplies requests invisibly and breaks budgets.
        max_retries=0,
    )
    return OpenAICompatibleLLM(
        client,
        model=model or config["model"],
        max_tokens=max_tokens or config["max_tokens"],
        disable_thinking=(
            config["disable_thinking"] if disable_thinking is None else disable_thinking
        ),
    )


__all__ = ["OpenAICompatibleLLM", "StructuredLLMResponse", "create_llm_client"]
