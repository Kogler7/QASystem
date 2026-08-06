from __future__ import annotations

import os
import shutil
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class DocumentProcessor:
    ENGINE_MATRIX = {
        ".pdf": ("docling", "vlm", "mineru"),
        ".ofd": ("docling", "vlm", "mineru"),
        ".docx": ("docling", "vlm", "mineru"),
        ".doc": ("docling", "vlm", "mineru"),
        ".pptx": ("docling", "vlm", "mineru"),
        ".ppt": ("docling", "vlm", "mineru"),
        ".png": ("vlm", "mineru", "docling"),
        ".jpg": ("vlm", "mineru", "docling"),
        ".jpeg": ("vlm", "mineru", "docling"),
        ".md": ("text",),
        ".txt": ("text",),
        ".xlsx": ("pandas",),
        ".xls": ("pandas",),
        ".csv": ("pandas",),
        ".html": ("beautifulsoup",),
        ".htm": ("beautifulsoup",),
    }

    def __init__(
        self,
        output_dir: Optional[str] = None,
        mineru_config_path: Optional[str] = None,
        mineru_model_source: str = "local",
        libreoffice_path: Optional[str] = None,
    ):
        self.output_dir = Path(output_dir or Path.cwd() / "processed")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        preproc_root = Path(__file__).resolve().parent.parent
        default_mineru = preproc_root / "mineru.json"
        self.mineru_config_path = (
            mineru_config_path
            or (str(default_mineru) if default_mineru.is_file() else None)
            or os.getenv("MINERU_CONFIG_PATH")
        )
        self.mineru_model_source = mineru_model_source
        self.libreoffice_path = libreoffice_path or os.getenv("LIBREOFFICE_PATH") or os.getenv("SOFFICE_PATH")
        self._processors: dict[str, Any] = {}

    def _processor(self, engine: str):
        if engine in self._processors:
            return self._processors[engine]
        if engine == "docling":
            from .processors.docx_processor import DocxProcessor

            processor = DocxProcessor(libreoffice_path=self.libreoffice_path)
        elif engine in {"mineru", "vlm"}:
            from .processors.ocr_processor import OCRProcessor

            processor = OCRProcessor(
                config_path=self.mineru_config_path,
                model_source=self.mineru_model_source,
                required_mode="pipeline" if engine == "mineru" else "vlm",
            )
        elif engine == "pandas":
            from .processors.excel_processor import ExcelProcessor

            processor = ExcelProcessor()
        elif engine == "beautifulsoup":
            from .processors.html_processor import HTMLProcessor

            processor = HTMLProcessor()
        else:
            raise ValueError(f"未知解析引擎: {engine}")
        self._processors[engine] = processor
        return processor

    def _ofd_converter(self):
        if "ofd_converter" not in self._processors:
            from .processors.ofd_converter import OFDConverter

            self._processors["ofd_converter"] = OFDConverter()
        return self._processors["ofd_converter"]

    def _profile_pdf(self, file_path: str) -> dict[str, Any]:
        profile = {
            "kind": "complex_or_scanned",
            "pages_checked": 0,
            "text_characters": 0,
            "images": 0,
            "reason": "无法确认文本层，优先使用 MinerU",
        }
        try:
            from pypdf import PdfReader

            reader = PdfReader(file_path)
            pages = reader.pages[: min(5, len(reader.pages))]
            text_characters = 0
            images = 0
            for page in pages:
                text_characters += len((page.extract_text() or "").strip())
                try:
                    images += len(page.images)
                except Exception:
                    pass
            checked = len(pages)
            average_text = text_characters / max(checked, 1)
            average_images = images / max(checked, 1)
            is_text_pdf = average_text >= 200 and average_images <= 1.5
            profile.update(
                {
                    "kind": "text" if is_text_pdf else "complex_or_scanned",
                    "pages_checked": checked,
                    "text_characters": text_characters,
                    "images": images,
                    "reason": (
                        "检测到稳定文本层且图片较少"
                        if is_text_pdf
                        else "文本层不足、图片较多或版面较复杂"
                    ),
                }
            )
        except Exception as exc:
            profile["profile_error"] = str(exc)
        return profile

    def _profile_docx(self, file_path: str) -> dict[str, Any]:
        profile = {"kind": "text", "reason": "常规 Word 文档"}
        try:
            with zipfile.ZipFile(file_path) as archive:
                document_xml = archive.read("word/document.xml")
            root = ET.fromstring(document_xml)
            word_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            math_ns = "http://schemas.openxmlformats.org/officeDocument/2006/math"
            tables = sum(1 for _ in root.iter(f"{{{word_ns}}}tbl"))
            drawings = sum(1 for _ in root.iter(f"{{{word_ns}}}drawing"))
            drawings += sum(1 for _ in root.iter(f"{{{word_ns}}}pict"))
            formulas = sum(1 for _ in root.iter(f"{{{math_ns}}}oMath"))
            complex_layout = tables >= 4 or drawings >= 5 or formulas >= 3
            profile = {
                "kind": "complex" if complex_layout else "text",
                "tables": tables,
                "drawings": drawings,
                "formulas": formulas,
                "complexity_thresholds": {"tables": 4, "drawings": 5, "formulas": 3},
                "reason": "表格、图形或公式较多" if complex_layout else "以连续文本为主",
            }
        except Exception as exc:
            profile["profile_error"] = str(exc)
        return profile

    def select_engine(
        self,
        file_path: str,
        requested: Optional[str] = None,
        force: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        suffix = Path(file_path).suffix.lower()
        allowed = self.ENGINE_MATRIX.get(suffix)
        if not allowed:
            raise ValueError(f"不支持的文件类型: {suffix}")

        normalized = (requested or "auto").strip().lower()
        aliases = {"excel": "pandas", "html": "beautifulsoup", "bs4": "beautifulsoup"}
        normalized = aliases.get(normalized, normalized)
        supplied_engine = None if normalized in {"", "auto"} else normalized
        requested_engine = supplied_engine if supplied_engine in allowed else None

        if force and not supplied_engine:
            raise ValueError("force=true 时必须指定 engine")
        if force and requested_engine is None:
            raise ValueError(f"文件类型 {suffix} 不支持强制解析引擎: {supplied_engine}")

        selection: dict[str, Any] = {
            "mode": "forced" if force else "auto",
            "allowed_engines": list(allowed),
        }
        if supplied_engine:
            selection["requested_engine"] = supplied_engine
            selection["request_compatible"] = requested_engine is not None
            if requested_engine is None:
                selection["request_role"] = "ignored_incompatible"

        if force:
            selection["force"] = True
            selection["request_role"] = "forced"
            selection["request_matched_selection"] = True
            return requested_engine, selection

        if suffix in {".md", ".txt"}:
            if supplied_engine:
                selection["request_role"] = "matched_default" if requested_engine == "text" else "ignored_incompatible"
            selection["preferred_engine"] = "text"
            return "text", selection

        if suffix == ".pdf":
            profile = self._profile_pdf(file_path)
            engine = "docling" if profile["kind"] == "text" else "vlm"
            selection["preferred_engine"] = engine
            selection["pdf_profile"] = profile
            if requested_engine:
                selection["request_role"] = "matched_default" if requested_engine == engine else "fallback_candidate"
                selection["request_matched_selection"] = requested_engine == engine
                if requested_engine != engine:
                    selection["fallback_engine"] = requested_engine
            return engine, selection
        if suffix == ".docx":
            profile = self._profile_docx(file_path)
            preferred_engine = "vlm" if profile["kind"] == "complex" else "docling"
            selection["preferred_engine"] = preferred_engine
            if profile["kind"] == "complex" and requested_engine:
                engine = requested_engine
                selection["request_role"] = "matched_default" if requested_engine == preferred_engine else "selected_primary"
            else:
                engine = preferred_engine
                if requested_engine:
                    selection["request_role"] = "matched_default" if requested_engine == engine else "fallback_candidate"
                    if requested_engine != engine:
                        selection["fallback_engine"] = requested_engine
            selection["word_profile"] = profile
            if requested_engine:
                selection["request_matched_selection"] = requested_engine == engine
            return engine, selection
        engine = allowed[0]
        selection["preferred_engine"] = engine
        if requested_engine:
            selection["request_role"] = "matched_default" if requested_engine == engine else "fallback_candidate"
            selection["request_matched_selection"] = requested_engine == engine
            if requested_engine != engine and len(allowed) > 1:
                selection["fallback_engine"] = requested_engine
        return engine, selection

    def _run_processor(
        self,
        engine: str,
        file_path: str,
        suffix: str,
        lang: str,
        ocr_mode: str = "auto",
        table_enable: bool = True,
        formula_enable: bool = True,
        start_page_id: int = 0,
        end_page_id: Optional[int] = None,
    ) -> dict[str, Any]:
        process_path = file_path
        temp_pdf_path = None
        try:
            if engine == "text":
                raw_content = Path(file_path).read_bytes()
                for encoding in ("utf-8-sig", "gb18030"):
                    try:
                        return {
                            "success": True,
                            "content": raw_content.decode(encoding),
                            "metadata": {"processor": "text", "read_mode": "direct", "encoding": encoding},
                        }
                    except UnicodeDecodeError:
                        continue
                return {"success": False, "error": "文本文件编码无法识别"}

            if suffix in {".doc", ".docx", ".ppt", ".pptx", ".png", ".jpg", ".jpeg"} and engine in {"mineru", "vlm"}:
                docling = self._processor("docling")
                temp_pdf_path = docling.convert_to_pdf(file_path)
                if not temp_pdf_path or not os.path.exists(temp_pdf_path):
                    detail = getattr(docling, "last_conversion_error", None) or f"{suffix} 转 PDF 失败"
                    return {"success": False, "error": detail}
                process_path = temp_pdf_path

            kwargs: dict[str, Any] = {}
            if engine in {"mineru", "vlm"}:
                backend = "pipeline" if engine == "mineru" else os.getenv("MINERU_VLM_BACKEND", "vlm-transformers")
                pipeline_method = {"auto": "auto", "on": "ocr", "off": "txt"}[ocr_mode]
                kwargs = {
                    "output_dir": str(self.output_dir),
                    "lang": lang,
                    "backend": backend,
                    "method": pipeline_method,
                    "formula_enable": formula_enable,
                    "table_enable": table_enable,
                    "draw_layout_bbox": False,
                    "draw_span_bbox": False,
                    "dump_md": True,
                    "dump_middle_json": False,
                    "dump_model_output": False,
                    "dump_orig_pdf": False,
                    "dump_content_list": False,
                    "start_page_id": start_page_id,
                    "end_page_id": end_page_id,
                }
                if engine == "vlm":
                    server_url = os.getenv("MINERU_VLM_SERVER_URL", "").strip()
                    if server_url:
                        kwargs["server_url"] = server_url
                    kwargs["max_concurrency"] = int(os.getenv("MINERU_VLM_MAX_CONCURRENCY", "16"))
                    kwargs["http_timeout"] = int(os.getenv("MINERU_VLM_HTTP_TIMEOUT_SECONDS", "600"))
            result = self._processor(engine).process(process_path, **kwargs)
            metadata = result.get("metadata") or {}
            if engine == "mineru":
                metadata["mineru_options"] = {
                    "language": lang,
                    "ocr_mode": ocr_mode,
                    "table_enable": table_enable,
                    "formula_enable": formula_enable,
                }
            content = result.get("content")
            if metadata.get("error"):
                return {"success": False, "error": str(metadata["error"]), "metadata": metadata}
            if content is None or not str(content).strip():
                return {"success": False, "error": f"{engine} 未生成有效内容", "metadata": metadata}
            return {"success": True, "content": content, "metadata": metadata}
        except ModuleNotFoundError as exc:
            return {"success": False, "error": f"缺少 Python 依赖模块：{exc.name}"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        finally:
            if temp_pdf_path:
                shutil.rmtree(os.path.dirname(temp_pdf_path), ignore_errors=True)

    def process_file(
        self,
        file_path: str,
        model_name: Optional[str] = None,
        force: bool = False,
        lang: str = "ch",
        ocr_mode: str = "auto",
        table_enable: bool = True,
        formula_enable: bool = True,
        start_page_id: int = 0,
        end_page_id: Optional[int] = None,
        selected_engine: Optional[str] = None,
        engine_selection: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        path = Path(file_path)
        if not path.exists():
            return {"success": False, "error_code": "FILE_NOT_FOUND", "error": "文件不存在"}
        suffix = path.suffix.lower()
        normalized_ocr_mode = (ocr_mode or "auto").strip().lower()
        if normalized_ocr_mode not in {"auto", "on", "off"}:
            return {
                "success": False,
                "error_code": "INVALID_OCR_MODE",
                "error": "ocr_mode 仅支持 auto、on、off",
                "file_type": suffix.lstrip("."),
            }
        process_path = file_path
        process_suffix = suffix
        converted_pdf_path = None
        try:
            if suffix == ".ofd":
                converter = self._ofd_converter()
                converted_pdf_path = converter.convert_to_pdf(file_path)
                if not converted_pdf_path:
                    error = converter.last_conversion_error or "OFD 转换为 PDF 失败"
                    return {
                        "success": False,
                        "error_code": "DOCUMENT_CONVERT_FAILED",
                        "error": error,
                        "file_type": suffix.lstrip("."),
                    }
                process_path = converted_pdf_path
                process_suffix = ".pdf"
            if selected_engine:
                allowed = self.ENGINE_MATRIX.get(process_suffix) or ()
                if selected_engine not in allowed:
                    raise ValueError(f"文件类型 {process_suffix} 不支持执行计划引擎: {selected_engine}")
                engine = selected_engine
                selection = dict(engine_selection or {})
            else:
                engine, selection = self.select_engine(process_path, model_name, force=force)
            if suffix == ".ofd":
                selection["source_file_type"] = ".ofd"
                selection["converted_file_type"] = ".pdf"
        except ValueError as exc:
            if converted_pdf_path:
                shutil.rmtree(os.path.dirname(converted_pdf_path), ignore_errors=True)
            return {
                "success": False,
                "error_code": "ENGINE_NOT_SUPPORTED",
                "error": str(exc),
                "file_type": suffix.lstrip("."),
            }

        run_options = (normalized_ocr_mode, table_enable, formula_enable)
        result = self._run_processor(
            engine,
            process_path,
            process_suffix,
            lang,
            *run_options,
            start_page_id=start_page_id,
            end_page_id=end_page_id,
        )
        fallback_from = None
        fallback = selection.get("fallback_engine")
        if not result.get("success") and fallback and fallback != engine:
            fallback_from = {"engine": engine, "error": result.get("error", "解析失败")}
            engine = fallback
            result = self._run_processor(
                engine,
                process_path,
                process_suffix,
                lang,
                *run_options,
                start_page_id=start_page_id,
                end_page_id=end_page_id,
            )

        base_metadata = {
            "file_name": path.name,
            "file_path": str(path.absolute()),
            "file_size": path.stat().st_size,
            "file_type": suffix,
            "processed_at": datetime.now().isoformat(),
            "engine_selection": selection,
            "model_used": engine,
            "page_range": {
                "start_page_id": start_page_id,
                "end_page_id": end_page_id,
            },
        }
        if fallback_from:
            base_metadata["fallback_from"] = fallback_from
        base_metadata.update(result.get("metadata") or {})
        if not result.get("success"):
            if converted_pdf_path:
                shutil.rmtree(os.path.dirname(converted_pdf_path), ignore_errors=True)
            return {
                "success": False,
                "error_code": "DOCUMENT_PARSE_FAILED",
                "error": result.get("error", "文档解析失败"),
                "file_type": suffix.lstrip("."),
                "metadata": base_metadata,
            }
        response = {
            "success": True,
            "file_type": suffix.lstrip("."),
            "content": result["content"],
            "metadata": base_metadata,
        }
        if converted_pdf_path:
            shutil.rmtree(os.path.dirname(converted_pdf_path), ignore_errors=True)
        return response

    def process_directory(self, directory: str, lang: str = "ch") -> dict[str, Any]:
        path = Path(directory)
        if not path.is_dir():
            return {"success": False, "error": "目录不存在"}
        results = []
        for file_path in path.rglob("*"):
            if file_path.is_file() and file_path.suffix.lower() in self.ENGINE_MATRIX:
                results.append({"file": str(file_path), "result": self.process_file(str(file_path), lang=lang)})
        successful = sum(1 for item in results if item["result"].get("success"))
        return {
            "success": successful == len(results),
            "total_files": len(results),
            "successful": successful,
            "failed": len(results) - successful,
            "results": results,
        }
