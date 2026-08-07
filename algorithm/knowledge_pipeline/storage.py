from __future__ import annotations

from typing import Any

from external_services.application import ApplicationServiceClient


def upload_file(
    content: bytes,
    file_name: str,
    content_type: str,
    base_url: str,
    knowledge_space_id: str | None = None,
) -> dict[str, Any]:
    try:
        result = ApplicationServiceClient(base_url=base_url).upload_file(
            content,
            file_name,
            content_type,
            knowledge_space_id,
        )
        return {
            "success": True,
            "id": result.get("id"),
            "name": result.get("name"),
            "path": result.get("path"),
            "url": result.get("url"),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "capability": "files.upload"}


def store_vectors(
    items: list[dict[str, Any]],
    base_url: str,
    knowledge_space_id: str | None,
    file_id: str | None,
) -> dict[str, Any]:
    try:
        result = ApplicationServiceClient(base_url=base_url).store_vectors(
            items,
            knowledge_space_id,
            file_id,
        )
        return {
            "success": True,
            "inserted_count": result.get("inserted_count"),
            "ids": result.get("ids"),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "capability": "vectors.store"}
