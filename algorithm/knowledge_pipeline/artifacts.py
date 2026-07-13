from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import requests


FILE_DIR = Path(__file__).resolve().parents[1] / "data" / "artifacts"


def store_artifact(
    content: bytes,
    file_name: str,
    content_type: str,
    file_service_url: str = "",
    knowledge_space_id: str | None = None,
) -> dict[str, Any]:
    safe_name = Path(file_name or "artifact.bin").name
    if not file_service_url:
        file_id = uuid.uuid4().hex
        target_dir = FILE_DIR / file_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / safe_name
        target_path.write_bytes(content)
        return {
            "success": True,
            "file_id": file_id,
            "file_path": str(target_path),
            "file_url": f"/file/{file_id}/{safe_name}",
        }

    upload_url = f"{file_service_url.rstrip('/')}/third/files/upload"
    data = {"knowledgeSpaceId": knowledge_space_id} if knowledge_space_id else {}
    try:
        response = requests.post(
            upload_url,
            files={"file": (safe_name, content, content_type)},
            data=data,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            return {"success": False, "error": payload.get("info", "文件服务返回失败")}
        result = payload.get("data") or {}
        return {
            "success": True,
            "file_id": result.get("id"),
            "file_path": result.get("file_path"),
            "file_url": result.get("file_url"),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "target": upload_url}


def resolve_local_artifact(file_id: str, file_name: str) -> Path | None:
    target = FILE_DIR / file_id / Path(file_name).name
    return target if target.is_file() else None
