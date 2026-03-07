"""
文档预处理组件 API：进程内调用 algorithm/preproc，支持单文件上传与转换。
"""
import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile

from . import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/component/document-preproc", tags=["component", "document-preproc"])


@router.get("/health")
async def document_preproc_health() -> dict[str, Any]:
    """检查文档预处理模块是否可加载。"""
    return service.health()


@router.post("/convert")
async def convert(file: UploadFile = File(...)) -> dict[str, Any]:
    """上传单个文档并转换为 Markdown/文本。"""
    if not file.filename or not file.filename.strip():
        raise HTTPException(status_code=400, detail="请选择文件")
    suffix = Path(file.filename).suffix.lower()
    allowed = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".html", ".htm"}
    if suffix not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {suffix}，仅支持: {', '.join(sorted(allowed))}",
        )

    upload_dir = service.get_upload_dir()
    safe_name = f"{uuid.uuid4().hex}_{Path(file.filename).name}"
    save_path = upload_dir / safe_name

    try:
        content = await file.read()
        with save_path.open("wb") as f:
            f.write(content)
    except Exception as e:
        logger.exception("保存上传文件失败")
        if save_path.exists():
            try:
                save_path.unlink()
            except OSError:
                pass
        raise HTTPException(status_code=500, detail=f"保存文件失败: {e}") from e

    try:
        result = await asyncio.to_thread(
            service.convert_document,
            input_path=str(save_path),
            save_metadata=False,
            store_excel_raw=False,
        )
        return result
    except Exception as e:
        logger.exception("文档转换失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        if save_path.exists():
            try:
                save_path.unlink()
            except OSError:
                pass
