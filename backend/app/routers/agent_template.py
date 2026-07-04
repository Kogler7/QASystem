"""Compatibility router for the standalone agentcode service.

The current agentcode package exposes a full FastAPI app via ``agentcode.api``
and is intended to run with the ``agentcode-api`` entrypoint. Keeping a small
router here lets the main backend start cleanly while pointing callers to the
new service boundary.
"""

from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/api/agent-template", tags=["agent-template"])


@router.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "standalone",
        "detail": "agentcode is served by the agentcode-api entrypoint.",
    }


__all__ = ["router"]
