from __future__ import annotations

from pathlib import Path
from typing import Any

import requests


def upload_file(
    content: bytes,
    file_name: str,
    content_type: str,
    base_url: str,
    knowledge_space_id: str | None = None,
) -> dict[str, Any]:
    safe_name = Path(file_name or "artifact.bin").name
    upload_url = f"{base_url.rstrip('/')}/third/files/upload"
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
            return {"success": False, "error": payload.get("info") or payload.get("msg") or "文件服务返回失败"}
        result = payload.get("data") or {}
        return {
            "success": True,
            "id": result.get("id"),
            "name": result.get("file_name") or result.get("name") or safe_name,
            "path": result.get("file_path") or result.get("path"),
            "url": result.get("file_url") or result.get("url"),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "target": upload_url}


def store_vectors(
    items: list[dict[str, Any]],
    base_url: str,
    knowledge_space_id: str | None,
    file_id: str | None,
) -> dict[str, Any]:
    store_url = f"{base_url.rstrip('/')}/third/vectors/store"
    body = {
        "items": [
            {
                "vector": item["vector"],
                "text": item["text"],
            }
            for item in items
        ],
        "knowledgeSpaceId": knowledge_space_id,
        "fileId": file_id,
    }
    try:
        response = requests.post(store_url, json=body, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            return {"success": False, "error": payload.get("msg") or payload.get("info") or "向量存储返回失败"}
        result = payload.get("data") or {}
        return {
            "success": True,
            "inserted_count": result.get("inserted_count"),
            "ids": result.get("ids"),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "target": store_url}
