from __future__ import annotations

import pytest

from backend.app import environment
from qasystem_config import configuration, startup_contract


@pytest.fixture(autouse=True)
def local_runtime(monkeypatch):
    monkeypatch.setattr(startup_contract, 'in_container_runtime', lambda: False)


def test_unmanaged_local_run_loads_root_dotenv(monkeypatch):
    calls = []
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("QASYSTEM_LOAD_DOTENV", raising=False)
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda path, override: calls.append((path, override)))

    assert environment.configure_runtime_environment() is True
    assert calls == [(environment.ROOT_ENV_FILE, False)]


def test_managed_runtime_uses_process_environment_only(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("QASYSTEM_LOAD_DOTENV", raising=False)
    monkeypatch.setattr(
        __import__('dotenv'),
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
    monkeypatch.setattr(startup_contract, 'in_container_runtime', lambda: True)
    assert environment.should_load_local_dotenv() is False


def test_development_agent_startup_preserves_spawned_concurrency(monkeypatch):
    monkeypatch.delenv('ENVIRONMENT', raising=False)
    monkeypatch.setenv('AGENT_WORKER_CONCURRENCY', '4')
    configuration.prepare_dev_agent_startup(8)
    assert configuration.startup_getenv('AGENT_WORKER_CONCURRENCY') == '8'


@pytest.mark.parametrize('concurrency', [0, -1, 257, True, '8'])
def test_development_agent_startup_rejects_invalid_concurrency(monkeypatch, concurrency):
    monkeypatch.delenv('ENVIRONMENT', raising=False)
    with pytest.raises(ValueError, match='1到256'):
        configuration.prepare_dev_agent_startup(concurrency)


def test_managed_agent_startup_cannot_override_approved_concurrency(monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'production')
    monkeypatch.setenv('AGENT_WORKER_CONCURRENCY', '4')
    with pytest.raises(ValueError, match='ops'):
        configuration.prepare_dev_agent_startup(8)
    assert configuration.startup_getenv('AGENT_WORKER_CONCURRENCY') == '4'
    assert environment.should_load_local_dotenv() is False


def test_mineru_adapter_only_sets_registered_dependency_keys(monkeypatch):
    keys = ('MINERU_TOOLS_CONFIG_JSON', 'MINERU_CONFIG_PATH', 'MINERU_MODEL_SOURCE')
    for key in keys:
        monkeypatch.setenv(key, 'fixture-old')
    configuration.configure_mineru_loader_environment('/tmp/mineru-fixture.json', 'local')
    assert configuration.startup_getenv('MINERU_TOOLS_CONFIG_JSON') == '/tmp/mineru-fixture.json'
    assert configuration.startup_getenv('MINERU_CONFIG_PATH') == '/tmp/mineru-fixture.json'
    assert configuration.startup_getenv('MINERU_MODEL_SOURCE') == 'local'
