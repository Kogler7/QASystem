from __future__ import annotations

import copy
import json
import traceback
from datetime import datetime, timezone

import httpx
import pytest

from external_services.application import bind_user_headers, endpoint_for
from external_services.clef import ClefFlashClient, ClefServiceError
from external_services.models import ModelServiceError


@pytest.fixture(autouse=True)
def clef_environment(monkeypatch):
    for name in (
        "CLEF_SERVICE_URL",
        "CLEF_SERVICE_PATH",
        "CLEF_SERVICE_API_KEY",
        "CLEF_SERVICE_TIMEOUT_SECONDS",
        "CLEF_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def request_body():
    return {
        "state": {"invoice": {"total": 1250, "status": "overdue"}},
        "questions": {
            "status": {"type": "choice", "criteria": {"paid": "Paid", "overdue": "Past due"}},
            "large": {"type": "noul", "instructions": "Is total greater than 1000?"},
            "urgency": {"type": "score", "criteria": ["Can wait", "This week", "Today"]},
        },
    }


@pytest.fixture
def response_body():
    return {
        "model": "clef-flash",
        "answers": {
            "status": {
                "type": "choice", "choice": "overdue", "confidence": 0.99,
                "probabilities": {"paid": 0.01, "overdue": 0.99},
            },
            "large": {"type": "noul", "noul": 0.999},
            "urgency": {
                "type": "score", "score": 1.8, "confidence": 0.85,
                "legend": {"0": "Can wait", "1": "This week", "2": "Today"},
                "probabilities": {"0": 0.05, "1": 0.1, "2": 0.85},
            },
        },
        "usage": {"input_tokens": 80, "output_tokens": 0},
    }


def mock_service(monkeypatch, handler):
    original_client = httpx.Client
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("external_services.clef.httpx.Client", client)
    return options


def test_systemone_preserves_official_payload_and_answers(monkeypatch, request_body, response_body):
    monkeypatch.setenv("CLEF_SERVICE_URL", "http://clef.internal:8016/")
    monkeypatch.setenv("CLEF_SERVICE_PATH", "/custom/systemone")
    monkeypatch.setenv("CLEF_SERVICE_TIMEOUT_SECONDS", "45")
    before = copy.deepcopy(request_body)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=response_body)

    options = mock_service(monkeypatch, handler)
    result = ClefFlashClient().systemone(request_body)
    assert result == response_body
    assert request_body == before
    assert json.loads(sent[0].content) == {"model": "clef-flash", **request_body}
    assert str(sent[0].url) == "http://clef.internal:8016/custom/systemone"
    assert sent[0].method == "POST"
    assert endpoint_for("models.decisions.create").path == "/custom/systemone"
    assert options == [{"timeout": 45.0, "trust_env": False}]


@pytest.mark.parametrize("service_key", [None, "dedicated-service-key"])
def test_authentication_never_inherits_user_or_proxy_environment(
    monkeypatch, request_body, response_body, service_key,
):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:private-proxy-key@proxy.invalid:9999")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9999")
    if service_key:
        monkeypatch.setenv("CLEF_SERVICE_API_KEY", service_key)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=response_body)

    options = mock_service(monkeypatch, handler)
    with bind_user_headers({"Authorization": "Bearer private-user-token", "X-User-Id": "private-user"}):
        result = ClefFlashClient("http://clef.internal").systemone(request_body)
    expected = f"Bearer {service_key}" if service_key else None
    assert sent[0].headers.get("authorization") == expected
    assert "x-user-id" not in sent[0].headers
    assert options[0]["trust_env"] is False
    assert "private-user" not in sent[0].content.decode()
    assert result == response_body
    assert "headers" not in result


def test_model_configuration_and_request_override(monkeypatch, request_body, response_body):
    monkeypatch.setenv("CLEF_MODEL", "configured-model")
    sent_models = []

    def handler(request):
        model = json.loads(request.content)["model"]
        sent_models.append(model)
        return httpx.Response(200, json={**response_body, "model": model})

    mock_service(monkeypatch, handler)
    client = ClefFlashClient("http://clef.internal")
    client.systemone(request_body)
    client.systemone({**request_body, "model": "request-model"})
    assert sent_models == ["configured-model", "request-model"]


def test_unconfigured_service_fails_without_network(monkeypatch, request_body):
    mock_service(monkeypatch, lambda _: pytest.fail("Network must not be reached"))
    with pytest.raises(ModelServiceError, match="未配置 CLEF_SERVICE_URL"):
        ClefFlashClient().systemone(request_body)


@pytest.mark.parametrize("failure", ["timeout", "connection", "invalid-url", "401", "500", "invalid-json"])
def test_service_failures_are_explicit_and_redacted(monkeypatch, request_body, failure):
    secret = "private-api-key-and-request-data"
    monkeypatch.setenv("CLEF_SERVICE_API_KEY", secret)

    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(secret, request=request)
        if failure == "connection":
            raise httpx.ConnectError(secret, request=request)
        if failure == "invalid-url":
            raise httpx.InvalidURL(secret)
        if failure == "invalid-json":
            return httpx.Response(200, text=secret)
        return httpx.Response(int(failure), json={"error": secret})

    options = mock_service(monkeypatch, handler)
    with pytest.raises(ModelServiceError) as caught:
        ClefFlashClient("http://clef.internal").systemone({**request_body, "state": secret})
    assert secret not in "".join(traceback.format_exception(caught.value))
    assert len(options) == 1  # An error never triggers a retry or local fallback.
    if failure.isdigit():
        assert f"HTTP {failure}" in str(caught.value)
    elif failure == "timeout":
        assert "超时" in str(caught.value)


@pytest.mark.parametrize(
    "change",
    [
        lambda body: [],
        lambda body: {"error": "private service detail"},
        lambda body: {**body, "code": 7, "msg": "private service detail"},
        lambda body: {**body, "model": "wrong-model"},
        lambda body: {**body, "usage": None},
        lambda body: {**body, "answers": {}},
        lambda body: {**body, "answers": {**body["answers"], "large": {"type": "noul", "noul": 2}}},
        lambda body: {**body, "answers": {**body["answers"], "large": {"type": "noul", "noul": 10**400}}},
        lambda body: {**body, "answers": {**body["answers"], "large": {"type": "choice", "choice": "yes"}}},
    ],
)
def test_error_and_malformed_response_never_become_results(monkeypatch, request_body, response_body, change):
    mock_service(monkeypatch, lambda _: httpx.Response(200, json=change(response_body)))
    with pytest.raises(ModelServiceError) as caught:
        ClefFlashClient("http://clef.internal").systemone(request_body)
    assert "private service detail" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"state": "private-input", "questions": {}},
        {"state": None, "questions": {"q": {"type": "unsupported"}}},
        {"state": None, "questions": {"q": {"type": []}}},
        {"state": None, "questions": {"q": {"type": "choice", "criteria": []}}},
        {"state": None, "questions": {"q": {"type": "score", "criteria": {"0": "low"}}}},
        {"state": {object()}, "questions": {"q": {"type": "noul"}}},
    ],
)
def test_invalid_request_is_rejected_without_network(monkeypatch, payload):
    mock_service(monkeypatch, lambda _: pytest.fail("Network must not be reached"))
    with pytest.raises(ValueError) as caught:
        ClefFlashClient("http://clef.internal").systemone(payload)
    assert "private-input" not in str(caught.value)


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "private-invalid-value"])
def test_invalid_timeout_is_redacted(monkeypatch, timeout):
    monkeypatch.setenv("CLEF_SERVICE_TIMEOUT_SECONDS", timeout)
    with pytest.raises(ModelServiceError, match="必须为正数") as caught:
        ClefFlashClient("http://clef.internal")
    assert "private-invalid-value" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("status,detail,kind,retryable", [
    (413, "private request data", "input_too_long", False),
    (422, "Complete input exceeds CLEF_MAX_LENGTH; shorten state or questions", "input_too_long", False),
    (422, "private schema data", "schema_error", False),
    (422, [{"type": "too_long", "input": "private request data"}], "schema_error", False),
    (429, "private request data", "rate_limited", True),
    (503, "private request data", "service_unavailable", True),
    (401, "private request data", "http_error", False),
    (500, "private request data", "http_error", False),
])
def test_http_error_classification_is_structured_and_redacted(
    monkeypatch, request_body, status, detail, kind, retryable,
):
    options = mock_service(monkeypatch, lambda _: httpx.Response(
        status, json={"detail": detail}, headers={"Retry-After": "2.5"},
    ))
    service_url = "http://private-host.invalid"
    with pytest.raises(ClefServiceError) as caught:
        ClefFlashClient(service_url).systemone(request_body)
    error = caught.value
    assert error.status_code == status
    assert error.error_kind == kind
    assert error.retryable is retryable
    assert error.retry_after == (2.5 if status in (429, 503) else None)
    assert "private" not in "".join(traceback.format_exception(error))
    assert len(options) == 1


def test_timeout_can_be_bounded_per_request_without_mutating_client(
    monkeypatch, request_body, response_body,
):
    options = mock_service(monkeypatch, lambda _: httpx.Response(200, json=response_body))
    client = ClefFlashClient("http://clef.internal", timeout=120)
    client.systemone(request_body, timeout=0.75)
    assert options == [{"timeout": 0.75, "trust_env": False}]
    assert client.timeout == 120


@pytest.mark.parametrize("retry_after", ["NaN", "inf", "private invalid value"])
def test_invalid_retry_after_is_not_exposed(monkeypatch, request_body, retry_after):
    mock_service(monkeypatch, lambda _: httpx.Response(
        429, json={"detail": "private"}, headers={"Retry-After": retry_after},
    ))
    with pytest.raises(ClefServiceError) as caught:
        ClefFlashClient("http://clef.internal").systemone(request_body)
    assert caught.value.retry_after is None
    assert retry_after not in str(caught.value)


def test_retry_after_http_date_is_supported(monkeypatch, request_body):
    now = datetime(2016, 9, 8, 7, 30, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr("external_services.clef.time.time", lambda: now)
    mock_service(monkeypatch, lambda _: httpx.Response(
        429, json={"detail": "Busy"}, headers={"Retry-After": "Thu, 08 Sep 2016 07:30:03 GMT"},
    ))
    with pytest.raises(ClefServiceError) as caught:
        ClefFlashClient("http://clef.internal").systemone(request_body)
    assert caught.value.retry_after == 3
