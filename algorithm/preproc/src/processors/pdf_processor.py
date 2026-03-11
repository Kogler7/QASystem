"""
PDF 处理器占位模块。PDF 实际由 MinerU（ocr_processor）在 main_processor 中处理，
此处仅提供 PDFProcessor 类以满足 main_processor 的导入，不参与实际 PDF 解析。
"""
from typing import Any, Optional, Dict
from . import BaseProcessor


class PDFProcessor(BaseProcessor):
    """占位：PDF 解析由 MinerU/OCRProcessor 在 DocumentProcessor.process_file 中完成。"""

    def __init__(self, **kwargs: Any) -> None:
        pass

    def process(self, file_path: str, **kwargs: Any) -> Dict[str, Any]:
        """占位，实际不调用。"""
        return {
            'content': '',
            'metadata': {
                'file_type': 'pdf',
                'processor': 'placeholder',
                'note': 'PDF processing is handled by OCRProcessor (MinerU)'
            }
        }
