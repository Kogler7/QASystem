from __future__ import annotations
from docker.configuration import getenv as config_getenv

import json
import math
import os
import time
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from algorithm.runtime_settings import getenv

from .application import endpoint_for
from .models import ModelServiceError


class ClefServiceError(ModelServiceError):
    """A redacted failure with machine-readable transport classification."""

    def __init__(
        self,
        message: str,
        *,
        error_kind: str,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class ClefFlashClient:
    """Call the separate Clef service without loading model dependencies.

    Authentication uses the service's own API key. User request headers and
    proxy/netrc environment settings are deliberately never inherited.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        model: str | None = None,
        timeout: float | None = None,
        env_prefix: str = "CLEF",
        capability: str = "models.decisions.create",
        service: dict[str, Any] | None = None,
        purpose: str | None = None,
        resolve_registered: bool = True,
    ) -> None:
        if resolve_registered and service is None and base_url is None:
            from .model_routes import resolve_adapter_service
            service = resolve_adapter_service('nanojev' if env_prefix == 'NANOJEV' else 'clef', purpose=purpose)
        self.service_snapshot = service if service and service.get('kind') != 'legacy' else None
        self.base_url = (service['base_url'] if service is not None else
            base_url if base_url is not None else getenv(f"{env_prefix}_SERVICE_URL", "")
        ).strip().rstrip("/")
        endpoint = endpoint_for(capability) if service is None else None
        self.path = endpoint.path if endpoint is not None else service.get('path', '/v1/systemone')
        self.method = endpoint.method if endpoint is not None else 'POST'
        if not self.path or not self.path.startswith("/") or self.path.startswith("//"):
            raise ModelServiceError("CLEF_SERVICE_PATH 必须是服务内的绝对路径")
        self.model = service['model'] if service is not None else model or getenv("CLEF_MODEL", "").strip() or "clef-flash"
        self.api_key = '' if service is not None else config_getenv(f"{env_prefix}_SERVICE_API_KEY", "").strip()
        if service is not None:
            if service.get('adapter') not in {'clef', 'nanojev'}:
                raise ModelServiceError('实体判定服务的推理协议不兼容')
            self.base_url = service['base_url'].rstrip('/')
            self.model = service['model']
            self.api_key = config_getenv(service['api_key_env'], '').strip() if service.get('api_key_env') else ''
            if service.get('kind') == 'api' and not self.api_key:
                raise ModelServiceError('判定服务凭据未配置')
        try:
            self.timeout = float(
                timeout if timeout is not None else service.get('timeout', 120) if service is not None else getenv(f"{env_prefix}_SERVICE_TIMEOUT_SECONDS", "120")
            )
        except (TypeError, ValueError):
            raise ModelServiceError("CLEF_SERVICE_TIMEOUT_SECONDS 必须为正数") from None
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ModelServiceError("CLEF_SERVICE_TIMEOUT_SECONDS 必须为正数")

    def systemone(
        self, request: dict[str, Any], *, timeout: float | None = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise ClefServiceError("未配置 CLEF_SERVICE_URL", error_kind="configuration")
        try:
            request_timeout = float(self.timeout if timeout is None else timeout)
        except (TypeError, ValueError):
            raise ValueError("Clef-flash timeout 必须为正数") from None
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("Clef-flash timeout 必须为正数")
        payload = self._payload(request)
        headers = {"accept": "application/json", "content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        try:
            from .llm.service import model_call
            with model_call(self.service_snapshot, payload, timeout=request_timeout) as remaining, httpx.Client(timeout=remaining, trust_env=False) as client:
                response = client.request(
                    self.method,
                    f"{self.base_url}{self.path}",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            # Do not include a remote response, URL, or chained request in an
            # exception: any of them may contain credentials or private state.
            status = exc.response.status_code
            kind = "http_error"
            if status == 413:
                kind = "input_too_long"
            elif status == 422:
                kind = "schema_error"
                try:
                    error_body = exc.response.json()
                except (ValueError, TypeError):
                    error_body = None
                # This exact public marker is emitted by the existing service.
                # Do not classify arbitrary validation messages as token overflow.
                if isinstance(error_body, dict) and error_body.get("detail") == (
                    "Complete input exceeds CLEF_MAX_LENGTH; shorten state or questions"
                ):
                    kind = "input_too_long"
            elif status == 429:
                kind = "rate_limited"
            elif status == 503:
                kind = "service_unavailable"
            raise ClefServiceError(
                f"远程 Clef-flash 请求失败（HTTP {status}）",
                status_code=status,
                error_kind=kind,
                retryable=status in (429, 503),
                retry_after=_retry_after(exc.response.headers.get("retry-after"))
                if status in (429, 503) else None,
            ) from None
        except httpx.TimeoutException:
            raise ClefServiceError(
                "远程 Clef-flash 请求超时", error_kind="timeout", retryable=True,
            ) from None
        except httpx.InvalidURL:
            raise ClefServiceError(
                "远程 Clef-flash 连接失败", error_kind="configuration",
            ) from None
        except httpx.HTTPError:
            raise ClefServiceError(
                "远程 Clef-flash 连接失败", error_kind="connection", retryable=True,
            ) from None
        except (TypeError, ValueError):
            raise ClefServiceError(
                "远程 Clef-flash 返回无效 JSON", error_kind="invalid_response",
            ) from None

        try:
            self._validate_response(body, payload)
        except ModelServiceError as exc:
            raise ClefServiceError(str(exc), error_kind="invalid_response") from None
        return body

    def _payload(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict) or "state" not in request:
            raise ValueError("Clef-flash 请求必须为包含 state 的对象")
        payload = {"model": self.model, **request}
        if not isinstance(payload["model"], str) or not payload["model"].strip():
            raise ValueError("Clef-flash model 必须为非空字符串")
        questions = payload.get("questions")
        if not isinstance(questions, dict) or not questions:
            raise ValueError("Clef-flash questions 必须为非空对象")
        for question_id, question in questions.items():
            if not isinstance(question_id, str) or not question_id or not isinstance(question, dict):
                raise ValueError("Clef-flash question 必须为字符串 ID 对应的对象")
            kind = question.get("type")
            if kind not in ("choice", "noul", "score"):
                raise ValueError("Clef-flash question type 必须为 choice、noul 或 score")
            criteria = question.get("criteria")
            if kind == "choice" and (not isinstance(criteria, dict) or not criteria):
                raise ValueError("Clef-flash choice criteria 必须为非空对象")
            if kind == "score" and (not isinstance(criteria, list) or not criteria):
                raise ValueError("Clef-flash score criteria 必须为非空数组")
        try:
            json.dumps(payload, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("Clef-flash 请求必须是有效 JSON 数据") from None
        return payload

    @staticmethod
    def _validate_response(body: Any, payload: dict[str, Any]) -> None:
        if not isinstance(body, dict):
            raise ModelServiceError("远程 Clef-flash 返回体不是对象")
        if "error" in body or body.get("code", 0) != 0:
            raise ModelServiceError("远程 Clef-flash 返回服务错误")
        if body.get("model") != payload["model"] or not isinstance(body.get("usage"), dict):
            raise ModelServiceError("远程 Clef-flash 返回缺少有效 model/usage")
        answers = body.get("answers")
        if not isinstance(answers, dict) or answers.keys() != payload["questions"].keys():
            raise ModelServiceError("远程 Clef-flash 返回 answers 与请求 questions 不一致")
        for question_id, question in payload["questions"].items():
            answer = answers[question_id]
            kind = question["type"]
            if not isinstance(answer, dict) or answer.get("type") != kind:
                raise ModelServiceError("远程 Clef-flash 返回 answer type 无效")
            value = answer.get(kind)
            if kind == "choice":
                valid = isinstance(value, str) and value in question["criteria"]
            else:
                upper = 1 if kind == "noul" else len(question["criteria"]) - 1
                valid = (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and 0 <= value <= upper
                    and math.isfinite(value)
                )
            if not valid:
                raise ModelServiceError("远程 Clef-flash 返回 answer 值无效")
