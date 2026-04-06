from docling.datamodel.base_models import InputFormat
from docling.document_converter import DocumentConverter
import os
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
        self.converter = DocumentConverter(
            allowed_formats=[InputFormat.DOCX]
        )
        self.pdf_converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF]
        )
        self.libreoffice_path = libreoffice_path or self._detect_office_app()

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
            'supported_formats': ['.pdf', '.docx', '.doc']
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
        else:
            content = f"错误：不支持的文件格式 - {ext}"
            metadata['error'] = f'不支持的文件格式 - {ext}'

        return {
            'content': content,
            'metadata': metadata
        }

    def _process_pdf(self, file_path: str) -> str:
        """使用 Docling 处理 PDF 文件"""
        try:
            result = self.pdf_converter.convert(file_path)
            return result.document.export_to_markdown()
        except Exception as e:
            print(f"使用docling处理PDF失败: {e}")
            return f"处理失败：{str(e)}"

    def _process_docx(self, file_path: str) -> str:
        """处理DOCX文件"""
        try:
            result = self.converter.convert(file_path)
            return result.document.export_to_markdown()
        except Exception as e:
            print(f"使用docling处理DOCX失败: {e}")
            return f"处理失败：{str(e)}"

    def _process_doc(self, file_path: str) -> str:
        """处理DOC文件：先转换再处理"""
        # 转换DOC为DOCX
        docx_path = self._convert_to_docx(file_path)

        if not docx_path or not os.path.exists(docx_path):
            return "错误：无法将DOC文件转换为DOCX格式"

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
        if not self.libreoffice_path:
            return None

        try:
            # 创建临时目录
            temp_dir = tempfile.mkdtemp(prefix='doc_convert_')
            output_file = os.path.join(
                temp_dir,
                os.path.basename(doc_path).replace('.doc', '.docx').replace('.DOC', '.docx')
            )

            # 构建转换命令
            cmd = [
                self.libreoffice_path,
                '--headless',
                '--convert-to', 'docx',
                '--outdir', temp_dir,
                doc_path
            ]

            # 执行转换
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60  # 60秒超时
            )

            if process.returncode == 0:
                # 查找生成的DOCX文件
                for file in os.listdir(temp_dir):
                    if file.lower().endswith('.docx'):
                        return os.path.join(temp_dir, file)

            print(f"转换失败：{process.stderr}")
            return None

        except subprocess.TimeoutExpired:
            print("转换超时")
            return None
        except Exception as e:
            print(f"转换过程出错：{e}")
            return None

    def convert_to_pdf(self, file_path: str) -> Optional[str]:
        """将 DOC/DOCX 文件转换为 PDF，返回生成的 PDF 路径"""
        if not self.libreoffice_path:
            return None

        try:
            temp_dir = tempfile.mkdtemp(prefix='doc_to_pdf_')
            stem = os.path.splitext(os.path.basename(file_path))[0]
            output_file = os.path.join(temp_dir, f"{stem}.pdf")

            cmd = [
                self.libreoffice_path,
                '--headless',
                '--convert-to', 'pdf',
                '--outdir', temp_dir,
                file_path
            ]

            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=90
            )

            if process.returncode == 0:
                for file in os.listdir(temp_dir):
                    if file.lower().endswith('.pdf'):
                        return os.path.join(temp_dir, file)

            print(f"DOC/DOCX 转 PDF 失败：{process.stderr}")
            return None
        except subprocess.TimeoutExpired:
            print("DOC/DOCX 转 PDF 超时")
            return None
        except Exception as e:
            print(f"DOC/DOCX 转 PDF 异常：{e}")
            return None
