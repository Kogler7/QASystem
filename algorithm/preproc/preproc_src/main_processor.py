from pathlib import Path
from typing import Dict, Any, Optional
import os
import shutil
from datetime import datetime


class DocumentProcessor:
    def __init__(self, output_dir: Optional[str] = None,
                 mineru_config_path: Optional[str] = None,
                 mineru_model_source: str = "local",
                 libreoffice_path: Optional[str] = None):
        """
        初始化文档处理器
        :param output_dir: MinerU 输出的基础目录，如果为 None 则使用当前目录下的 'processed' 文件夹
        :param mineru_config_path: MinerU 配置文件路径（如 mineru.json）
        :param mineru_model_source: MinerU 模型源，默认 "local"
        """
        from .processors.pdf_processor import PDFProcessor
        from .processors.docx_processor import DocxProcessor
        from .processors.excel_processor import ExcelProcessor
        from .processors.html_processor import HTMLProcessor
        from .processors.ocr_processor import OCRProcessor   # 这是 MinerU 封装

        # 设置输出目录（新增）
        if output_dir is None:
            output_dir = os.path.join(os.getcwd(), "processed")
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.pdf_processor = PDFProcessor()
        # LibreOffice/soffice path precedence: explicit parameter > environment vars > auto-detect in DocxProcessor
        _lo = libreoffice_path or os.getenv("LIBREOFFICE_PATH") or os.getenv("SOFFICE_PATH")
        self.docx_processor = DocxProcessor(libreoffice_path=_lo or None)
        self.excel_processor = ExcelProcessor()
        self.html_processor = HTMLProcessor()
        # self.ocr_processor = OCRProcessor()      # 旧的 OCR 处理器（如果存在）

        # Determine MinerU config path precedence:
        # 1) explicit mineru_config_path argument
        # 2) local algorithm/preproc/mineru.json if it exists
        # 3) environment variable MINERU_CONFIG_PATH
        _preproc_root = Path(__file__).resolve().parent.parent
        _default_mineru = _preproc_root / "mineru.json"
        if mineru_config_path is None and _default_mineru.is_file():
            mineru_config_path = str(_default_mineru)
        if mineru_config_path is None:
            mineru_config_path = os.getenv("MINERU_CONFIG_PATH")
        self.mineru_processor = OCRProcessor(
            config_path=mineru_config_path,
            model_source=mineru_model_source,
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
                    },
                    'docling': {
                        'processor': self.docx_processor,
                        'metadata': {
                            'name': 'Docling PDF',
                            'description': '基于Docling库的PDF文档解析器',
                            '适用场景': '文本型PDF、排版相对规整的文档',
                            '优点': '处理速度快，输出结构清晰',
                            '缺点': '对扫描版或复杂布局PDF效果可能弱于MinerU'
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
                    },
                    'mineru': {
                        'processor': self.mineru_processor,
                        'metadata': {
                            'name': 'MinerU (Word转PDF)',
                            'description': '先将Word转换为PDF，再使用MinerU进行解析',
                            '适用场景': '包含复杂排版、图文混排的Word文档',
                            '优点': '对复杂版式鲁棒性更高',
                            '缺点': '处理链路更长，转换和解析耗时较高'
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
                    },
                    'mineru': {
                        'processor': self.mineru_processor,
                        'metadata': {
                            'name': 'MinerU (DOC转PDF)',
                            'description': '先将DOC转换为PDF，再使用MinerU进行解析',
                            '适用场景': '旧版Word文档(.doc)且版式复杂',
                            '优点': '复杂版式提取能力强',
                            '缺点': '依赖LibreOffice转换，耗时较高'
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
            },
            '.html': {
                'default': 'bs4',
                'models': {
                    'bs4': {
                        'processor': self.html_processor,
                        'metadata': {
                            'name': 'BeautifulSoup HTML',
                            'description': '基于BeautifulSoup与lxml的HTML结构化解析器',
                            '适用场景': '网页导出文档、说明文档、知识页面等HTML内容',
                            '优点': '可清理脚本样式并提取结构化正文，支持表格转Markdown',
                            '缺点': '复杂动态页面（JS渲染）内容可能不完整'
                        }
                    }
                }
            },
            '.htm': {
                'default': 'bs4',
                'models': {
                    'bs4': {
                        'processor': self.html_processor,
                        'metadata': {
                            'name': 'BeautifulSoup HTML',
                            'description': '基于BeautifulSoup与lxml的HTML结构化解析器',
                            '适用场景': '网页导出文档、说明文档、知识页面等HTML内容',
                            '优点': '可清理脚本样式并提取结构化正文，支持表格转Markdown',
                            '缺点': '复杂动态页面（JS渲染）内容可能不完整'
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
        temp_pdf_path = None
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
            if file_type == '.pdf' or (file_type in {'.docx', '.doc'} and model_name == 'mineru'):
                # 后端选择：优先使用环境变量 MINERU_BACKEND，否则默认使用 'vlm'（可改回 'pipeline'）
                backend_choice = os.getenv('MINERU_BACKEND') or 'vlm'
                kwargs = {
                    'output_dir': str(self.output_dir),
                    'lang': "ch",
                    'backend': backend_choice,
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

            process_path = file_path
            if file_type in {'.docx', '.doc'} and model_name == 'mineru':
                temp_pdf_path = self.docx_processor.convert_to_pdf(file_path)
                if not temp_pdf_path or not os.path.exists(temp_pdf_path):
                    return {
                        'success': False,
                        'file_type': 'word',
                        'error': 'Word 转 PDF 失败，无法使用 MinerU 解析（请检查 LibreOffice 配置）'
                    }
                process_path = temp_pdf_path

            # 确定文件类型字符串
            file_type_str = {
                '.pdf': 'pdf',
                '.docx': 'word',
                '.doc': 'word',
                '.xlsx': 'excel',
                '.xls': 'excel',
                '.csv': 'csv',
                '.html': 'html',
                '.htm': 'html'
            }.get(file_type, file_type.lstrip('.'))

            # 调用处理器
            result = processor.process(process_path, **kwargs)

            processor_metadata = result.get('metadata') or {}
            processor_content = result.get('content')
            processor_error = processor_metadata.get('error')

            if processor_error:
                return {
                    'success': False,
                    'file_type': file_type_str,
                    'error': str(processor_error),
                    'metadata': {
                        **metadata,
                        **processor_metadata,
                        'model_used': model_name,
                        'model_info': model_info,
                    }
                }

            if (file_type == '.pdf' or model_name == 'mineru') and (processor_content is None or not str(processor_content).strip()):
                return {
                    'success': False,
                    'file_type': file_type_str,
                    'error': f'{model_name} 解析未生成有效内容，请检查模型配置或输入文件质量',
                    'metadata': {
                        **metadata,
                        **processor_metadata,
                        'model_used': model_name,
                        'model_info': model_info,
                    }
                }

            # 合并元数据
            metadata.update(processor_metadata)
            metadata['model_used'] = model_name
            metadata['model_info'] = model_info

            return {
                'success': True,
                'file_type': file_type_str,
                'content': processor_content,
                'metadata': metadata
            }

        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }
        finally:
            if temp_pdf_path:
                try:
                    temp_dir = os.path.dirname(temp_pdf_path)
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass

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