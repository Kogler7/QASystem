from __future__ import annotations

import hashlib
from typing import Any


def caller_identity(headers: dict[str, str]) -> dict[str, str]:
    """Non-secret owner labels for independent multi-tenant tasks.

    Authorization is stored only as a fingerprint so later GET/SSE/cancel
    can verify the same caller without keeping the bearer token in the
    task snapshot.
    """
    identity: dict[str, str] = {}
    tenant = (headers.get("x-tenant-id") or "").strip()
    user = (headers.get("x-user-id") or "").strip()
    authorization = headers.get("authorization") or ""
    if tenant:
        identity["tenant_id"] = tenant
    if user:
        identity["user_id"] = user
    if authorization:
        identity["auth_fp"] = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
    return identity


def identity_matches(owner: dict[str, Any] | None, incoming: dict[str, str]) -> bool:
    if not owner:
        return True
    for key in ("tenant_id", "user_id", "auth_fp"):
        expected = owner.get(key)
        if expected and incoming.get(key) != expected:
            return False
    return True
