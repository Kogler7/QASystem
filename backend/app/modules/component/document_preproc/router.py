"""
文档预处理组件 API：进程内调用 algorithm/preproc，支持单文件上传与转换。
"""
import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from ....deps.auth import require_admin
from . import service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/component/document-preproc",
    tags=["component", "document-preproc"],
    dependencies=[Depends(require_admin)],
)


@router.get("/health")
async def document_preproc_health() -> dict[str, Any]:
    """检查文档预处理模块是否可加载。"""
    return service.health()


@router.post("/convert")
async def convert(
    file: UploadFile = File(...),
    model_name: str | None = Form(default=None),
) -> dict[str, Any]:
    """上传单个文档并转换为 Markdown/文本。"""
    if not file.filename or not file.filename.strip():
        raise HTTPException(status_code=400, detail="请选择文件")
    suffix = Path(file.filename).suffix.lower()
    allowed = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".html", ".htm"}
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
            model_name=model_name,
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


@router.post("/convert-to-pdf")
async def convert_to_pdf_route(file: UploadFile = File(...)) -> Response:
    """上传单个 .doc/.docx 文件并返回生成的 PDF（二进制）。"""
    if not file.filename or not file.filename.strip():
        raise HTTPException(status_code=400, detail="请选择文件")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".doc", ".docx"}:
        raise HTTPException(status_code=400, detail="仅支持 .doc/.docx 转 PDF")

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

    pdf_path = None
    try:
        pdf_path = await asyncio.to_thread(service.convert_to_pdf, str(save_path))
        if not pdf_path or not Path(pdf_path).exists():
            raise HTTPException(status_code=500, detail="文档转换为 PDF 失败")
        with open(pdf_path, "rb") as pf:
            pdf_bytes = pf.read()
        return Response(content=pdf_bytes, media_type="application/pdf")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("DOC -> PDF 转换失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        try:
            if save_path.exists():
                save_path.unlink()
        except Exception:
            pass
        try:
            if pdf_path and Path(pdf_path).exists():
                Path(pdf_path).unlink()
        except Exception:
            pass
