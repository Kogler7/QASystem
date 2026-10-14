from __future__ import annotations
from qasystem_config.configuration import bootstrap_local_environment
from qasystem_config.configuration import should_load_local_dotenv

from pathlib import Path



ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def configure_runtime_environment() -> bool:
    """Load the repository-root .env only for local development startup."""
    return bootstrap_local_environment()
