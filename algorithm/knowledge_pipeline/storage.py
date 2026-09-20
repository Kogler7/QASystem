from __future__ import annotations

from typing import Any

from algorithm.logging_setup import get_step_logger
from external_services.application import ApplicationServiceClient


logger = get_step_logger(__name__)


def upload_file(
    content: bytes,
    file_name: str,
    content_type: str,
    base_url: str,
    knowledge_space_id: str | None = None,
) -> dict[str, Any]:
    logger.info(
        "[步骤][存储] 原文上传输入: file=%s bytes=%d content_type=%s space=%s",
        file_name,
        len(content),
        content_type,
        knowledge_space_id,
    )
    try:
        result = ApplicationServiceClient(base_url=base_url).upload_file(
            content,
            file_name,
            content_type,
            knowledge_space_id,
        )
        logger.info(
            "[步骤][存储] 原文上传输出: success=True id=%s name=%s url=%s",
            result.get("id"),
            result.get("name"),
            result.get("url"),
        )
        return {
            "success": True,
            "id": result.get("id"),
            "name": result.get("name"),
            "path": result.get("path"),
            "url": result.get("url"),
        }
    except Exception as exc:
        logger.error("[步骤][存储] 原文上传失败: %s", exc)
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
