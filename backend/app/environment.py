from __future__ import annotations
from docker.configuration import getenv as config_getenv

from pathlib import Path

from dotenv import load_dotenv
from docker.startup_contract import in_container_runtime


ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def should_load_local_dotenv() -> bool:
    """Use dotenv for unmanaged local runs, never as a production config source."""
    # Docker/Kubernetes/systemd normally inject a deployment environment.
    # If it is already present, process environment is the single source of truth.
    return not in_container_runtime() and config_getenv("ENVIRONMENT") is None


def configure_runtime_environment() -> bool:
    """Load the repository-root .env only for local development startup."""
    if not should_load_local_dotenv():
        return False
    load_dotenv(ROOT_ENV_FILE, override=False)
    return True
