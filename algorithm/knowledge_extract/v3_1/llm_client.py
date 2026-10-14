"""Live plain-text LLM adapter; extraction JSONL is validated locally.

仅在调用 :func:`create_llm_client` 时才导入 openai，因此测试/无网络环境
不需要安装 openai 也能导入本模块与 split_extractor。
"""
from __future__ import annotations

import re
import math
from typing import Any
from urllib.parse import urlparse

from external_services.llm.chat import thinking_unsupported
from ._logging import get_step_logger
from .config import get_llm_config, get_llm_policy
from .prompt_router import ChatMessages


logger = get_step_logger(__name__)

# Exact semantic operations, never inferred from retry counts or error strings.
SEMANTIC_THINKING_PASSES = frozenset({
    "entity_verification", "entity_admission", "property_verification",
    "evidence_verification", "candidate_identity", "candidate_identity_verification",
    "property_conflict", "repair_entity", "repair_property",
})


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
    """MaaS 上的 deepseek-v4-flash / -0731 在 Responses 路径上输出不稳定。

    模型 id 可能带组织前缀（例如硅基流动的 ``deepseek-ai/DeepSeek-V4-Flash``），
    这类 id 在只有 chat.completions 的兼容服务上会被 Responses 路径打成 404，
    因此按最后一段名字匹配，而不是整串前缀匹配。
    """
    name = model.casefold().rsplit("/", 1)[-1]
    return name.startswith("deepseek-v4-flash")


def _is_official_deepseek_endpoint(base_url: str) -> bool:
    host = (urlparse(base_url or "").hostname or "").casefold().rstrip(".")
    return host == "api.deepseek.com"


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


def explicit_document_field(item):
    labels = {'文件标题', '来文字号', '收文号', '来文单位', '收文日期', '收文部门',
              '联系电话', '密级', '缓急'}
    return (item.get('type') == 'OfficialDocument'
        and item.get('property') in labels and type(item.get('value')) in (str, int, float)
        and len(str(item['value'])) <= 256 and bool(item.get('evidence_quotes'))
        and 'previous_value' not in item and 'conflicting_values' not in item)


def _explicit_document_fields(messages, run_context):
    """A short semantic review still checks ownership; complex claims keep thinking."""
    if (run_context or {}).get('pass_name') != 'property_verification':
        return False
    import json
    try:
        user = json.loads(messages[-1]['content'])
        items = user['task']['items']
    except (ValueError, KeyError, TypeError, IndexError):
        return False
    return bool(items) and all(explicit_document_field(item) for item in items)


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
        thinking: dict[str, Any] | None = None,
    ) -> None:
        self.text = text or ""
        self.api = api
        self.response_mode = response_mode
        self.status = status
        self.finish_reason = finish_reason
        self.incomplete_details = dict(incomplete_details or {})
        self.usage = dict(usage or {}) if usage is not None else None
        self.usage_unavailable = usage is None or usage_unavailable
        self.thinking = dict(thinking or {})

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
            "thinking": dict(self.thinking),
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
        base_url: str = "",
        generation_temperature: float = 0,
        semantic_thinking: bool = True,
        thinking_parameter: str = "thinking",
        stream_generation: bool = False,
        verification_max_tokens: int | None = None,
        fast_verification: bool = False,
    ) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.disable_thinking = disable_thinking
        self.semantic_thinking = semantic_thinking
        self.stream_generation = bool(stream_generation)
        self.verification_max_tokens = max(1, int(verification_max_tokens or max_tokens))
        self.fast_verification = bool(fast_verification)
        if thinking_parameter not in {"thinking", "enable_thinking", "none"}:
            raise ValueError("Invalid thinking parameter")
        self.thinking_parameter = thinking_parameter
        self.base_url = base_url
        if not math.isfinite(generation_temperature) or not 0 <= generation_temperature <= 2:
            raise ValueError('generation_temperature must be finite and between 0 and 2')
        self.generation_temperature = generation_temperature

    def _create(self, messages: ChatMessages, *, response_format: dict[str, Any] | None = None,
                temperature: float = 0, thinking: bool | None = None,
                thinking_trace: dict[str, Any] | None = None,
                stream: bool = False, max_tokens: int | None = None) -> Any:
        enabled = not self.disable_thinking if thinking is None else thinking
        mode = "enabled" if enabled else "disabled"
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": min(self.max_tokens, max_tokens or self.max_tokens),
        }
        if stream:
            from .streaming import consume_entity_stream
            from external_services.llm.runtime import ScheduledClient
            kwargs.update(stream=True, stream_options={"include_usage": True})
            if isinstance(self.client, ScheduledClient):
                kwargs['_response_consumer'] = lambda result: consume_entity_stream(
                    result, deadline=self.client.call_deadline)
        if response_format is not None:
            kwargs["response_format"] = response_format
        if self.thinking_parameter == "thinking":
            kwargs["extra_body"] = {"thinking": {"type": mode}}
        elif self.thinking_parameter == "enable_thinking":
            kwargs["extra_body"] = {"enable_thinking": enabled}
        if thinking_trace is not None:
            thinking_trace.update(requested=mode, parameter=self.thinking_parameter,
                                  sent=mode if "extra_body" in kwargs else "provider_default", fallback=False)
        logger.info(
            "[步骤][LLM-Client] 请求: model=%s messages=%d prompt=%d字符 max_tokens=%s response_format=%s thinking=%s temperature=%s",
            self.model,
            len(messages),
            sum(len(str(item.get("content") or "")) for item in messages),
            kwargs['max_tokens'],
            (response_format or {}).get("type") if isinstance(response_format, dict) else None,
            mode if "extra_body" in kwargs else "provider_default",
            temperature,
        )
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - compatible providers vary
            if "extra_body" not in kwargs or not thinking_unsupported(exc):
                raise
            logger.info("[步骤][LLM-Client] thinking 参数不被支持，改用默认思考模式重试")
            kwargs.pop("extra_body")
            if thinking_trace is not None:
                thinking_trace.update(sent="provider_default", fallback=True)
            response = self.client.chat.completions.create(**kwargs)
        if stream and '_response_consumer' not in kwargs:
            from .request_budget import document_deadline
            deadline = document_deadline(self)
            response = consume_entity_stream(response, deadline=deadline)
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
        temperature: float = 0,
        thinking: bool | None = None,
        stream: bool = False,
        max_tokens: int | None = None,
    ) -> StructuredLLMResponse:
        thinking_trace: dict[str, Any] = {}
        response = self._create(messages, response_format=response_format, temperature=temperature,
                                thinking=thinking, thinking_trace=thinking_trace,
                                stream=stream, max_tokens=max_tokens)
        text = self._chat_text(response)
        finish_reason = self._chat_finish_reason(response)
        return StructuredLLMResponse(
            text=text,
            api="chat.completions",
            response_mode=response_mode,
            status=self._response_status(response, text=text, finish_reason=finish_reason),
            finish_reason=finish_reason,
            usage=_usage_dict(_field(response, "usage")),
            thinking=thinking_trace,
            incomplete_details=_incomplete_details_dict(_field(response, "incomplete_details")),
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
        """Use plain chat text; JSONL parsing and schema validation are local."""
        generation = (run_context or {}).get('pass_name') in {
            'entity', 'recover_entity', 'repair_entity', 'property', 'property_conflict', 'repair_property', 'candidate_identity'}
        thinking = not self.disable_thinking or (
            self.semantic_thinking and (run_context or {}).get("pass_name") in SEMANTIC_THINKING_PASSES)
        fast = self.fast_verification and _explicit_document_fields(messages, run_context)
        if fast:
            thinking = False
        stream = self.stream_generation and (run_context or {}).get('pass_name') in {
            'entity', 'recover_entity', 'repair_entity'} and 'entities' in schema.get('properties', {})
        return self._chat_structured(messages, response_mode="plain",
            temperature=self.generation_temperature if generation else 0, thinking=thinking,
            stream=stream, max_tokens=self.verification_max_tokens if fast else None)


def create_llm_client(
    *,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    max_tokens: int | None = None,
    disable_thinking: bool | None = None,
    request_timeout: float | None = None,
    schedule_requests: bool = True,
    semantic_thinking: bool | None = None,
    thinking_parameter: str = "thinking",
    stream_generation: bool | None = None,
    verification_max_tokens: int | None = None,
    fast_verification: bool | None = None,
) -> OpenAICompatibleLLM:
    """Build an OpenAI-compatible live client without importing OpenAI at module import."""
    from openai import OpenAI  # lazy import：仅 live 调用时才需要

    config = get_llm_policy() if base_url else get_llm_config()
    if not (base_url or config["base_url"]):
        raise ValueError("知识抽取未选择已登记的模型服务；请使用 ops 配置抽取用途")
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
    if schedule_requests and not base_url:
        from algorithm.knowledge_extract.service_configuration import standalone_sdk
        client = standalone_sdk(client, config)
    return OpenAICompatibleLLM(
        client,
        model=model or config["model"],
        max_tokens=max_tokens or config["max_tokens"],
        disable_thinking=(
            config["disable_thinking"] if disable_thinking is None else disable_thinking
        ),
        base_url=base_url or config["base_url"],
        generation_temperature=config['generation_temperature'],
        semantic_thinking=config["semantic_thinking"] if semantic_thinking is None else semantic_thinking,
        thinking_parameter=thinking_parameter,
        stream_generation=config['stream_generation'] if stream_generation is None else stream_generation,
        verification_max_tokens=config['verification_max_tokens'] if verification_max_tokens is None else verification_max_tokens,
        fast_verification=config['fast_verification'] if fast_verification is None else fast_verification,
    )


__all__ = ["OpenAICompatibleLLM", "StructuredLLMResponse", "create_llm_client"]
