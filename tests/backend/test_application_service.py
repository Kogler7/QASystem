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


def test_application_service_url_requires_canonical_name(monkeypatch):
    monkeypatch.delenv("APPLICATION_SERVICE_URL", raising=False)
    monkeypatch.setenv("FILE_SERVICE_URL", "https://legacy.example/")
    assert get_application_service_url() == ""


def test_unopened_capability_fails_only_when_resolved(monkeypatch):
    monkeypatch.delenv("APPLICATION_API_GRAPH_WRITE_PATH", raising=False)
    with pytest.raises(ExternalCapabilityUnavailable, match="第三方接口尚未开通"):
        endpoint_for("graph.write")


@pytest.mark.parametrize("capability,variable", [
    ("graph.write", "APPLICATION_API_GRAPH_WRITE_PATH"),
    ("confidence.calculate", "APPLICATION_API_CONFIDENCE_PATH"),
    ("documents.evidence.search", "APPLICATION_API_DOCUMENT_EVIDENCE_PATH"),
])
def test_environment_cannot_unlock_unopened_capability(monkeypatch, capability, variable):
    monkeypatch.setenv(variable, "/unsafe-unlock")
    with pytest.raises(ExternalCapabilityUnavailable, match="第三方接口尚未开通"):
        endpoint_for(capability)


@pytest.mark.parametrize("capability,variable,path", [
    ("files.upload", "APPLICATION_API_FILE_UPLOAD_PATH", "/third/files/upload"),
    ("vectors.store", "APPLICATION_API_VECTOR_STORE_PATH", "/third/vectors/store"),
    ("graph.cypher.query", "APPLICATION_API_CYPHER_QUERY_PATH", "/third/cypher/query"),
])
def test_application_contract_paths_are_fixed(monkeypatch, capability, variable, path):
    monkeypatch.setenv(variable, "/old-override")
    assert endpoint_for(capability).path == path
