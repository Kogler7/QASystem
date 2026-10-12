"""Non-secret, request-scoped runtime settings from a directory mount."""
from __future__ import annotations

from docker.configuration import getenv
from docker.runtime_config import HOT_KEYS, read_settings
