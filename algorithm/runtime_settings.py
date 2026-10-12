"""Non-secret, request-scoped runtime settings from a directory mount."""
from __future__ import annotations

import os
from docker.runtime_config import HOT_KEYS, read_settings


def getenv(key: str, default: str | None = None) -> str | None:
    if key in HOT_KEYS:
        values = read_settings()
        if key in values:
            return values[key]
    return os.getenv(key, default)
