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

        self.pdf_processor = PDFProcessor()      
        self.docx_processor = DocxProcessor(libreoffice_path="/home/admin_user_3/cly/LibreOffice-fresh.full-x86_64.AppImage")
        self.excel_processor = ExcelProcessor()
        # self.html_processor = HTMLProcessor()
        # self.ocr_processor = OCRProcessor()      # 旧的 OCR 处理器（如果存在）

        # 初始化 MinerU 处理器
        if mineru_config_path is None:
            mineru_config_path = os.getenv("MINERU_CONFIG_PATH", "/home/admin_user_3/mineru.json")
        self.mineru_processor = OCRProcessor(   
            config_path=mineru_config_path,
            model_source=mineru_model_source
        )

        # 构建模型注册表（硬编码版）
        self.available_processors = {
            '.pdf': {
                'default': 'mineru',
                'models': {
                    'mineru': {
                        'processor': self.mineru_processor,
                        'metadata': {
                            'name': 'MinerU',
                            'description': '基于深度学习的PDF文档解析工具',
                            '适用场景': '扫描版PDF、复杂布局PDF、包含表格和公式的文档',
                            '优点': '解析准确度高，支持多种元素识别，输出Markdown格式',
                            '缺点': '处理速度较慢，需要GPU加速，对简单文本PDF可能过度'
                        }
                    }
                }
            },
            '.docx': {
                'default': 'docling',
                'models': {
                    'docling': {
                        'processor': self.docx_processor,
                        'metadata': {
                            'name': 'Docling',
                            'description': '基于Docling库的Word文档解析器',
                            '适用场景': '现代Word文档(.docx)，包含文本、表格、图片',
                            '优点': '解析速度快，保持文档结构，输出Markdown',
                            '缺点': '不支持旧版.doc文件，需要LibreOffice转换'
                        }
                    }
                }
            },
            '.doc': {
                'default': 'docling',
                'models': {
                    'docling': {
                        'processor': self.docx_processor,
                        'metadata': {
                            'name': 'Docling (DOC转换)',
                            'description': '通过LibreOffice转换DOC为DOCX后使用Docling解析',
                            '适用场景': '旧版Word文档(.doc)',
                            '优点': '支持旧格式，保持结构',
                            '缺点': '转换过程慢，可能丢失格式'
                        }
                    }
                }
            },
            '.xlsx': {
                'default': 'pandas',
                'models': {
                    'pandas': {
                        'processor': self.excel_processor,
                        'metadata': {
                            'name': 'Pandas Excel',
                            'description': '基于Pandas的Excel文件解析器',
                            '适用场景': '结构化数据表格，多种格式支持',
                            '优点': '快速处理，数据完整性好',
                            '缺点': '不保留格式信息，仅提取数据'
                        }
                    }
                }
            },
            '.xls': {
                'default': 'pandas',
                'models': {
                    'pandas': {
                        'processor': self.excel_processor,
                        'metadata': {
                            'name': 'Pandas Excel',
                            'description': '基于Pandas的Excel文件解析器',
                            '适用场景': '旧版Excel文件(.xls)',
                            '优点': '兼容旧格式，数据提取准确',
                            '缺点': '不保留视觉格式'
                        }
                    }
                }
            },
            '.csv': {
                'default': 'pandas',
                'models': {
                    'pandas': {
                        'processor': self.excel_processor,
                        'metadata': {
                            'name': 'Pandas CSV',
                            'description': '基于Pandas的CSV文件解析器',
                            '适用场景': '纯文本数据文件，逗号分隔',
                            '优点': '处理速度极快，自动检测分隔符',
                            '缺点': '不支持复杂编码或多行记录'
                        }
                    }
                }
            }
        }

    def process_file(self, file_path: str, model_name: Optional[str] = None) -> Dict[str, Any]:
        """
        处理单个文件，支持指定模型
        :param file_path: 文件路径
        :param model_name: 可选的模型名称，如果为None则使用默认模型
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

            # 检查文件类型是否支持
            if file_type not in self.available_processors:
                return {'success': False, 'error': f'不支持的文件类型: {file_type}'}

            processor_config = self.available_processors[file_type]

            # 确定使用的模型
            if model_name is None:
                model_name = processor_config['default']
            if model_name not in processor_config['models']:
                return {'success': False, 'error': f'不支持的模型: {model_name} 对于文件类型 {file_type}'}

            processor = processor_config['models'][model_name]['processor']
            model_info = processor_config['models'][model_name]['metadata']

            # 准备处理器参数
            kwargs = {}
            if file_type == '.pdf':
                kwargs = {
                    'output_dir': str(self.output_dir),
                    'lang': "ch",
                    'backend': "pipeline",
                    'method': "auto",
                    'formula_enable': True,
                    'table_enable': True,
                    'draw_layout_bbox': False,
                    'draw_span_bbox': False,
                    'dump_md': True,
                    'dump_middle_json': False,
                    'dump_model_output': False,
                    'dump_orig_pdf': False,
                    'dump_content_list': False,
                    'start_page_id': 0,
                    'end_page_id': None
                }

            # 确定文件类型字符串
            file_type_str = {
                '.pdf': 'pdf',
                '.docx': 'word',
                '.doc': 'word',
                '.xlsx': 'excel',
                '.xls': 'excel',
                '.csv': 'csv'
            }.get(file_type, file_type.lstrip('.'))

            # 调用处理器
            result = processor.process(file_path, **kwargs)

            # 合并元数据
            metadata.update(result['metadata'])
            metadata['model_used'] = model_name
            metadata['model_info'] = model_info

            return {
                'success': True,
                'file_type': file_type_str,
                'content': result['content'],
                'metadata': metadata
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