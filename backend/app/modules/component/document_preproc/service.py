"""
文档预处理组件：进程内调用 algorithm/preproc，按函数封装，不启动独立服务。
支持 PDF/DOCX/Excel 等转 Markdown/文本；使用前需安装 algorithm/preproc 依赖。
"""
import logging
import os
import sys
from pathlib import Path
from typing import Any
import importlib.util

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parents[4]  # backend
_ROOT = _THIS.parents[5]  # qasystem
_PREPROC_ROOT = _ROOT / "algorithm" / "preproc"
if _PREPROC_ROOT.exists() and str(_PREPROC_ROOT) not in sys.path:
    sys.path.insert(0, str(_PREPROC_ROOT))

_OUTPUT_DIR = _BACKEND / "data" / "preproc_output"
_UPLOAD_DIR = _BACKEND / "data" / "preproc_upload"

_interface = None


def _get_interface():
    global _interface
    if _interface is None:
        try:
            from document_ingestion import DocumentConversionInterface
            _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            _interface = DocumentConversionInterface(
                output_format="md",
                save_to_file=True,
                output_dir=str(_OUTPUT_DIR),
                enable_db_storage=False,
                excel_to_mysql=True,
                mineru_config_path=None,
                mineru_model_source=os.environ.get("MINERU_MODEL_SOURCE", "local"),
            )
        except Exception as e:
            logger.warning("加载文档预处理模块失败: %s", e)
            raise
    return _interface


def health() -> dict[str, Any]:
    """检查文档预处理模块是否可加载。"""
    try:
        _get_interface()
        return {"status": "ok", "service": "document-preproc"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"文档预处理模块不可用: {e}。请确认已安装 algorithm/preproc 依赖（见 README/requirements）。",
        }


def convert_document(
    input_path: str,
    model_name: str | None = None,
    output_filename: str | None = None,
    save_metadata: bool = False,
    store_excel_raw: bool = False,
) -> dict[str, Any]:
    """
    处理单个文档，转为 Markdown/文本。
    由路由层用 asyncio.to_thread 调用。
    """
    if not input_path or not Path(input_path).exists():
        return {
            "success": False,
            "input_path": input_path,
            "error": "文件不存在或路径为空",
        }
    iface = _get_interface()
    result = iface.convert_document(
        input_path=input_path,
        model_name=model_name,
        output_filename=output_filename,
        save_metadata=save_metadata,
        store_excel_raw=store_excel_raw,
    )
    if result is None:
        return {
            "success": False,
            "input_path": input_path,
            "error": "文档预处理未返回结果",
        }
    if result.get("content") and isinstance(result["content"], str) and len(result["content"]) > 20000:
        result["content_preview"] = result["content"][:20000] + "\n...(已截断)"
        result["content"] = result["content"][:20000]
    return result


def get_available_models() -> dict[str, Any]:
    """获取可用的文档处理模型信息。"""
    try:
        iface = _get_interface()
        processors = iface.processor.available_processors
        
        result = {}
        for file_type, config in processors.items():
            result[file_type] = {
                "default": config["default"],
                "models": {}
            }
            for model_name, model_data in config["models"].items():
                metadata = model_data["metadata"]
                result[file_type]["models"][model_name] = {
                    "name": metadata["name"],
                    "description": metadata["description"],
                    "pros": [metadata["优点"]],
                    "cons": [metadata["缺点"]],
                    "scenario": metadata["适用场景"]
                }
        return result
    except Exception as e:
        logger.error("获取可用模型信息失败: %s", e)
        return {}


def get_upload_dir() -> Path:
    """返回上传临时目录，供路由保存文件。"""
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return _UPLOAD_DIR


def convert_to_pdf(input_path: str) -> str | None:
    """
    封装预处理模块中对 DOC/DOCX 转 PDF 的能力，优先使用 preproc 的 DocxProcessor.convert_to_pdf。
    返回生成的 PDF 文件绝对路径，失败时返回 None 或抛出异常。
    """
    if not input_path or not Path(input_path).exists():
        return None

    try:
        # 尝试以包路径导入（preproc 已被插入到 sys.path）
        try:
            from preproc_src.processors.docx_processor import DocxProcessor  # type: ignore
        except Exception:
            try:
                from processors.docx_processor import DocxProcessor  # type: ignore
            except Exception:
                # 最后按文件路径动态加载模块
                module_path = _PREPROC_ROOT / "preproc_src" / "processors" / "docx_processor.py"
                if not module_path.exists():
                    raise ImportError("找不到 docx_processor 模块")
                spec = importlib.util.spec_from_file_location("docx_processor", str(module_path))
                mod = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(mod)  # type: ignore
                DocxProcessor = getattr(mod, "DocxProcessor")

        processor = DocxProcessor()
        pdf_path = processor.convert_to_pdf(input_path)
        return pdf_path
    except Exception:
        raise
