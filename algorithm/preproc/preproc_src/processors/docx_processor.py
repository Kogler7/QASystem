from qasystem_config.configuration import getenv as config_getenv
from docling.datamodel.base_models import InputFormat
from docling.datamodel.settings import settings as docling_settings
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import PdfFormatOption
from docling.document_converter import DocumentConverter
import os
import shutil
import sys
import tempfile
import subprocess
from typing import Optional, Dict, Any
from . import BaseProcessor
from ..page_edges import (
    DEFAULT_MIN_CHARS,
    extract_edge_lines,
    merge_page_edges,
    missing_lines,
    normalize_keep_page_edges,
    page_is_kept,
)
from ..table_json import convert_tables
from ..text_layer import (
    merge_split_headings,
    page_text_lines,
    reconcile_markdown,
    tighten_number_artifacts,
)

DOCLING_MODELS_PATH = "/models/docling-models"


def get_docling_models_path() -> str:
    """Docling 离线模型目录：默认容器挂载点，可用 DOCLING_MODELS_PATH 覆盖。"""
    return config_getenv("DOCLING_MODELS_PATH") or DOCLING_MODELS_PATH


class DocxProcessor(BaseProcessor):
    def __init__(self, libreoffice_path: str = None):
        """
        初始化处理器

        Args:
            libreoffice_path: LibreOffice/OpenOffice路径，如果为None则自动查找
        """
        # Docling converters are expensive to initialize. Keep them lazy so MinerU's
        # image/Office-to-PDF preparation path does not load Docling models.
        self.converter = None
        self.pdf_converter = None
        self.libreoffice_path = libreoffice_path or self._detect_office_app()
        self.last_conversion_error: Optional[str] = None

    def _get_converter(self) -> DocumentConverter:
        if self.converter is None:
            self._ensure_docling_artifacts()
            self.converter = DocumentConverter(
                allowed_formats=[InputFormat.DOCX, InputFormat.PPTX, InputFormat.IMAGE]
            )
        return self.converter

    def _get_pdf_converter(self) -> DocumentConverter:
        if self.pdf_converter is None:
            self._ensure_docling_artifacts()
            models_path = get_docling_models_path()
            self.pdf_converter = DocumentConverter(
                allowed_formats=[InputFormat.PDF],
                format_options={
                    InputFormat.PDF: PdfFormatOption(
                        pipeline_options=PdfPipelineOptions(artifacts_path=models_path)
                    )
                },
            )
        return self.pdf_converter

    @staticmethod
    def _ensure_docling_artifacts() -> None:
        models_path = get_docling_models_path()
        if not os.path.isdir(models_path):
            raise RuntimeError(
                f"Docling 离线模型目录不存在：{models_path}；"
                "请在交付包的 models 目录中包含 docling-models，"
                "或用 DOCLING_MODELS_PATH 指向真实目录"
            )
        docling_settings.artifacts_path = models_path

    def _detect_office_app(self) -> Optional[str]:
        """检测系统中的LibreOffice应用"""
        # 可能的执行文件路径
        candidates = []
        for key in ("LIBREOFFICE_PATH",):
            v = config_getenv(key)
            if v:
                candidates.append(v)
        candidates.extend(
            [
                "soffice",
                "libreoffice",
                r"C:\Program Files\LibreOffice\program\soffice.exe",
            ]
        )

        for candidate in candidates:
            try:
                # 检查文件是否存在
                if os.path.isfile(candidate):
                    return candidate
                # 检查是否在PATH中
                result = subprocess.run(
                    ['which', candidate] if os.name != 'nt' else
                    ['where', candidate],
                    capture_output=True
                )
                if result.returncode == 0:
                    return candidate
            except:
                continue

        print("警告：未找到LibreOffice，DOC文件处理可能受限")
        return None

    def process(self, file_path: str, **kwargs) -> Dict[str, Any]:
        """统一处理文件，自动识别DOC/DOCX格式，返回包含 content 和 metadata 的字典"""
        metadata = {
            'file_type': 'word',
            'processor': 'docling',
            'supported_formats': ['.pdf', '.docx', '.doc', '.pptx', '.ppt', '.png', '.jpg', '.jpeg']
        }
        
        if not os.path.exists(file_path):
            return {
                'content': '',
                'metadata': {**metadata, 'error': f'文件不存在 - {file_path}'}
            }

        _, ext = os.path.splitext(file_path.lower())

        if ext == '.pdf':
            metadata['file_type'] = 'pdf'
            content, pdf_metadata = self._process_pdf(
                file_path,
                start_page_id=kwargs.get('start_page_id') or 0,
                end_page_id=kwargs.get('end_page_id'),
                keep_page_edges=kwargs.get('keep_page_edges'),
                page_edge_min_chars=kwargs.get('page_edge_min_chars'),
            )
            metadata.update(pdf_metadata)
        elif ext == '.docx':
            content = self._process_docx(file_path)
        elif ext == '.doc':
            content = self._process_doc(file_path)
        elif ext == '.pptx':
            metadata['file_type'] = 'presentation'
            content = self._process_with_converter(file_path, 'PPTX')
        elif ext == '.ppt':
            metadata['file_type'] = 'presentation'
            content = self._process_ppt(file_path)
        elif ext in {'.png', '.jpg', '.jpeg'}:
            metadata['file_type'] = 'image'
            content = self._process_with_converter(file_path, 'image')
        else:
            content = f"错误：不支持的文件格式 - {ext}"
            metadata['error'] = f'不支持的文件格式 - {ext}'

        if content.startswith(("处理失败：", "处理转换后的文件失败：", "错误：")):
            metadata['error'] = content

        return {
            'content': content,
            'metadata': metadata
        }

    def _process_pdf(
        self,
        file_path: str,
        *,
        start_page_id: int = 0,
        end_page_id: Optional[int] = None,
        keep_page_edges: Any = None,
        page_edge_min_chars: Optional[int] = None,
    ) -> tuple[str, Dict[str, Any]]:
        """使用 Docling 处理 PDF 文件。

        ``start_page_id`` / ``end_page_id`` 为 0 基闭区间页码（``None`` 表示到末页），
        内部换算为 Docling 的 1 基 ``page_range``。

        ``keep_page_edges`` 打开时，Docling 判为 FURNITURE（页眉/页脚）而默认不导出的
        内容，会按页从 PDF 文本层补回 Markdown，详见 ``preproc_src/page_edges.py``。
        """
        selection = normalize_keep_page_edges(keep_page_edges)
        min_chars = DEFAULT_MIN_CHARS if page_edge_min_chars is None else int(page_edge_min_chars)

        first_page = start_page_id + 1
        last_page = (end_page_id + 1) if end_page_id is not None else sys.maxsize
        page_range = (first_page, last_page) if (start_page_id or end_page_id is not None) else None

        try:
            # docling 不接受显式 page_range=None，未限定范围时保持默认调用方式。
            convert_kwargs: Dict[str, Any] = {"page_range": page_range} if page_range else {}
            result = self._get_pdf_converter().convert(file_path, **convert_kwargs)
            document = result.document
            pages = sorted(document.pages.keys())
            if not pages:
                return self._export_markdown(result), {}

            # 逐页导出：docling 的 page_break_placeholder 不会为空白页插入占位符
            # （标准封面背面、目次背面常见），按占位符切分会导致页号错位。
            # 逐页导出与整篇导出的内容等价，但页号始终与 document.pages 对齐。
            injected: list[dict] = []
            rendered: list[str] = []
            reconciled_lines = 0
            merged_lines = 0
            table_count = 0
            form_count = 0
            for index, page_no in enumerate(pages):
                chunk = document.export_to_markdown(
                    page_no=page_no, escape_html=False, escape_underscores=False
                )
                page_lines = page_text_lines(file_path, page_no)
                # 先合并被拆开的条款标题，再用文本层逐行对账，最后收拢编号内部空格。
                chunk, merged = merge_split_headings(chunk, page_lines)
                chunk, replaced = reconcile_markdown(chunk, page_lines)
                chunk = tighten_number_artifacts(chunk)
                chunk, stats = convert_tables(chunk, source_name=file_path)
                merged_lines += merged
                reconciled_lines += replaced
                table_count += stats["tables"]
                form_count += stats["forms"]
                if selection is not None and page_is_kept(selection, index + 1, 1):
                    edges = extract_edge_lines(file_path, page_no, min_chars=min_chars)
                    top = missing_lines(edges["top"], chunk)
                    bottom = missing_lines(edges["bottom"], chunk)
                    if top or bottom:
                        chunk = merge_page_edges(chunk, top, bottom)
                        injected.append({"page": page_no, "top": top, "bottom": bottom})
                rendered.append(chunk)
            content = "\n\n".join(text.strip() for text in rendered if text.strip())
            metadata: Dict[str, Any] = {
                "text_layer_reconcile": {
                    "pages": len(pages),
                    "replaced_lines": reconciled_lines,
                    "merged_heading_lines": merged_lines,
                },
                # 关闭时也记录，便于从元数据确认页边内容是有意不补回的。
                "page_edges": {"mode": str(keep_page_edges), "injected": injected},
                "tables_json": {"count": table_count, "forms": form_count},
            }
            return (content + "\n" if content else ""), metadata
        except Exception as e:
            print(f"使用docling处理PDF失败: {e}")
            return f"处理失败：{str(e)}", {}

    @staticmethod
    def _tables_to_json(markdown: str, source_name: str | None = None) -> str:
        """替换表格：普通文件出 JSON，表单类文件按阅读顺序还原为文本。"""
        converted, _stats = convert_tables(markdown, source_name=source_name)
        return converted

    def _process_docx(self, file_path: str, source_name: str | None = None) -> str:
        """处理DOCX文件"""
        try:
            result = self._get_converter().convert(file_path)
            return self._tables_to_json(self._export_markdown(result), source_name or file_path)
        except Exception as e:
            print(f"使用docling处理DOCX失败: {e}")
            return f"处理失败：{str(e)}"

    def _process_with_converter(self, file_path: str, label: str, source_name: str | None = None) -> str:
        """使用 Docling 原生转换器处理 PPTX 或图片。"""
        try:
            result = self._get_converter().convert(file_path)
            return self._tables_to_json(self._export_markdown(result), source_name or file_path)
        except Exception as e:
            print(f"使用docling处理{label}失败: {e}")
            return f"处理失败：{str(e)}"

    @staticmethod
    def _export_markdown(result) -> str:
        # 关闭 docling 的 HTML/下划线转义：本体 DSL 文档里的 <<Sterotype>> 和
        # DIM_TIME 型标识符会被转成 &lt;&lt;...&gt;&gt; / DIM\_TIME，下游知识抽取
        # 无法识别；直接上传 .md 的内容本身就是未转义原文，这里保持一致。
        return result.document.export_to_markdown(escape_html=False, escape_underscores=False)

    def _process_ppt(self, file_path: str) -> str:
        """旧版 PPT 先转换为 PPTX，再交给 Docling。"""
        pptx_path = self._convert_office_format(file_path, "pptx", "ppt_convert_")
        if not pptx_path:
            return f"错误：{self.last_conversion_error or '无法将 PPT 转换为 PPTX'}"
        try:
            return self._process_with_converter(pptx_path, 'PPT', source_name=file_path)
        finally:
            try:
                os.remove(pptx_path)
                os.rmdir(os.path.dirname(pptx_path))
            except Exception:
                pass

    def _process_doc(self, file_path: str) -> str:
        """处理DOC文件：先转换再处理"""
        # 转换DOC为DOCX
        docx_path = self._convert_to_docx(file_path)

        if not docx_path or not os.path.exists(docx_path):
            return f"错误：{self.last_conversion_error or '无法将 DOC 转换为 DOCX'}"

        try:
            # 处理转换后的DOCX文件（沿用原始文件名做表单判定）
            result = self._process_docx(docx_path, source_name=file_path)

            # 清理临时文件
            try:
                os.remove(docx_path)
                os.rmdir(os.path.dirname(docx_path))
            except:
                pass

            return result
        except Exception as e:
            # 清理临时文件
            try:
                if os.path.exists(docx_path):
                    os.remove(docx_path)
            except:
                pass
            return f"处理转换后的文件失败：{str(e)}"

    def _convert_to_docx(self, doc_path: str) -> Optional[str]:
        """将DOC文件转换为DOCX格式"""
        return self._convert_office_format(doc_path, "docx", "doc_convert_")

    def convert_to_pdf(self, file_path: str) -> Optional[str]:
        """将 Office 文档或图片转换为 PDF，供 MinerU 使用。"""
        suffix = os.path.splitext(file_path)[1].lower()
        if suffix in {'.png', '.jpg', '.jpeg'}:
            return self._convert_image_to_pdf(file_path)

        return self._convert_office_format(file_path, "pdf", "office_to_pdf_", timeout=90)

    def _convert_office_format(
        self,
        file_path: str,
        target_format: str,
        temp_prefix: str,
        timeout: int = 60,
    ) -> Optional[str]:
        """通过 LibreOffice 将 Office 文件转换为指定格式。"""
        self.last_conversion_error = None
        if not self.libreoffice_path:
            self.last_conversion_error = (
                "缺少 LibreOffice（soffice），无法转换 Office 文件；"
                "请检查镜像中的 libreoffice-core、libreoffice-writer 和 libreoffice-impress"
            )
            return None

        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp(prefix=temp_prefix)

            cmd = [
                self.libreoffice_path,
                '--headless',
                '--convert-to', target_format,
                '--outdir', temp_dir,
                file_path
            ]

            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            if process.returncode == 0:
                for file in os.listdir(temp_dir):
                    if file.lower().endswith(f'.{target_format.lower()}'):
                        return os.path.join(temp_dir, file)

            detail = (process.stderr or process.stdout or "LibreOffice 未生成目标文件").strip()
            self.last_conversion_error = f"LibreOffice 转换为 {target_format} 失败：{detail}"
            shutil.rmtree(temp_dir, ignore_errors=True)
            return None
        except subprocess.TimeoutExpired:
            self.last_conversion_error = f"LibreOffice 转换为 {target_format} 超时（{timeout} 秒）"
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            return None
        except Exception as e:
            self.last_conversion_error = f"LibreOffice 转换为 {target_format} 异常：{e}"
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            return None

    def _convert_image_to_pdf(self, file_path: str) -> Optional[str]:
        """将图片无损地封装为单页 PDF。"""
        self.last_conversion_error = None
        temp_dir = None
        try:
            from PIL import Image

            temp_dir = tempfile.mkdtemp(prefix='image_to_pdf_')
            output_file = os.path.join(temp_dir, f"{os.path.splitext(os.path.basename(file_path))[0]}.pdf")
            with Image.open(file_path) as source:
                if source.mode in {"RGBA", "LA"}:
                    rgba = source.convert("RGBA")
                    image = Image.new("RGB", rgba.size, "white")
                    image.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    image = source.convert("RGB")
                image.save(output_file, "PDF", resolution=150.0)
            return output_file
        except ImportError:
            self.last_conversion_error = "缺少 Pillow，无法将图片转换为 PDF"
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            return None
        except Exception as e:
            self.last_conversion_error = f"图片转换为 PDF 失败，请检查文件是否损坏或格式是否正确：{e}"
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            return None
