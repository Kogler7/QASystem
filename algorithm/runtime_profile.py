from __future__ import annotations

import os


REMOTE_ONLY = "remote-only"
FULL_RUNTIME = "full-runtime"


def get_runtime_profile() -> str:
    value = os.getenv("ALGORITHM_RUNTIME_PROFILE", FULL_RUNTIME).strip().lower()
    if value in {REMOTE_ONLY, FULL_RUNTIME}:
        return value
    raise ValueError(
        "ALGORITHM_RUNTIME_PROFILE 仅支持 remote-only 或 full-runtime"
    )


def is_remote_only() -> bool:
    return get_runtime_profile() == REMOTE_ONLY

