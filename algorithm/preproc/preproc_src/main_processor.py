from __future__ import annotations

import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class DocumentProcessor:
    ENGINE_MATRIX = {
        ".pdf": ("docling", "mineru"),
        ".docx": ("docling", "mineru"),
        ".doc": ("docling", "mineru"),
        ".xlsx": ("pandas",),
        ".xls": ("pandas",),
        ".csv": ("pandas",),
        ".html": ("bs4",),
        ".htm": ("bs4",),
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
        elif engine == "mineru":
            from .processors.ocr_processor import OCRProcessor

            processor = OCRProcessor(
                config_path=self.mineru_config_path,
                model_source=self.mineru_model_source,
            )
        elif engine == "pandas":
            from .processors.excel_processor import ExcelProcessor

            processor = ExcelProcessor()
        elif engine == "bs4":
            from .processors.html_processor import HTMLProcessor

            processor = HTMLProcessor()
        else:
            raise ValueError(f"未知解析引擎: {engine}")
        self._processors[engine] = processor
        return processor

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
                document_xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
            tables = document_xml.count("<w:tbl")
            drawings = document_xml.count("<w:drawing") + document_xml.count("<w:pict")
            formulas = document_xml.count("<m:oMath")
            complex_layout = tables >= 4 or drawings >= 5 or formulas >= 3
            profile = {
                "kind": "complex" if complex_layout else "text",
                "tables": tables,
                "drawings": drawings,
                "formulas": formulas,
                "reason": "表格、图形或公式较多" if complex_layout else "以连续文本为主",
            }
        except Exception as exc:
            profile["profile_error"] = str(exc)
        return profile

    def select_engine(self, file_path: str, requested: Optional[str] = None) -> tuple[str, dict[str, Any]]:
        suffix = Path(file_path).suffix.lower()
        allowed = self.ENGINE_MATRIX.get(suffix)
        if not allowed:
            raise ValueError(f"不支持的文件类型: {suffix}")

        normalized = (requested or "auto").strip().lower()
        aliases = {"excel": "pandas", "html": "bs4", "beautifulsoup": "bs4"}
        normalized = aliases.get(normalized, normalized)
        if normalized not in {"", "auto"}:
            if normalized not in allowed:
                raise ValueError(
                    f"解析引擎 {normalized} 不适用于 {suffix}，可用引擎: {', '.join(allowed)}"
                )
            return normalized, {"mode": "explicit", "allowed_engines": list(allowed)}

        if suffix == ".pdf":
            profile = self._profile_pdf(file_path)
            engine = "docling" if profile["kind"] == "text" else "mineru"
            return engine, {"mode": "auto", "pdf_profile": profile, "allowed_engines": list(allowed)}
        if suffix == ".docx":
            profile = self._profile_docx(file_path)
            engine = "mineru" if profile["kind"] == "complex" else "docling"
            return engine, {"mode": "auto", "word_profile": profile, "allowed_engines": list(allowed)}
        return allowed[0], {"mode": "auto", "allowed_engines": list(allowed)}

    def _run_processor(self, engine: str, file_path: str, suffix: str, lang: str) -> dict[str, Any]:
        process_path = file_path
        temp_pdf_path = None
        try:
            if suffix in {".doc", ".docx"} and engine == "mineru":
                docling = self._processor("docling")
                temp_pdf_path = docling.convert_to_pdf(file_path)
                if not temp_pdf_path or not os.path.exists(temp_pdf_path):
                    return {"success": False, "error": "Word 转 PDF 失败，无法使用 MinerU 解析"}
                process_path = temp_pdf_path

            kwargs: dict[str, Any] = {}
            if engine == "mineru":
                kwargs = {
                    "output_dir": str(self.output_dir),
                    "lang": lang,
                    "backend": os.getenv("MINERU_BACKEND") or "vlm",
                    "method": "auto",
                    "formula_enable": True,
                    "table_enable": True,
                    "draw_layout_bbox": False,
                    "draw_span_bbox": False,
                    "dump_md": True,
                    "dump_middle_json": False,
                    "dump_model_output": False,
                    "dump_orig_pdf": False,
                    "dump_content_list": False,
                    "start_page_id": 0,
                    "end_page_id": None,
                }
            result = self._processor(engine).process(process_path, **kwargs)
            metadata = result.get("metadata") or {}
            content = result.get("content")
            if metadata.get("error"):
                return {"success": False, "error": str(metadata["error"]), "metadata": metadata}
            if content is None or not str(content).strip():
                return {"success": False, "error": f"{engine} 未生成有效内容", "metadata": metadata}
            return {"success": True, "content": content, "metadata": metadata}
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        finally:
            if temp_pdf_path:
                shutil.rmtree(os.path.dirname(temp_pdf_path), ignore_errors=True)

    def process_file(self, file_path: str, model_name: Optional[str] = None, lang: str = "ch") -> dict[str, Any]:
        path = Path(file_path)
        if not path.exists():
            return {"success": False, "error_code": "FILE_NOT_FOUND", "error": "文件不存在"}
        suffix = path.suffix.lower()
        try:
            engine, selection = self.select_engine(file_path, model_name)
        except ValueError as exc:
            return {
                "success": False,
                "error_code": "ENGINE_NOT_SUPPORTED",
                "error": str(exc),
                "file_type": suffix.lstrip("."),
            }

        result = self._run_processor(engine, file_path, suffix, lang)
        fallback_from = None
        if not result.get("success") and selection["mode"] == "auto" and len(self.ENGINE_MATRIX[suffix]) > 1:
            fallback = next(candidate for candidate in self.ENGINE_MATRIX[suffix] if candidate != engine)
            fallback_from = {"engine": engine, "error": result.get("error", "解析失败")}
            engine = fallback
            result = self._run_processor(engine, file_path, suffix, lang)

        base_metadata = {
            "file_name": path.name,
            "file_path": str(path.absolute()),
            "file_size": path.stat().st_size,
            "file_type": suffix,
            "processed_at": datetime.now().isoformat(),
            "engine_selection": selection,
            "model_used": engine,
        }
        if fallback_from:
            base_metadata["fallback_from"] = fallback_from
        base_metadata.update(result.get("metadata") or {})
        if not result.get("success"):
            return {
                "success": False,
                "error_code": "DOCUMENT_PARSE_FAILED",
                "error": result.get("error", "文档解析失败"),
                "file_type": suffix.lstrip("."),
                "metadata": base_metadata,
            }
        return {
            "success": True,
            "file_type": suffix.lstrip("."),
            "content": result["content"],
            "metadata": base_metadata,
        }

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
