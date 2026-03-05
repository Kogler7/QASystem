from pathlib import Path
from typing import Dict, Any, Optional
import os
from datetime import datetime


class DocumentProcessor:
    def __init__(self, mineru_config_path: Optional[str] = None, mineru_model_source: str = "local"):
        """
        初始化文档处理器
        :param mineru_config_path: MinerU 配置文件路径（如 mineru.json），若为 None 则从环境变量 MINERU_CONFIG_PATH 读取
        :param mineru_model_source: MinerU 模型源，默认 "local" 表示使用本地已下载模型
        """
        from .processors.pdf_processor import PDFProcessor
        from .processors.docx_processor import DocxProcessor
        from .processors.excel_processor import ExcelProcessor
        # from .processors.html_processor import HTMLProcessor
        from .processors.ocr_processor import OCRProcessor

        self.pdf_processor = PDFProcessor()
        self.docx_processor = DocxProcessor()
        self.excel_processor = ExcelProcessor()
        # self.html_processor = HTMLProcessor()
        self.ocr_processor = OCRProcessor()

        # 初始化 MinerU 处理器
        if mineru_config_path is None:
            mineru_config_path = os.getenv("MINERU_CONFIG_PATH", "C:/Users/chengly/mineru.json")
        self.mineru_processor = OCRProcessor(
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

            # if file_type == '.pdf':
            #     # 使用OCR处理器处理PDF
            #     content, is_scanned = self.ocr_processor.process_pdf(file_path)
            #     metadata['is_scanned'] = is_scanned
            #
            #     return {
            #         'success': True,
            #         'file_type': 'pdf',
            #         'content': content,
            #         'metadata': metadata
            #     }

            if file_type == '.pdf':
                # 使用 MinerU 处理 PDF
                # 调用 MinerU 解析，输出到 self.output_dir / path.stem
                result_dirs = self.mineru_processor.process(
                    input_paths=[file_path],
                    output_dir=str(self.output_dir),
                    lang="ch",  # 可根据需要配置或从参数传入
                    backend="pipeline",  # 可根据需要选择 "pipeline" 或 "vlm-transformers"
                    method="auto",
                    formula_enable=True,
                    table_enable=True,
                    draw_layout_bbox=False,  # 可选，如需可视化可设为 True
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

                # 记录 MinerU 输出路径
                metadata['mineru_output_dir'] = str(file_output_dir)
                metadata['is_scanned'] = False  # MinerU 不区分扫描版，默认设为 False

                return {
                    'success': True,
                    'file_type': 'pdf',
                    'content': content,
                    'metadata': metadata
                }

            elif file_type == ['.docx', '.doc'] :
                content = self.docx_processor.process(file_path)
                return {
                    'success': True,
                    'file_type': 'word',
                    'content': content,
                    'metadata': metadata
                }

            elif file_type in ['.xlsx', '.xls']:
                content = self.excel_processor.process_excel(file_path)
                return {
                    'success': True,
                    'file_type': 'excel',
                    'content': content,
                    'metadata': metadata
                }

            elif file_type in '.csv':
                content = self.excel_processor.process_excel(file_path)
                return {
                    'success': True,
                    'file_type': 'csv',
                    'content': content,
                    'metadata': metadata
                }

            # elif file_type in ['.html', '.htm']:
            #     with open(file_path, 'r', encoding='utf-8') as f:
            #         html_content = f.read()
            #     content, html_metadata = self.html_processor.process_html(
            #         html_content, is_url=False
            #     )
            #     metadata.update(html_metadata)
            #
            #     return {
            #         'success': True,
            #         'file_type': 'html',
            #         'content': content,
            #         'metadata': metadata
            #     }

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