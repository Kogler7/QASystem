from pathlib import Path
from typing import Dict, Any, Optional
import os
from datetime import datetime


class DocumentProcessor:
    def __init__(self, output_dir: Optional[str] = None,
                 mineru_config_path: Optional[str] = None,
                 mineru_model_source: str = "local"):
        """
        初始化文档处理器
        :param output_dir: MinerU 输出的基础目录，如果为 None 则使用当前目录下的 'processed' 文件夹
        :param mineru_config_path: MinerU 配置文件路径（如 mineru.json）
        :param mineru_model_source: MinerU 模型源，默认 "local"
        """
        from .processors.pdf_processor import PDFProcessor
        from .processors.docx_processor import DocxProcessor
        from .processors.excel_processor import ExcelProcessor
        # from .processors.html_processor import HTMLProcessor
        from .processors.ocr_processor import OCRProcessor   # 这是 MinerU 封装

        # 设置输出目录（新增）
        if output_dir is None:
            output_dir = os.path.join(os.getcwd(), "processed")
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.pdf_processor = PDFProcessor()      # 可能不再使用，但保留
        self.docx_processor = DocxProcessor()
        self.excel_processor = ExcelProcessor()
        # self.html_processor = HTMLProcessor()
        self.ocr_processor = OCRProcessor()      # 旧的 OCR 处理器（如果存在）

        # 初始化 MinerU 处理器
        if mineru_config_path is None:
            mineru_config_path = os.getenv("MINERU_CONFIG_PATH", "C:/Users/chengly/mineru.json")
        self.mineru_processor = OCRProcessor(    # MinerU 封装（类名与上面相同，但实际已重构）
            config_path=mineru_config_path,
            model_source=mineru_model_source
        )

    def process_file(self, file_path: str) -> Dict[str, Any]:
        """
        处理单个文件
        返回格式: {
            'success': bool,
            'file_type': str,
            'content': Any,  # Markdown文本或Excel数据
            'metadata': Dict,
            'error': str
        }
        """
        try:
            path = Path(file_path)
            if not path.exists():
                return {'success': False, 'error': '文件不存在'}

            file_type = path.suffix.lower()
            file_size = path.stat().st_size

            metadata = {
                'file_name': path.name,
                'file_path': str(path.absolute()),
                'file_size': file_size,
                'file_type': file_type,
                'processed_at': datetime.now().isoformat()
            }

            # PDF 处理（使用 MinerU）
            if file_type == '.pdf':
                # 调用 MinerU 解析，输出到 self.output_dir / path.stem
                result_dirs = self.mineru_processor.process(
                    input_paths=[file_path],
                    output_dir=str(self.output_dir),          # 使用类的输出目录
                    lang="ch",
                    backend="pipeline",
                    method="auto",
                    formula_enable=True,
                    table_enable=True,
                    draw_layout_bbox=False,
                    draw_span_bbox=False,
                    dump_md=True,
                    dump_middle_json=False,
                    dump_model_output=False,
                    dump_orig_pdf=False,
                    dump_content_list=False,
                    start_page_id=0,
                    end_page_id=None
                )

                # 解析结果目录为 output_dir/文件名/
                file_output_dir = self.output_dir / path.stem
                md_file = file_output_dir / f"{path.stem}.md"
                if md_file.exists():
                    content = md_file.read_text(encoding='utf-8')
                else:
                    content = ""

                metadata['mineru_output_dir'] = str(file_output_dir)
                metadata['is_scanned'] = False

                return {
                    'success': True,
                    'file_type': 'pdf',
                    'content': content,
                    'metadata': metadata
                }

            # 修复：DOCX/DOC 判断
            elif file_type in ['.docx', '.doc']:
                content = self.docx_processor.process(file_path)
                return {
                    'success': True,
                    'file_type': 'word',
                    'content': content,
                    'metadata': metadata
                }

            # Excel 文件
            elif file_type in ['.xlsx', '.xls']:
                content = self.excel_processor.process_excel(file_path)
                return {
                    'success': True,
                    'file_type': 'excel',
                    'content': content,
                    'metadata': metadata
                }

            # 修复：CSV 判断
            elif file_type == '.csv':
                content = self.excel_processor.process_excel(file_path)
                return {
                    'success': True,
                    'file_type': 'csv',
                    'content': content,
                    'metadata': metadata
                }

            # 其他文件类型（如 HTML）可在此扩展
            else:
                return {
                    'success': False,
                    'error': f'不支持的文件类型: {file_type}'
                }

        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    def process_directory(self, directory: str) -> Dict[str, Any]:
        """
        处理目录中的所有文件
        """
        results = {
            'total_files': 0,
            'successful': 0,
            'failed': 0,
            'results': [],
            'start_time': datetime.now().isoformat()
        }

        path = Path(directory)
        if not path.exists() or not path.is_dir():
            return {'error': '目录不存在'}

        # 遍历所有支持的文件
        for ext in ['.pdf', '.docx', '.doc', '.xlsx', '.xls', '.csv', '.html', '.htm']:
            for file_path in path.rglob(f'*{ext}'):
                results['total_files'] += 1
                print(f"处理文件: {file_path}")
                result = self.process_file(str(file_path))
                results['results'].append({
                    'file': str(file_path),
                    'result': result
                })
                if result['success']:
                    results['successful'] += 1
                else:
                    results['failed'] += 1

        results['end_time'] = datetime.now().isoformat()
        return results