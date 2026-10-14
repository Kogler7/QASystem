from __future__ import annotations
from docker.configuration import bootstrap_local_environment
from docker.configuration import should_load_local_dotenv

from pathlib import Path



ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def configure_runtime_environment() -> bool:
    """Load the repository-root .env only for local development startup."""
    return bootstrap_local_environment()
