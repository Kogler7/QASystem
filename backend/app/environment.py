from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def should_load_local_dotenv() -> bool:
    """Use dotenv for unmanaged local runs, never as a production config source."""
    explicit = os.getenv("QASYSTEM_LOAD_DOTENV")
    if explicit is not None:
        normalized = explicit.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
        raise ValueError(
            "QASYSTEM_LOAD_DOTENV 仅支持 true/false、1/0、yes/no 或 on/off"
        )

    # Docker/Kubernetes/systemd normally inject a deployment environment.
    # If it is already present, process environment is the single source of truth.
    return "ENVIRONMENT" not in os.environ


def configure_runtime_environment() -> bool:
    """Load the repository-root .env only for local development startup."""
    if not should_load_local_dotenv():
        return False
    load_dotenv(ROOT_ENV_FILE, override=False)
    return True
