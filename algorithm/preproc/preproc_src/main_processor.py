from __future__ import annotations

import os
import shutil
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from algorithm.runtime_profile import is_remote_only
from algorithm.preproc.preproc_src.page_edges import (
    DEFAULT_MIN_CHARS as DEFAULT_PAGE_EDGE_MIN_CHARS,
    normalize_keep_page_edges,
)

# 显示面积占比低于该值的图片按水印/角标等装饰处理，不计入 PDF 版面复杂度。
# 典型情况：标准文本每页带一个 15pt 的角标（约占页面 0.04%），旧逻辑按 pypdf 的
# 图片对象数统计会把纯文本 PDF 误判成"扫描或复杂版面"。
DECORATIVE_IMAGE_MAX_AREA_RATIO = 0.03
# 保留页眉/页脚时默认打开到解析范围的首页（封面），与文档解析行为规范一致。
DEFAULT_KEEP_PAGE_EDGES = "first_page"


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

    @staticmethod
    def _content_image_count(file_path: str, max_pages: int) -> Optional[int]:
        """统计"内容图片"数：忽略水印、角标这类显示面积很小的装饰图。

        返回 None 表示无法判定（缺少 pypdfium2 或读取失败），调用方退回 pypdf 的对象计数。
        """
        try:
            import pypdfium2 as pdfium
        except ModuleNotFoundError:
            return None
        pdf = None
        try:
            pdf = pdfium.PdfDocument(file_path)
            total = 0
            for index in range(min(max_pages, len(pdf))):
                page = pdf[index]
                width, height = page.get_size()
                page_area = float(width) * float(height)
                if page_area <= 0:
                    continue
                for obj in page.get_objects(filter=(pdfium.raw.FPDF_PAGEOBJ_IMAGE,)):
                    left, bottom, right, top = obj.get_pos()
                    area = abs(float(right) - float(left)) * abs(float(top) - float(bottom))
                    if area / page_area >= DECORATIVE_IMAGE_MAX_AREA_RATIO:
                        total += 1
            return total
        except Exception:
            return None
        finally:
            if pdf is not None:
                try:
                    pdf.close()
                except Exception:
                    pass

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
            raw_images = 0
            for page in pages:
                text_characters += len((page.extract_text() or "").strip())
                try:
                    raw_images += len(page.images)
                except Exception:
                    pass
            checked = len(pages)
            content_images = self._content_image_count(file_path, checked)
            images = raw_images if content_images is None else content_images
            average_text = text_characters / max(checked, 1)
            average_images = images / max(checked, 1)
            is_text_pdf = average_text >= 200 and average_images <= 1.5
            profile.update(
                {
                    "kind": "text" if is_text_pdf else "complex_or_scanned",
                    "pages_checked": checked,
                    "text_characters": text_characters,
                    "images": images,
                    "raw_images": raw_images,
                    "decorative_images_ignored": max(raw_images - images, 0),
                    "reason": (
                        "检测到稳定文本层且内容图片较少"
                        if is_text_pdf
                        else "文本层不足、内容图片较多或版面较复杂"
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
        allow_empty_content: bool = False,
        keep_page_edges: Any = DEFAULT_KEEP_PAGE_EDGES,
        page_edge_min_chars: int = DEFAULT_PAGE_EDGE_MIN_CHARS,
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
                backend = "pipeline" if engine == "mineru" else os.getenv("MINERU_VLM_BACKEND", "vlm-http-client")
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
            elif engine == "docling":
                # Docling 支持 0 基闭区间换算出的 1 基 page_range，并可按页补回页眉/页脚。
                kwargs = {
                    "start_page_id": start_page_id,
                    "end_page_id": end_page_id,
                    "keep_page_edges": keep_page_edges,
                    "page_edge_min_chars": page_edge_min_chars,
                }
            if engine in {"mineru", "vlm"}:
                kwargs["keep_page_edges"] = keep_page_edges
                kwargs["page_edge_min_chars"] = page_edge_min_chars
            result = self._processor(engine).process(process_path, **kwargs)
            metadata = result.get("metadata") or {}
            if engine == "mineru":
                metadata["mineru_options"] = {
                    "language": lang,
                    "ocr_mode": ocr_mode,
                    "table_enable": table_enable,
                    "formula_enable": formula_enable,
                }
            if engine in {"mineru", "vlm", "docling"}:
                metadata["keep_page_edges"] = {
                    "requested": keep_page_edges,
                    "page_edge_min_chars": page_edge_min_chars,
                }
            content = result.get("content")
            if metadata.get("error"):
                return {"success": False, "error": str(metadata["error"]), "metadata": metadata}
            if content is None or not str(content).strip():
                if allow_empty_content:
                    metadata["empty_content"] = True
                    return {"success": True, "content": "", "metadata": metadata}
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
        keep_page_edges: Any = DEFAULT_KEEP_PAGE_EDGES,
        page_edge_min_chars: int = DEFAULT_PAGE_EDGE_MIN_CHARS,
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
        try:
            normalize_keep_page_edges(keep_page_edges)
        except ValueError as exc:
            return {
                "success": False,
                "error_code": "INVALID_KEEP_PAGE_EDGES",
                "error": str(exc),
                "file_type": suffix.lstrip("."),
            }
        if page_edge_min_chars is None or int(page_edge_min_chars) < 1:
            return {
                "success": False,
                "error_code": "INVALID_PAGE_EDGE_MIN_CHARS",
                "error": "page_edge_min_chars 必须是不小于 1 的整数",
                "file_type": suffix.lstrip("."),
            }
        if is_remote_only() and suffix not in {".md", ".txt"}:
            try:
                from external_services.models import RemoteDocumentParserClient

                result = RemoteDocumentParserClient().parse(
                    str(path),
                    engine=model_name,
                    force=force,
                    language=lang,
                    ocr_mode=normalized_ocr_mode,
                    table_enable=table_enable,
                    formula_enable=formula_enable,
                    start_page_id=start_page_id,
                    end_page_id=end_page_id,
                )
                result.setdefault("metadata", {})["engine_selection"] = {
                    "mode": "remote-only",
                    "requested_engine": model_name,
                }
                return {"success": True, **result}
            except Exception as exc:
                return {
                    "success": False,
                    "error_code": "REMOTE_DOCUMENT_PARSE_FAILED",
                    "error": str(exc),
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
            allow_empty_content=end_page_id is not None,
            keep_page_edges=keep_page_edges,
            page_edge_min_chars=page_edge_min_chars,
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
                allow_empty_content=end_page_id is not None,
                keep_page_edges=keep_page_edges,
                page_edge_min_chars=page_edge_min_chars,
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
