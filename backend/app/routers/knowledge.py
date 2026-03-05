import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models.document import Document
from ..config import settings

logger = logging.getLogger(__name__)

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


def get_file_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    type_map = {
        ".pdf": "pdf",
        ".doc": "doc",
        ".docx": "docx",
        ".txt": "txt",
        ".md": "markdown",
        ".xlsx": "excel",
        ".xls": "excel",
    }
    return type_map.get(ext, "other")


@router.get("/documents")
async def list_documents(session: AsyncSession = Depends(get_session)) -> List[dict[str, Any]]:
    result = await session.execute(
        select(Document).order_by(Document.created_at.desc())
    )
    documents = result.scalars().all()
    
    items = []
    for doc in documents:
        items.append({
            "id": doc.id,
            "name": doc.name,
            "size": doc.file_size,
            "fileType": doc.file_type,
            "status": doc.status,
            "description": doc.description,
            "difyDocumentId": doc.dify_document_id,
            "createTime": doc.created_at.isoformat() if doc.created_at else None,
            "updateTime": doc.updated_at.isoformat() if doc.updated_at else None,
        })
    return items


@router.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    description: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="No filename provided")

        # 检查文件名是否已存在
        result = await session.execute(
            select(Document).where(Document.name == file.filename)
        )
        existing_doc = result.scalar_one_or_none()
        if existing_doc:
            raise HTTPException(status_code=400, detail=f"文件 '{file.filename}' 已存在")

        # 处理文件名冲突（如果文件系统中已存在）
        target_path = DOCUMENT_DIR / file.filename
        if target_path.exists():
            # 生成唯一文件名
            base_name = target_path.stem
            extension = target_path.suffix
            counter = 1
            while target_path.exists():
                target_path = DOCUMENT_DIR / f"{base_name}_{counter}{extension}"
                counter += 1

        content = await file.read()

        try:
            # 先保存文件
            with target_path.open("wb") as f:
                f.write(content)

            # 然后保存数据库记录
            now = datetime.now(timezone.utc)
            doc = Document(
                name=target_path.name,  # 使用实际保存的文件名
                file_path=str(target_path),
                file_size=len(content),
                file_type=get_file_type(file.filename),
                content="",
                description=description or "",
                status="uploaded",
                created_at=now,
                updated_at=now,
            )
            session.add(doc)
            await session.commit()
            await session.refresh(doc)
        except OSError as e:
            logger.error(f"保存文件失败: {e}", exc_info=True)
            # 如果文件操作失败，删除已保存的文件
            if target_path.exists():
                try:
                    target_path.unlink()
                except OSError:
                    pass
            raise HTTPException(status_code=500, detail=f"保存文件失败: {e}") from e
        except Exception as e:
            logger.error(f"保存文档记录失败: {e}", exc_info=True)
            # 如果数据库操作失败，删除已保存的文件
            if target_path.exists():
                try:
                    target_path.unlink()
                except OSError:
                    pass
            await session.rollback()
            raise HTTPException(status_code=500, detail=f"保存文档记录失败: {e}") from e

        return {
            "success": True,
            "id": doc.id,
            "name": doc.name,
            "size": doc.file_size,
            "fileType": doc.file_type,
            "status": doc.status,
            "description": doc.description,
            "createTime": doc.created_at.isoformat(),
            "message": "上传成功",
        }
    except HTTPException:
        # 重新抛出 HTTP 异常
        raise
    except Exception as e:
        logger.error(f"上传文档时发生未预期的错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}") from e


@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: int,
    session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    result = await session.execute(
        select(Document).where(Document.id == doc_id)
    )
    doc = result.scalar_one_or_none()
    
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    file_path = Path(doc.file_path)
    file_exists = file_path.exists()
    
    try:
        # 先删除数据库记录
        await session.delete(doc)
        await session.commit()
        
        # 数据库删除成功后，再删除文件
        if file_exists:
            try:
                file_path.unlink()
            except OSError as e:
                # 文件删除失败不影响数据库操作，只记录警告
                # 可以考虑添加日志记录
                pass
    except Exception as e:
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"删除文档失败: {e}") from e

    return {"success": True, "message": "文档删除成功"}


@router.put("/documents/{doc_id}")
async def update_document(
    doc_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    result = await session.execute(
        select(Document).where(Document.id == doc_id)
    )
    doc = result.scalar_one_or_none()
    
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    old_path = None
    new_path = None
    
    try:
        if name is not None and name != doc.name:
            # 检查新名称是否已被其他文档使用
            name_check = await session.execute(
                select(Document).where(Document.name == name, Document.id != doc_id)
            )
            if name_check.scalar_one_or_none():
                raise HTTPException(status_code=400, detail=f"文件名 '{name}' 已被使用")
            
            old_path = Path(doc.file_path)
            new_path = old_path.parent / name
            
            # 如果文件存在，先重命名文件
            if old_path.exists():
                if new_path.exists():
                    raise HTTPException(status_code=400, detail=f"目标文件 '{name}' 已存在")
                old_path.rename(new_path)
            
            doc.file_path = str(new_path)
            doc.name = name
        
        if description is not None:
            doc.description = description
        
        await session.commit()
        await session.refresh(doc)
    except HTTPException:
        # 如果是 HTTPException，直接抛出
        raise
    except OSError as e:
        # 文件操作失败，尝试恢复
        if old_path and new_path and new_path.exists() and not old_path.exists():
            try:
                new_path.rename(old_path)
            except OSError:
                pass
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"更新文件失败: {e}") from e
    except Exception as e:
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"更新文档失败: {e}") from e

    return {
        "success": True,
        "id": doc.id,
        "name": doc.name,
        "description": doc.description,
        "updateTime": doc.updated_at.isoformat(),
        "message": "文档更新成功",
    }


@router.get("/documents/{doc_id}")
async def get_document(
    doc_id: int,
    session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    result = await session.execute(
        select(Document).where(Document.id == doc_id)
    )
    doc = result.scalar_one_or_none()
    
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    return {
        "id": doc.id,
        "name": doc.name,
        "filePath": doc.file_path,
        "size": doc.file_size,
        "fileType": doc.file_type,
        "content": doc.content,
        "description": doc.description,
        "status": doc.status,
        "difyDocumentId": doc.dify_document_id,
        "createTime": doc.created_at.isoformat() if doc.created_at else None,
        "updateTime": doc.updated_at.isoformat() if doc.updated_at else None,
    }


@router.post("/documents/{doc_id}/sync-dify")
async def sync_to_dify(
    doc_id: int,
    session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    result = await session.execute(
        select(Document).where(Document.id == doc_id)
    )
    doc = result.scalar_one_or_none()
    
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    if not settings.DIFY_KNOWLEDGE_API_KEY:
        raise HTTPException(status_code=400, detail="DIFY_KNOWLEDGE_API_KEY 未配置")

    # 检查文件是否存在
    file_path = Path(doc.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文档文件不存在")

    try:
        doc.status = "syncing"
        await session.commit()
        await session.refresh(doc)
    except Exception as e:
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"更新文档状态失败: {e}") from e

    return {
        "success": True,
        "message": "开始同步到Dify知识库",
        "status": "syncing",
    }


@router.post("/query")
async def knowledge_query(body: KnowledgeQueryRequest) -> dict[str, Any]:
    from ..services.dify_client import dify_client

    if not body.query:
        raise HTTPException(status_code=400, detail="query is required")

    try:
        dify_resp = await dify_client.query_knowledge(question=body.query, user_id="default")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    results: List[dict[str, Any]] = []

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
