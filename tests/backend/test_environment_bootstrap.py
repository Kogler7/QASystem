from __future__ import annotations

import pytest

from backend.app import environment


@pytest.fixture(autouse=True)
def local_runtime(monkeypatch):
    monkeypatch.setattr(environment, 'in_container_runtime', lambda: False)


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


@pytest.mark.parametrize('legacy_switch', ['true', 'false', 'sometimes'])
def test_removed_switch_cannot_override_runtime_detection(monkeypatch, legacy_switch):
    monkeypatch.setenv('ENVIRONMENT', 'production')
    monkeypatch.setenv('QASYSTEM_LOAD_DOTENV', legacy_switch)
    assert environment.should_load_local_dotenv() is False


def test_container_never_loads_dotenv_even_without_environment(monkeypatch):
    monkeypatch.delenv('ENVIRONMENT', raising=False)
    monkeypatch.setattr(environment, 'in_container_runtime', lambda: True)
    assert environment.should_load_local_dotenv() is False
