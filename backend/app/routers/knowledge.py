from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parents[2]
DOCUMENT_DIR = BASE_DIR / "data" / "documents"
DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


class KnowledgeQueryRequest(BaseModel):
    query: str


class KnowledgeResultItem(BaseModel):
    title: Optional[str] = None
    name: Optional[str] = None
    content: Optional[str] = None
    text: Optional[str] = None
    snippet: Optional[str] = None
    source: Optional[str] = None


@router.get("/documents")
async def list_documents() -> List[dict[str, Any]]:
    items: List[dict[str, Any]] = []
    for file in sorted(DOCUMENT_DIR.glob("*")):
        if file.is_file():
            stat = file.stat()
            items.append(
                {
                    "id": file.name,
                    "name": file.name,
                    "size": stat.st_size,
                    "createTime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
    return items


@router.post("/documents")
async def upload_document(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    target_path = DOCUMENT_DIR / file.filename
    content = await file.read()
    try:
        with target_path.open("wb") as f:
            f.write(content)
    except OSError as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}") from e

    return {
        "success": True,
        "name": file.filename,
        "size": len(content),
        "message": "上传成功",
    }


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str) -> dict[str, Any]:
    target_path = DOCUMENT_DIR / doc_id
    if not target_path.exists():
        raise HTTPException(status_code=404, detail="文档不存在")
    try:
        target_path.unlink()
    except OSError as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"删除失败: {e}") from e

    return {"success": True, "message": "文档删除成功"}


@router.post("/query")
async def knowledge_query(body: KnowledgeQueryRequest) -> dict[str, Any]:
    from ..services.dify_client import dify_client

    if not body.query:
        raise HTTPException(status_code=400, detail="query is required")

    try:
        dify_resp = await dify_client.query_knowledge(question=body.query, user_id="default")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e

    results: List[dict[str, Any]] = []

    # Try to adapt common Dify retrieval response structures
    documents: Any = (
        dify_resp.get("documents")
        or dify_resp.get("data", {}).get("documents")
        or dify_resp.get("retrievals")
    )

    if isinstance(documents, list):
        for doc in documents:
            title = doc.get("title") or doc.get("filename") or doc.get("name")
            content = (
                doc.get("content")
                or doc.get("snippet")
                or doc.get("text")
                or doc.get("segment")
            )
            source = doc.get("source") or doc.get("url") or doc.get("metadata", {}).get("source")
            results.append(
                {
                    "title": title or "相关文档",
                    "content": content,
                    "source": source,
                }
            )

    # If no documents parsed, fall back to using answer as a single item
    if not results:
        answer_text = (
            dify_resp.get("answer")
            or dify_resp.get("output_text")
            or dify_resp.get("message")
            or ""
        )
        if answer_text:
            results.append({"title": "知识库回答", "content": answer_text})

    return {"results": results}

