from __future__ import annotations

import pytest

from external_services.application import (
    ExternalCapabilityUnavailable,
    endpoint_for,
    get_application_service_url,
)


def test_application_service_url_prefers_canonical_name(monkeypatch):
    monkeypatch.setenv("APPLICATION_SERVICE_URL", "https://application.example/")
    monkeypatch.setenv("FILE_SERVICE_URL", "https://legacy.example")
    assert get_application_service_url() == "https://application.example"


def test_application_service_url_supports_legacy_name(monkeypatch):
    monkeypatch.delenv("APPLICATION_SERVICE_URL", raising=False)
    monkeypatch.setenv("FILE_SERVICE_URL", "https://legacy.example/")
    assert get_application_service_url() == "https://legacy.example"


def test_unopened_capability_fails_only_when_resolved(monkeypatch):
    monkeypatch.delenv("APPLICATION_API_GRAPH_WRITE_PATH", raising=False)
    with pytest.raises(ExternalCapabilityUnavailable, match="第三方接口尚未开通"):
        endpoint_for("graph.write")
