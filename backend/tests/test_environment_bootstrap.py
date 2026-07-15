from __future__ import annotations

import pytest

from backend.app import environment


def test_unmanaged_local_run_loads_root_dotenv(monkeypatch):
    calls = []
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("QASYSTEM_LOAD_DOTENV", raising=False)
    monkeypatch.setattr(environment, "load_dotenv", lambda path, override: calls.append((path, override)))

    assert environment.configure_runtime_environment() is True
    assert calls == [(environment.ROOT_ENV_FILE, False)]


def test_managed_runtime_uses_process_environment_only(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("QASYSTEM_LOAD_DOTENV", raising=False)
    monkeypatch.setattr(
        environment,
        "load_dotenv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dotenv must not load")),
    )

    assert environment.configure_runtime_environment() is False


def test_explicit_dotenv_switch_overrides_automatic_detection(monkeypatch):
    calls = []
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("QASYSTEM_LOAD_DOTENV", "true")
    monkeypatch.setattr(environment, "load_dotenv", lambda path, override: calls.append((path, override)))

    assert environment.configure_runtime_environment() is True
    assert calls == [(environment.ROOT_ENV_FILE, False)]

    monkeypatch.setenv("QASYSTEM_LOAD_DOTENV", "false")
    calls.clear()
    assert environment.configure_runtime_environment() is False
    assert calls == []


def test_invalid_explicit_dotenv_switch_fails_fast(monkeypatch):
    monkeypatch.setenv("QASYSTEM_LOAD_DOTENV", "sometimes")

    with pytest.raises(ValueError, match="QASYSTEM_LOAD_DOTENV"):
        environment.should_load_local_dotenv()
