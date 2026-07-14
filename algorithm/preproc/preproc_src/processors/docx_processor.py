from docling.datamodel.base_models import InputFormat
from docling.document_converter import DocumentConverter
import os
import shutil
import tempfile
import subprocess
from typing import Optional, Dict, Any
from . import BaseProcessor

# import os
# os.environ["HF_HOME"] = "/home/admin_user_3/.cache/huggingface"

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
            self.converter = DocumentConverter(
                allowed_formats=[InputFormat.DOCX, InputFormat.PPTX, InputFormat.IMAGE]
            )
        return self.converter

    def _get_pdf_converter(self) -> DocumentConverter:
        if self.pdf_converter is None:
            self.pdf_converter = DocumentConverter(allowed_formats=[InputFormat.PDF])
        return self.pdf_converter

    def _detect_office_app(self) -> Optional[str]:
        """检测系统中的LibreOffice应用"""
        # 可能的执行文件路径
        candidates = []
        for key in ("LIBREOFFICE_PATH", "SOFFICE_PATH"):
            v = os.getenv(key)
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
            content = self._process_pdf(file_path)
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

    def _process_pdf(self, file_path: str) -> str:
        """使用 Docling 处理 PDF 文件"""
        try:
            result = self._get_pdf_converter().convert(file_path)
            return result.document.export_to_markdown()
        except Exception as e:
            print(f"使用docling处理PDF失败: {e}")
            return f"处理失败：{str(e)}"

    def _process_docx(self, file_path: str) -> str:
        """处理DOCX文件"""
        try:
            result = self._get_converter().convert(file_path)
            return result.document.export_to_markdown()
        except Exception as e:
            print(f"使用docling处理DOCX失败: {e}")
            return f"处理失败：{str(e)}"

    def _process_with_converter(self, file_path: str, label: str) -> str:
        """使用 Docling 原生转换器处理 PPTX 或图片。"""
        try:
            result = self._get_converter().convert(file_path)
            return result.document.export_to_markdown()
        except Exception as e:
            print(f"使用docling处理{label}失败: {e}")
            return f"处理失败：{str(e)}"

    def _process_ppt(self, file_path: str) -> str:
        """旧版 PPT 先转换为 PPTX，再交给 Docling。"""
        pptx_path = self._convert_office_format(file_path, "pptx", "ppt_convert_")
        if not pptx_path:
            return f"错误：{self.last_conversion_error or '无法将 PPT 转换为 PPTX'}"
        try:
            return self._process_with_converter(pptx_path, 'PPT')
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
            # 处理转换后的DOCX文件
            result = self._process_docx(docx_path)

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
