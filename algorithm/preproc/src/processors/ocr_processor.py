import os
import json
from pathlib import Path
from typing import List, Optional, Union, Dict, Any
from loguru import logger
from . import BaseProcessor

# MinerU 内部模块导入（确保 mineru 已安装且环境变量正确）
from mineru.cli.common import convert_pdf_bytes_to_bytes_by_pypdfium2, prepare_env, read_fn
from mineru.data.data_reader_writer import FileBasedDataWriter
from mineru.backend.pipeline.pipeline_analyze import doc_analyze as pipeline_doc_analyze
from mineru.backend.pipeline.pipeline_middle_json_mkcontent import union_make as pipeline_union_make
from mineru.backend.pipeline.model_json_to_middle_json import result_to_middle_json as pipeline_result_to_middle_json
from mineru.backend.vlm.vlm_analyze import doc_analyze as vlm_doc_analyze
from mineru.backend.vlm.vlm_middle_json_mkcontent import union_make as vlm_union_make
from mineru.utils.draw_bbox import draw_layout_bbox, draw_span_bbox
from mineru.utils.enum_class import MakeMode
from mineru.utils.guess_suffix_or_lang import guess_suffix_by_path


class OCRProcessor(BaseProcessor):
    """
    MinerU PDF/图像解析处理器，封装了底层API调用。
    支持 pipeline 和 vlm 两种后端。
    """

    def __init__(self, config_path: Optional[str] = None, model_source: str = "local"):
        """
        初始化处理器。

        :param config_path: MinerU 配置文件路径（如 mineru.json），若提供则设置环境变量 MINERU_CONFIG_PATH
        :param model_source: 模型源，可选 'local', 'modelscope', 'huggingface'。默认 'local' 表示使用本地已下载的模型。
        """
        # os.environ["MINERU_CONFIG_PATH"] = '/home/admin_user_3/mineru.json'
        os.environ["MINERU_CONFIG_PATH"] = '/mnt/data3/srv/vllm/models/'
        os.environ["MINERU_MODEL_SOURCE"] = 'local'
        # if config_path:
        #     os.environ["MINERU_CONFIG_PATH"] = config_path
        # os.environ["MINERU_MODEL_SOURCE"] = model_source
        logger.info(f"MinerU initialized with config: {config_path}, model source: {model_source}")

    def process(self, file_path: str, **kwargs) -> Dict[str, Any]:
        """
        处理单个文件，返回包含 content 和 metadata 的字典。
        """
        metadata = {
            'file_type': 'pdf',
            'processor': 'mineru',
            'supported_formats': ['.pdf'],
            'backend': kwargs.get('backend', 'pipeline'),
            'method': kwargs.get('method', 'auto')
        }
        
        try:
            output_dir = kwargs.get('output_dir', '/tmp/mineru_output')
            result_dirs = self._process_batch(
                input_paths=[file_path],
                output_dir=output_dir,
                lang=kwargs.get('lang', 'ch'),
                backend=kwargs.get('backend', 'pipeline'),
                method=kwargs.get('method', 'auto'),
                formula_enable=kwargs.get('formula_enable', True),
                table_enable=kwargs.get('table_enable', True),
                draw_layout_bbox=kwargs.get('draw_layout_bbox', False),
                draw_span_bbox=kwargs.get('draw_span_bbox', False),
                dump_md=kwargs.get('dump_md', True),
                dump_middle_json=kwargs.get('dump_middle_json', False),
                dump_model_output=kwargs.get('dump_model_output', False),
                dump_orig_pdf=kwargs.get('dump_orig_pdf', False),
                dump_content_list=kwargs.get('dump_content_list', False),
                start_page_id=kwargs.get('start_page_id', 0),
                end_page_id=kwargs.get('end_page_id', None)
            )
            
            # 读取生成的 Markdown 文件
            file_output_dir = Path(output_dir) / Path(file_path).stem / "auto"
            md_file = file_output_dir / f"{Path(file_path).stem}.md"
            if md_file.exists():
                content = md_file.read_text(encoding='utf-8')
            else:
                content = ""
                metadata['error'] = 'Markdown file not found'
            
            metadata['output_dir'] = str(file_output_dir)
            
            return {
                'content': content,
                'metadata': metadata
            }
        except Exception as e:
            return {
                'content': '',
                'metadata': {**metadata, 'error': str(e)}
            }

    def _process_batch(
        self,
        input_paths: List[Union[str, Path]],
        output_dir: Union[str, Path],
        lang: str = "ch",
        backend: str = "pipeline",
        method: str = "auto",
        server_url: Optional[str] = None,
        formula_enable: bool = True,
        table_enable: bool = True,
        draw_layout_bbox: bool = True,
        draw_span_bbox: bool = True,
        dump_md: bool = True,
        dump_middle_json: bool = True,
        dump_model_output: bool = False,
        dump_orig_pdf: bool = True,
        dump_content_list: bool = True,
        start_page_id: int = 0,
        end_page_id: Optional[int] = None,
    ) -> List[Path]:
        """
        :param output_dir: 输出根目录，每个文件的解析结果会放在 output_dir/文件名/ 下
        :param lang: 文档语言（仅 pipeline 后端有效），如 'ch', 'en' 等
        :param backend: 后端，可选 'pipeline', 'vlm-transformers', 'vlm-vllm-engine', 'vlm-http-client' 等
        :param method: 解析方法（仅 pipeline），'auto', 'txt', 'ocr'
        :param server_url: 当 backend 为 'vlm-http-client' 时需要指定服务器 URL
        :param formula_enable: 是否解析公式
        :param table_enable: 是否解析表格
        :param draw_layout_bbox: 是否绘制布局框并输出 PDF
        :param draw_span_bbox: 是否绘制文本块框并输出 PDF
        :param dump_md: 是否输出 Markdown 文件
        :param dump_middle_json: 是否输出中间 JSON 文件（包含结构化信息）
        :param dump_model_output: 是否输出模型原始输出 JSON
        :param dump_orig_pdf: 是否复制原始 PDF 到输出目录
        :param dump_content_list: 是否输出内容列表 JSON
        :param start_page_id: 起始页码（0 表示第一页）
        :param end_page_id: 结束页码（None 表示到最后一页）
        :return: 每个输出目录的路径列表
        """
        # 将输入路径统一为 Path 对象
        paths = [Path(p) for p in input_paths]
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # 准备输入数据：文件名、文件字节、语言列表
        file_names = []
        pdf_bytes_list = []
        lang_list = []

        for path in paths:
            if not path.exists():
                logger.warning(f"文件不存在，跳过: {path}")
                continue
            file_names.append(path.stem)          # 不含扩展名的文件名
            pdf_bytes_list.append(read_fn(path))  # 读取文件为字节
            lang_list.append(lang)

        if not file_names:
            logger.error("没有有效的输入文件")
            return []

        # 调用底层解析函数
        if backend.startswith("pipeline"):
            # 处理 pipeline 后端
            # 先对每个 PDF 进行页面裁剪（如果需要）
            for i, pdf_bytes in enumerate(pdf_bytes_list):
                pdf_bytes_list[i] = convert_pdf_bytes_to_bytes_by_pypdfium2(
                    pdf_bytes, start_page_id, end_page_id
                )

            infer_results, all_image_lists, all_pdf_docs, lang_list_out, ocr_enabled_list = pipeline_doc_analyze(
                pdf_bytes_list,
                lang_list,
                parse_method=method,
                formula_enable=formula_enable,
                table_enable=table_enable
            )

            for idx, model_list in enumerate(infer_results):
                pdf_file_name = file_names[idx]
                local_image_dir, local_md_dir = prepare_env(str(output_dir), pdf_file_name, method)
                image_writer = FileBasedDataWriter(local_image_dir)
                md_writer = FileBasedDataWriter(local_md_dir)

                images_list = all_image_lists[idx]
                pdf_doc = all_pdf_docs[idx]
                _lang = lang_list_out[idx]
                _ocr_enable = ocr_enabled_list[idx]

                middle_json = pipeline_result_to_middle_json(
                    model_list, images_list, pdf_doc, image_writer,
                    _lang, _ocr_enable, formula_enable
                )

                self._process_output(
                    pdf_info=middle_json["pdf_info"],
                    pdf_bytes=pdf_bytes_list[idx],
                    pdf_file_name=pdf_file_name,
                    local_md_dir=local_md_dir,
                    local_image_dir=local_image_dir,
                    md_writer=md_writer,
                    f_draw_layout_bbox=draw_layout_bbox,
                    f_draw_span_bbox=draw_span_bbox,
                    f_dump_orig_pdf=dump_orig_pdf,
                    f_dump_md=dump_md,
                    f_dump_content_list=dump_content_list,
                    f_dump_middle_json=dump_middle_json,
                    f_dump_model_output=dump_model_output,
                    f_make_md_mode=MakeMode.MM_MD,
                    middle_json=middle_json,
                    model_output=model_list,
                    is_pipeline=True
                )
                logger.success(f"解析完成: {pdf_file_name} -> {local_md_dir}")

        else:  # vlm 系列后端
            # VLM 后端目前不支持表格/公式开关？这里按原始逻辑处理
            if backend.startswith("vlm-"):
                backend = backend[4:]  # 去掉 'vlm-' 前缀
            for idx, pdf_bytes in enumerate(pdf_bytes_list):
                pdf_file_name = file_names[idx]
                # VLM 也支持页面范围裁剪
                pdf_bytes = convert_pdf_bytes_to_bytes_by_pypdfium2(pdf_bytes, start_page_id, end_page_id)
                local_image_dir, local_md_dir = prepare_env(str(output_dir), pdf_file_name, "vlm")
                image_writer = FileBasedDataWriter(local_image_dir)
                md_writer = FileBasedDataWriter(local_md_dir)

                middle_json, infer_result = vlm_doc_analyze(
                    pdf_bytes,
                    image_writer=image_writer,
                    backend=backend,
                    server_url=server_url
                )

                self._process_output(
                    pdf_info=middle_json["pdf_info"],
                    pdf_bytes=pdf_bytes,
                    pdf_file_name=pdf_file_name,
                    local_md_dir=local_md_dir,
                    local_image_dir=local_image_dir,
                    md_writer=md_writer,
                    f_draw_layout_bbox=draw_layout_bbox,
                    f_draw_span_bbox=False,          # VLM 可能不支持 span bbox
                    f_dump_orig_pdf=dump_orig_pdf,
                    f_dump_md=dump_md,
                    f_dump_content_list=dump_content_list,
                    f_dump_middle_json=dump_middle_json,
                    f_dump_model_output=dump_model_output,
                    f_make_md_mode=MakeMode.MM_MD,
                    middle_json=middle_json,
                    model_output=infer_result,
                    is_pipeline=False
                )
                logger.success(f"VLM解析完成: {pdf_file_name} -> {local_md_dir}")

        # 返回所有输出目录路径
        return [output_dir / name for name in file_names]

    def _process_output(self, **kwargs):
        """内部函数：处理输出文件的写入"""
        pdf_info = kwargs["pdf_info"]
        pdf_bytes = kwargs["pdf_bytes"]
        pdf_file_name = kwargs["pdf_file_name"]
        local_md_dir = kwargs["local_md_dir"]
        local_image_dir = kwargs["local_image_dir"]
        md_writer = kwargs["md_writer"]
        f_draw_layout_bbox = kwargs["f_draw_layout_bbox"]
        f_draw_span_bbox = kwargs["f_draw_span_bbox"]
        f_dump_orig_pdf = kwargs["f_dump_orig_pdf"]
        f_dump_md = kwargs["f_dump_md"]
        f_dump_content_list = kwargs["f_dump_content_list"]
        f_dump_middle_json = kwargs["f_dump_middle_json"]
        f_dump_model_output = kwargs["f_dump_model_output"]
        f_make_md_mode = kwargs["f_make_md_mode"]
        middle_json = kwargs["middle_json"]
        model_output = kwargs.get("model_output")
        is_pipeline = kwargs.get("is_pipeline", True)

        if f_draw_layout_bbox:
            draw_layout_bbox(pdf_info, pdf_bytes, local_md_dir, f"{pdf_file_name}_layout.pdf")
        if f_draw_span_bbox:
            draw_span_bbox(pdf_info, pdf_bytes, local_md_dir, f"{pdf_file_name}_span.pdf")
        if f_dump_orig_pdf:
            md_writer.write(f"{pdf_file_name}_origin.pdf", pdf_bytes)

        image_dir = str(os.path.basename(local_image_dir))

        if f_dump_md:
            make_func = pipeline_union_make if is_pipeline else vlm_union_make
            md_content = make_func(pdf_info, f_make_md_mode, image_dir)
            md_writer.write_string(f"{pdf_file_name}.md", md_content)

        if f_dump_content_list:
            make_func = pipeline_union_make if is_pipeline else vlm_union_make
            content_list = make_func(pdf_info, MakeMode.CONTENT_LIST, image_dir)
            md_writer.write_string(
                f"{pdf_file_name}_content_list.json",
                json.dumps(content_list, ensure_ascii=False, indent=4)
            )

        if f_dump_middle_json:
            md_writer.write_string(
                f"{pdf_file_name}_middle.json",
                json.dumps(middle_json, ensure_ascii=False, indent=4)
            )

        if f_dump_model_output and model_output is not None:
            md_writer.write_string(
                f"{pdf_file_name}_model.json",
                json.dumps(model_output, ensure_ascii=False, indent=4)
            )
#
#
#
# # import fitz  # PyMuPDF
# # import numpy as np
# # from PIL import Image
# # import io
# # from typing import List, Tuple, Optional, Dict, Any
# # import logging
# # import os
# # import tempfile
# # import sys
# #
# # logger = logging.getLogger(__name__)
# #
# #
# # class OCRProcessor:
# #     def __init__(self, use_gpu: bool = False, lang: str = 'ch', enable_table_recognition: bool = True,
# #                  table_ocr_backend: str = 'img2table'):
# #         """
# #         初始化OCR处理器（新方案：RapidOCR + img2table）
# #         Args:
# #             use_gpu: 是否尝试使用GPU（某些后端可能支持）
# #             lang: 语言类型，支持 'ch', 'en', 'chinese' 等
# #             enable_table_recognition: 是否启用表格识别
# #             table_ocr_backend: 表格识别后端，可选 'img2table'
# #         """
# #         self.lang = lang
# #         self.use_gpu = use_gpu
# #         self.enable_table_recognition = enable_table_recognition
# #         self.table_ocr_backend = table_ocr_backend
# #
# #         # 创建临时目录（确保路径不包含中文字符）
# #         self._create_safe_temp_dir()
# #
# #         # 核心引擎
# #         self.ocr_engine = None  # 用于文本识别的RapidOCR引擎
# #         self.table_engine = None  # 用于表格识别的引擎 (img2table)
# #         self.layout_engine = None  # (可选) 用于版面分析的引擎
# #
# #         # 初始化核心文本OCR引擎
# #         self._initialize_rapidocr()
# #
# #         # 按需初始化表格识别引擎
# #         if self.enable_table_recognition:
# #             self._initialize_table_ocr()
# #
# #     def _create_safe_temp_dir(self):
# #         """创建安全的临时目录，避免中文路径问题"""
# #         try:
# #             # 方案1: 在当前项目目录创建temp目录（确保无中文）
# #             project_dir = os.getcwd()
# #             temp_dir = os.path.join(project_dir, "ocr_temp")
# #             os.makedirs(temp_dir, exist_ok=True)
# #
# #             # 验证路径是否包含中文
# #             def has_chinese(text):
# #                 """检查字符串是否包含中文字符"""
# #                 import re
# #                 pattern = re.compile(r'[\u4e00-\u9fff]+')
# #                 return bool(pattern.search(text))
# #
# #             if has_chinese(temp_dir):
# #                 # 方案2: 使用系统临时目录，但尝试创建英文子目录
# #                 base_temp = tempfile.gettempdir()
# #                 temp_dir = os.path.join(base_temp, "ocr_python_temp")
# #                 os.makedirs(temp_dir, exist_ok=True)
# #
# #                 # 如果还有中文，使用绝对英文路径
# #                 if has_chinese(temp_dir):
# #                     # 方案3: 尝试在系统根目录创建（需要管理员权限？）
# #                     # 或者在当前盘符的根目录创建
# #                     drive = os.path.splitdrive(project_dir)[0] + "\\"
# #                     temp_dir = os.path.join(drive, "temp_ocr_python")
# #                     os.makedirs(temp_dir, exist_ok=True)
# #
# #             self.temp_dir = temp_dir
# #             logger.info(f"创建临时目录: {self.temp_dir}")
# #
# #             # 设置环境变量，让OpenCV等库使用这个临时目录
# #             os.environ['TMP'] = self.temp_dir
# #             os.environ['TEMP'] = self.temp_dir
# #             os.environ['TMPDIR'] = self.temp_dir
# #
# #         except Exception as e:
# #             logger.error(f"创建临时目录失败: {e}")
# #             # 回退到系统临时目录
# #             self.temp_dir = tempfile.gettempdir()
# #             logger.warning(f"使用系统临时目录: {self.temp_dir}")
# #
# #     def _initialize_rapidocr(self):
# #         """初始化RapidOCR作为文本识别主力引擎"""
# #         try:
# #             from rapidocr_onnxruntime import RapidOCR
# #
# #             lang_map = {
# #                 'ch': 'ch', 'zh': 'ch', 'chinese': 'ch',
# #                 'en': 'en', 'english': 'en'
# #             }
# #             ocr_lang = lang_map.get(self.lang.lower(), 'ch')
# #
# #             self.ocr_engine = RapidOCR(
# #                 rec_model_path=None,  # 使用默认模型
# #                 det_model_path=None,
# #                 cls_model_path=None,
# #                 use_angle_cls=False,
# #                 lang=ocr_lang,
# #                 det_limit_side_len=960,
# #                 det_limit_type='max'
# #             )
# #             logger.info(f"RapidOCR文本引擎初始化成功，语言: {ocr_lang}")
# #
# #         except ImportError as e:
# #             logger.error(f"无法导入RapidOCR: {e}")
# #             logger.warning("将尝试使用Tesseract作为备选...")
# #             self._initialize_tesseract()
# #
# #     def _initialize_table_ocr(self):
# #         """初始化表格识别引擎"""
# #         try:
# #             if self.table_ocr_backend == 'img2table':
# #                 self._initialize_img2table()
# #             # elif self.table_ocr_backend == 'paddleocr':
# #             #     self._initialize_paddleocr_table()
# #             else:
# #                 logger.warning(f"未知的表格识别后端: {self.table_ocr_backend}，将禁用表格识别")
# #                 self.enable_table_recognition = False
# #
# #         except Exception as e:
# #             logger.error(f"初始化表格识别引擎失败: {e}")
# #             logger.warning("表格识别功能将不可用")
# #             self.enable_table_recognition = False
# #
# #     def _initialize_img2table(self):
# #         """初始化img2table表格识别"""
# #         try:
# #             # 方案1: 使用img2table自带的OCR (Tesseract)
# #             from img2table.document import Image as TableImage
# #             from img2table.ocr import TesseractOCR
# #
# #             # 检查Tesseract是否可用
# #             try:
# #                 import pytesseract
# #                 tesseract_version = pytesseract.get_tesseract_version()
# #                 logger.info(f"检测到Tesseract版本: {tesseract_version}")
# #             except Exception as e:
# #                 logger.warning(f"Tesseract检查失败: {e}")
# #                 logger.warning("请确保已安装Tesseract-OCR并添加到系统PATH")
# #
# #             # 设置语言，如果中文则使用'chi_sim'
# #             ocr_lang = 'chi_sim' if self.lang.lower() in ['ch', 'zh', 'chinese'] else self.lang
# #
# #             # 尝试多种初始化方式
# #             try:
# #                 # 尝试方式1: 使用简单初始化
# #                 tesseract_ocr = TesseractOCR(lang=ocr_lang)
# #                 logger.info(f"img2table TesseractOCR初始化成功 (方式1)，语言: {ocr_lang}")
# #             except Exception as e1:
# #                 logger.warning(f"TesseractOCR初始化方式1失败: {e1}")
# #                 try:
# #                     # 尝试方式2: 指定页面分割模式
# #                     tesseract_ocr = TesseractOCR(lang=ocr_lang, psm=11)
# #                     logger.info(f"img2table TesseractOCR初始化成功 (方式2)，语言: {ocr_lang}")
# #                 except Exception as e2:
# #                     logger.warning(f"TesseractOCR初始化方式2失败: {e2}")
# #                     try:
# #                         # 尝试方式3: 不指定语言，使用默认
# #                         tesseract_ocr = TesseractOCR()
# #                         logger.warning(f"img2table TesseractOCR初始化成功 (方式3)，使用默认语言")
# #                     except Exception as e3:
# #                         logger.error(f"所有TesseractOCR初始化方式都失败: {e3}")
# #                         raise
# #
# #             self.table_engine = {
# #                 'type': 'img2table_tesseract',
# #                 'image_class': TableImage,
# #                 'ocr_engine': tesseract_ocr
# #             }
# #             logger.info("img2table表格识别引擎初始化成功")
# #
# #         except ImportError as e:
# #             logger.warning(f"img2table导入失败: {e}，尝试备选方案...")
# #             # 备选方案: 尝试导入img2table基本功能
# #             try:
# #                 from img2table.document import Image as TableImage
# #                 self.table_engine = {
# #                     'type': 'img2table_basic',
# #                     'image_class': TableImage
# #                 }
# #                 logger.info("img2table基础引擎初始化成功 (需额外配置OCR)")
# #             except Exception as e2:
# #                 logger.error(f"img2table备选方案也失败: {e2}")
# #                 raise
# #         except Exception as e:
# #             logger.error(f"初始化img2table失败: {e}")
# #             raise
# #
# #
# #     def _initialize_tesseract(self):
# #         """初始化Tesseract作为文本OCR的备选方案"""
# #         try:
# #             import pytesseract
# #             # 简单检查Tesseract是否可用
# #             pytesseract.get_tesseract_version()
# #             self.ocr_engine = 'tesseract'
# #             logger.info("Tesseract OCR引擎将作为备选使用")
# #         except Exception as e:
# #             logger.error(f"Tesseract初始化失败: {e}")
# #             self.ocr_engine = None
# #
# #     def extract_text_from_page(self, image: Image.Image, page_num: int = None) -> Dict[str, Any]:
# #         """
# #         从单页图像中提取文本和表格
# #         Args:
# #             image: PIL Image对象
# #             page_num: 页码（用于日志）
# #         Returns:
# #             Dict包含:
# #             - 'text': 提取的文本
# #             - 'tables': 表格列表（Markdown格式）
# #             - 'table_count': 表格数量
# #             - 'processing_time': 处理时间
# #         """
# #         import time
# #         start_time = time.time()
# #         page_info = f"第 {page_num} 页" if page_num else "当前页"
# #
# #         result = {
# #             'text': "",
# #             'tables': [],
# #             'table_count': 0,
# #             'processing_time': 0
# #         }
# #
# #         try:
# #             # 1. 使用RapidOCR提取文本
# #             img_array = np.array(image)
# #             ocr_result, _ = self.ocr_engine(img_array)
# #
# #             if ocr_result:
# #                 text_lines = []
# #                 for item in ocr_result:
# #                     text = item[1]
# #                     confidence = item[2]
# #                     if confidence > 0.5:  # 置信度阈值
# #                         text_lines.append(text)
# #                 result['text'] = "\n".join(text_lines)
# #
# #             # 2. 如果启用表格识别，处理表格
# #             if self.enable_table_recognition and self.table_engine:
# #                 tables = self._extract_tables_from_image(image)
# #                 result['tables'] = tables
# #                 result['table_count'] = len(tables)
# #
# #             result['processing_time'] = round(time.time() - start_time, 2)
# #             logger.info(
# #                 f"{page_info}处理完成: 文本行数{len(ocr_result) if ocr_result else 0}, 表格数{result['table_count']}, 耗时{result['processing_time']}秒")
# #
# #         except Exception as e:
# #             logger.error(f"{page_info}处理失败: {e}")
# #             import traceback
# #             logger.error(traceback.format_exc())
# #
# #         return result
# #
# #     def _extract_tables_from_image(self, image: Image.Image) -> List[Dict[str, Any]]:
# #         """
# #         从图像中提取表格
# #         Returns:
# #             表格字典列表，每个包含'markdown'和'raw_data'
# #         """
# #         temp_file_path = None
# #         tables = []
# #
# #         try:
# #             # 创建临时文件，使用安全的临时目录
# #             import uuid
# #             temp_filename = f"table_{uuid.uuid4().hex}.png"
# #             temp_file_path = os.path.join(self.temp_dir, temp_filename)
# #
# #             # 保存图像
# #             image.save(temp_file_path, 'PNG')
# #             logger.debug(f"临时表格图片已保存至: {temp_file_path}")
# #
# #             # 根据表格引擎类型处理
# #             if self.table_engine['type'] == 'img2table_tesseract':
# #                 from img2table.document import Image as TableImage
# #
# #                 # 创建文档对象
# #                 doc = TableImage(temp_file_path)
# #
# #                 # 优化表格提取参数
# #                 try:
# #                     # 使用更严格的表格检测参数
# #                     extracted_tables = doc.extract_tables(
# #                         ocr=self.table_engine['ocr_engine'],
# #                         implicit_rows=True,
# #                         implicit_columns=True,
# #                         borderless_tables=True,
# #                         min_confidence=50,
# #                         vertical_whitespace=30,  # 增加垂直空白阈值
# #                         horizontal_whitespace=30,  # 增加水平空白阈值
# #                         process_background=False  # 不处理背景
# #                     )
# #                 except Exception as e:
# #                     logger.warning(f"表格提取方式1失败: {e}")
# #                     try:
# #                         # 简化参数，更严格的检测
# #                         extracted_tables = doc.extract_tables(
# #                             ocr=self.table_engine['ocr_engine'],
# #                             implicit_rows=False,  # 不推断隐含行
# #                             implicit_columns=False,  # 不推断隐含列
# #                             borderless_tables=False  # 不处理无边框表格
# #                         )
# #                     except Exception as e2:
# #                         logger.warning(f"表格提取方式2失败: {e2}")
# #                         try:
# #                             # 基础提取，仅检测明显的表格
# #                             extracted_tables = doc.extract_tables(
# #                                 ocr=self.table_engine['ocr_engine']
# #                             )
# #                         except Exception as e3:
# #                             logger.error(f"所有表格提取方式都失败: {e3}")
# #                             extracted_tables = []
# #
# #                 # 转换提取的表格为Markdown，并优化表格标题
# #                 for idx, table in enumerate(extracted_tables):
# #                     try:
# #                         # 获取表格数据
# #                         df = table.df
# #
# #                         # 清理表格标题 - 移除表格上方的无关文本
# #                         title = self._clean_table_title(table.title)
# #
# #                         # 如果标题仍然包含太多文本，尝试从表格内容中提取标题
# #                         if self._is_title_too_long(title):
# #                             # 从第一行数据中寻找可能的标题
# #                             title = self._extract_title_from_table_data(df, idx)
# #
# #                         # 将DataFrame转换为Markdown
# #                         md_table = self._dataframe_to_markdown(df)
# #
# #                         # 添加优化后的标题
# #                         if title:
# #                             md_table = f"**表{idx + 1}: {title}**\n\n" + md_table
# #                         else:
# #                             md_table = f"**表{idx + 1}**\n\n" + md_table
# #
# #                         tables.append({
# #                             'markdown': md_table,
# #                             'raw_data': table,
# #                             'title': title
# #                         })
# #
# #                     except Exception as e:
# #                         logger.error(f"表格转换失败: {e}")
# #                         # 即使转换失败，也尝试保存原始数据
# #                         tables.append({
# #                             'markdown': f"[表格 {idx + 1} 转换失败: {e}]",
# #                             'raw_data': table,
# #                             'title': f"表格 {idx + 1}"
# #                         })
# #
# #             elif self.table_engine['type'] == 'img2table_basic':
# #                 # 基础img2table，可能需要额外配置
# #                 logger.debug("使用基础img2table引擎（表格检测）")
# #                 # 这里可以添加基础表格检测逻辑
# #
# #         except Exception as e:
# #             logger.error(f"表格识别失败: {e}")
# #             import traceback
# #             logger.error(traceback.format_exc())
# #
# #         finally:
# #             # 清理临时文件
# #             if temp_file_path and os.path.exists(temp_file_path):
# #                 try:
# #                     os.unlink(temp_file_path)
# #                     logger.debug(f"已清理临时文件: {temp_file_path}")
# #                 except Exception as e:
# #                     logger.debug(f"清理临时文件失败: {e}")
# #
# #         return tables
# #
# #     def _clean_table_title(self, title: str) -> str:
# #         """清理表格标题，移除无关文本"""
# #         if not title:
# #             return ""
# #
# #         import re
# #
# #         # 如果标题包含换行符，只取最后一行（通常表格标题在最后）
# #         lines = title.split('\n')
# #         if len(lines) > 1:
# #             # 从后向前查找，寻找包含"表"、"Table"、"TABLE"的行
# #             for line in reversed(lines):
# #                 if re.search(r'表\s*\d+', line) or '表' in line:
# #                     return line.strip()
# #             # 如果没有找到，取最后一行
# #             return lines[-1].strip()
# #
# #         # 清理标题中的多余空格和标点
# #         cleaned_title = re.sub(r'\s+', ' ', title).strip()
# #
# #         # 如果标题过长（超过100字符），尝试截取
# #         if len(cleaned_title) > 100:
# #             # 寻找"表"字位置
# #             table_match = re.search(r'表\s*\d+', cleaned_title)
# #             if table_match:
# #                 start_pos = table_match.start()
# #                 cleaned_title = cleaned_title[start_pos:].strip()
# #             else:
# #                 # 截取最后80个字符
# #                 cleaned_title = "..." + cleaned_title[-80:]
# #
# #         return cleaned_title
# #
# #     def _is_title_too_long(self, title: str) -> bool:
# #         """判断标题是否过长（可能包含了正文）"""
# #         if not title:
# #             return False
# #
# #         # 如果标题超过150字符，认为过长
# #         if len(title) > 150:
# #             return True
# #
# #         # 如果包含太多标点符号（可能是正文）
# #         import string
# #         punctuation_count = sum(1 for char in title if char in string.punctuation or char in '，。；、')
# #         if punctuation_count > 5:
# #             return True
# #
# #         return False
# #
# #     def _extract_title_from_table_data(self, df, table_idx: int) -> str:
# #         """从表格数据中提取标题"""
# #         try:
# #             # 尝试从DataFrame的第一行第一列提取标题信息
# #             if not df.empty:
# #                 # 获取第一行第一列的值
# #                 first_cell = str(df.iloc[0, 0]) if len(df.columns) > 0 and len(df) > 0 else ""
# #
# #                 # 检查是否包含"表"字
# #                 import re
# #                 if re.search(r'表\s*\d+', first_cell):
# #                     # 提取表号
# #                     table_match = re.search(r'表\s*\d+[^\d]*', first_cell)
# #                     if table_match:
# #                         return table_match.group(0).strip()
# #
# #                 # 如果第一行可能是表头，检查是否有明显的标题特征
# #                 header_text = " ".join([str(col) for col in df.columns[:3]])
# #                 if "周期" in header_text or "表" in header_text:
# #                     # 提取前几个关键词
# #                     keywords = []
# #                     for word in ["周期", "表", "序号", "顺号", "车种", "检修"]:
# #                         if word in header_text:
# #                             keywords.append(word)
# #                     if keywords:
# #                         return f"表格{keywords[0]}信息"
# #
# #             # 如果无法提取，返回通用标题
# #             return f"表格{table_idx + 1}"
# #
# #         except Exception as e:
# #             logger.debug(f"从表格数据提取标题失败: {e}")
# #             return f"表格{table_idx + 1}"
# #
# #     def _dataframe_to_markdown(self, df) -> str:
# #         """将DataFrame（或类似结构）转换为Markdown表格"""
# #         try:
# #             # 如果是pandas DataFrame
# #             if hasattr(df, 'to_markdown'):
# #                 return df.to_markdown(index=False)
# #
# #             # 如果是列表的列表（2D数组）
# #             if isinstance(df, list) and all(isinstance(row, list) for row in df):
# #                 md_lines = []
# #                 for i, row in enumerate(df):
# #                     # 确保所有元素都是字符串
# #                     str_row = [str(cell) for cell in row]
# #                     md_lines.append("| " + " | ".join(str_row) + " |")
# #
# #                     # 第一行后添加分隔线
# #                     if i == 0:
# #                         separator = "| " + " | ".join(["---"] * len(row)) + " |"
# #                         md_lines.append(separator)
# #
# #                 return "\n".join(md_lines)
# #
# #             # 其他格式，转换为字符串
# #             return str(df)
# #
# #         except Exception as e:
# #             logger.error(f"DataFrame转换Markdown失败: {e}")
# #             return f"[表格数据，但转换失败: {e}]"
# #
# #
# #     def _dataframe_to_markdown(self, df) -> str:
# #         """将DataFrame（或类似结构）转换为Markdown表格"""
# #         try:
# #             # 如果是pandas DataFrame
# #             if hasattr(df, 'to_markdown'):
# #                 return df.to_markdown(index=False)
# #
# #             # 如果是列表的列表（2D数组）
# #             if isinstance(df, list) and all(isinstance(row, list) for row in df):
# #                 md_lines = []
# #                 for i, row in enumerate(df):
# #                     # 确保所有元素都是字符串
# #                     str_row = [str(cell) for cell in row]
# #                     md_lines.append("| " + " | ".join(str_row) + " |")
# #
# #                     # 第一行后添加分隔线
# #                     if i == 0:
# #                         separator = "| " + " | ".join(["---"] * len(row)) + " |"
# #                         md_lines.append(separator)
# #
# #                 return "\n".join(md_lines)
# #
# #             # 其他格式，转换为字符串
# #             return str(df)
# #
# #         except Exception as e:
# #             logger.error(f"DataFrame转换Markdown失败: {e}")
# #             return f"[表格数据，但转换失败: {e}]"
# #
# #     def _html_table_to_markdown(self, html_table: str) -> str:
# #         """将HTML表格转换为Markdown"""
# #         import re
# #         try:
# #             rows = re.findall(r'<tr>(.*?)</tr>', html_table, re.DOTALL)
# #             md_rows = []
# #
# #             for i, row in enumerate(rows):
# #                 cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)
# #                 if cells:
# #                     clean_cells = []
# #                     for cell in cells:
# #                         clean_cell = re.sub(r'<[^>]+>', '', cell)
# #                         clean_cell = clean_cell.replace('&nbsp;', ' ').strip()
# #                         clean_cells.append(clean_cell)
# #
# #                     md_row = "| " + " | ".join(clean_cells) + " |"
# #                     md_rows.append(md_row)
# #
# #                     if i == 0:
# #                         separator = "| " + " | ".join(["---"] * len(clean_cells)) + " |"
# #                         md_rows.append(separator)
# #
# #             return "\n".join(md_rows) if md_rows else ""
# #
# #         except Exception as e:
# #             logger.error(f"HTML表格转换失败: {e}")
# #             return ""
# #
# #     def extract_text_from_scanned_pdf(self, pdf_path: str, dpi: int = 150, max_pages: int = None) -> str:
# #         """
# #         从扫描版PDF提取文本和表格
# #         Args:
# #             pdf_path: PDF文件路径
# #             dpi: 图像DPI（影响清晰度和处理速度）
# #             max_pages: 最大处理页数（None表示处理所有页）
# #         """
# #         if not self.ocr_engine:
# #             logger.error("OCR引擎未初始化")
# #             return ""
# #
# #         doc = fitz.open(pdf_path)
# #         full_content = []
# #         total_tables = 0
# #         total_pages = len(doc)
# #         pages_to_process = min(max_pages, total_pages) if max_pages else total_pages
# #
# #         logger.info(f"开始处理PDF: {pdf_path}，共{total_pages}页，将处理{pages_to_process}页")
# #
# #         try:
# #             for page_num in range(pages_to_process):
# #                 page = doc[page_num]
# #
# #                 # 转换为图像
# #                 pix = page.get_pixmap(dpi=dpi)
# #                 img_data = pix.tobytes("png")
# #                 image = Image.open(io.BytesIO(img_data))
# #
# #                 # 提取页面内容
# #                 page_result = self.extract_text_from_page(image, page_num + 1)
# #
# #                 # 构建页面内容
# #                 page_content = []
# #
# #                 # 添加文本
# #                 if page_result['text'].strip():
# #                     page_content.append(f"=== 第 {page_num + 1} 页 ===")
# #                     page_content.append(page_result['text'])
# #
# #                 # 添加表格
# #                 if page_result['tables']:
# #                     page_content.append(f"\n【第 {page_num + 1} 页表格】")
# #                     for i, table in enumerate(page_result['tables']):
# #                         page_content.append(f"\n表{i + 1}:")
# #                         page_content.append(table['markdown'])
# #                         total_tables += 1
# #
# #                 if page_content:
# #                     full_content.append("\n".join(page_content))
# #
# #         except Exception as e:
# #             logger.error(f"处理PDF时出错: {e}")
# #             import traceback
# #             logger.error(traceback.format_exc())
# #         finally:
# #             doc.close()
# #
# #         # 添加摘要信息
# #         if full_content:
# #             summary = [
# #                 f"\n{'=' * 60}",
# #                 f"PDF处理摘要",
# #                 f"{'=' * 60}",
# #                 f"总页数: {total_pages}",
# #                 f"已处理页数: {pages_to_process}",
# #                 f"总表格数: {total_tables}",
# #                 f"文本引擎: RapidOCR",
# #                 f"表格引擎: {self.table_ocr_backend if self.enable_table_recognition else '未启用'}",
# #                 f"临时目录: {self.temp_dir}",
# #                 f"{'=' * 60}"
# #             ]
# #             full_content.append("\n".join(summary))
# #
# #         return "\n\n".join(full_content) if full_content else ""
# #
# #     def is_scanned_pdf(self, pdf_path: str, sample_pages: int = 3) -> bool:
# #         """
# #         判断PDF是否为扫描版
# #         Args:
# #             pdf_path: PDF文件路径
# #             sample_pages: 抽检页数
# #         """
# #         try:
# #             doc = fitz.open(pdf_path)
# #             text_pages = 0
# #
# #             check_pages = min(sample_pages, len(doc))
# #             for page_num in range(check_pages):
# #                 page = doc[page_num]
# #                 text = page.get_text()
# #                 if text and len(text.strip()) > 50:  # 有实质文本内容
# #                     text_pages += 1
# #
# #             doc.close()
# #             is_scanned = text_pages < (check_pages * 0.3)  # 少于30%的页有文本
# #             logger.info(
# #                 f"PDF检测: 检查{check_pages}页，{text_pages}页有文本，判断为{'扫描版' if is_scanned else '文字版'}")
# #             return is_scanned
# #
# #         except Exception as e:
# #             logger.error(f"判断PDF类型时出错: {e}")
# #             return True  # 出错时默认按扫描版处理
# #
# #     def process_pdf(self, pdf_path: str, max_pages: int = None) -> Tuple[str, bool]:
# #         """
# #         处理PDF文件，返回文本和是否为扫描版的标志
# #         Args:
# #             pdf_path: PDF文件路径
# #             max_pages: 最大处理页数（用于测试或分步处理）
# #         """
# #         try:
# #             is_scanned = self.is_scanned_pdf(pdf_path)
# #
# #             if is_scanned:
# #                 logger.info(f"检测到扫描版PDF: {pdf_path}")
# #                 text = self.extract_text_from_scanned_pdf(pdf_path, dpi=150, max_pages=max_pages)
# #             else:
# #                 logger.info(f"检测到文字版PDF: {pdf_path}")
# #                 # 注意：这里需要根据实际情况导入PDFProcessor
# #                 # 如果PDFProcessor在同一目录下，可以使用相对导入
# #                 try:
# #                     from .pdf_processor import PDFProcessor
# #                     processor = PDFProcessor()
# #                     text = processor.extract_text(pdf_path)
# #                 except ImportError:
# #                     # 如果无法导入，使用fitz提取文字版PDF的文本
# #                     logger.warning("无法导入PDFProcessor，使用fitz提取文字版PDF")
# #                     doc = fitz.open(pdf_path)
# #                     text_parts = []
# #                     pages_to_process = min(max_pages, len(doc)) if max_pages else len(doc)
# #                     for page_num in range(pages_to_process):
# #                         page = doc[page_num]
# #                         text_parts.append(f"=== 第 {page_num + 1} 页 ===")
# #                         text_parts.append(page.get_text())
# #                     doc.close()
# #                     text = "\n\n".join(text_parts)
# #
# #             return text, is_scanned
# #
# #         except Exception as e:
# #             logger.error(f"处理PDF失败: {e}")
# #             import traceback
# #             logger.error(traceback.format_exc())
# #             return "", True
# #
# #     def cleanup(self):
# #         """清理临时文件"""
# #         try:
# #             if hasattr(self, 'temp_dir') and os.path.exists(self.temp_dir):
# #                 # 只删除自己创建的文件，不删除目录本身
# #                 import glob
# #                 temp_files = glob.glob(os.path.join(self.temp_dir, "*.png"))
# #                 for file_path in temp_files:
# #                     try:
# #                         os.unlink(file_path)
# #                         logger.debug(f"已删除临时文件: {file_path}")
# #                     except Exception as e:
# #                         logger.debug(f"删除临时文件失败: {e}")
# #         except Exception as e:
# #             logger.warning(f"清理临时文件失败: {e}")
# #
# #
# #
# import fitz  # PyMuPDF
# import numpy as np
# from PIL import Image
# import io
# from typing import List, Tuple, Optional, Dict, Any
# import logging
# import time
#
# logger = logging.getLogger(__name__)
#
#
# class OCRProcessor:
#     def __init__(self, use_gpu: bool = False, lang: str = 'ch', use_table_recognition: bool = True):
#         """
#         初始化OCR处理器
#         Args:
#             use_gpu: 是否使用GPU
#             lang: 语言类型
#             use_table_recognition: 是否启用表格识别
#         """
#         self.lang = lang
#         self.use_gpu = use_gpu
#         self.use_table_recognition = use_table_recognition
#
#         # Surya预测器实例
#         self.foundation_predictor = None
#         self.recognition_predictor = None
#         self.detection_predictor = None
#         self.table_rec_predictor = None
#         self.layout_predictor = None
#
#         # 快速回退的RapidOCR
#         self.rapid_ocr = None
#
#         self._initialize_surya()
#
#     def _initialize_surya(self):
#         """按照官方文档初始化Surya预测器"""
#         try:
#             logger.info("初始化Surya OCR预测器...")
#
#             # 1. 导入必要的模块
#             from surya.foundation import FoundationPredictor
#             from surya.recognition import RecognitionPredictor
#             from surya.detection import DetectionPredictor
#
#             # 2. 初始化基础预测器
#             self.foundation_predictor = FoundationPredictor()
#
#             # 3. 初始化识别预测器（需要基础预测器）
#             self.recognition_predictor = RecognitionPredictor(self.foundation_predictor)
#
#             # 4. 初始化检测预测器
#             self.detection_predictor = DetectionPredictor()
#
#             logger.info("Surya文本OCR预测器初始化成功")
#
#             # 5. 如果启用表格识别，初始化表格预测器
#             if self.use_table_recognition:
#                 try:
#                     from surya.table_rec import TableRecPredictor
#                     self.table_rec_predictor = TableRecPredictor()
#                     logger.info("Surya表格识别预测器初始化成功")
#                 except ImportError as e:
#                     logger.warning(f"表格识别不可用: {e}")
#
#             # 6. 可选：初始化版面分析预测器
#             try:
#                 from surya.layout import LayoutPredictor
#                 from surya.settings import settings
#                 self.layout_predictor = LayoutPredictor(
#                     self.foundation_predictor
#                 )
#                 logger.info("Surya版面分析预测器初始化成功")
#             except ImportError as e:
#                 logger.debug(f"版面分析不可用: {e}")
#
#             self.ocr_engine = 'surya'
#
#         except ImportError as e:
#             logger.error(f"导入Surya模块失败: {e}")
#             logger.info("回退到RapidOCR...")
#             self._initialize_rapidocr()
#         except Exception as e:
#             logger.error(f"初始化Surya失败: {e}")
#             self._initialize_rapidocr()
#
#     def _initialize_rapidocr(self):
#         """初始化RapidOCR作为备选"""
#         try:
#             from rapidocr_onnxruntime import RapidOCR
#
#             lang_map = {'ch': 'ch', 'zh': 'ch', 'en': 'en'}
#             ocr_lang = lang_map.get(self.lang.lower(), 'ch')
#
#             self.rapid_ocr = RapidOCR(
#                 rec_model_path=None,
#                 det_model_path=None,
#                 cls_model_path=None,
#                 use_angle_cls=False,
#                 lang=ocr_lang,
#                 det_limit_side_len=960,
#                 det_limit_type='max'
#             )
#             self.ocr_engine = 'rapidocr'
#             logger.info(f"RapidOCR初始化成功，语言: {ocr_lang}")
#
#         except ImportError as e:
#             logger.error(f"无法导入RapidOCR: {e}")
#             self.ocr_engine = None
#
#     def extract_text_from_page(self, image: Image.Image) -> Dict[str, Any]:
#         """
#         从单页图像中提取文本
#         返回结构化的OCR结果
#         """
#         result = {
#             'text': "",
#             'lines': [],
#             'tables': [],
#             'layout': [],
#             'processing_time': 0
#         }
#
#         start_time = time.time()
#
#         try:
#             # 1. 使用Surya进行文本识别
#             if self.ocr_engine == 'surya':
#                 predictions = self.recognition_predictor(
#                     [image],
#                     det_predictor=self.detection_predictor
#                 )
#
#                 if predictions and len(predictions) > 0:
#                     page_predictions = predictions[0]
#
#                     # 提取文本行
#                     if "text_lines" in page_predictions:
#                         text_lines = []
#                         for line in page_predictions["text_lines"]:
#                             if "text" in line:
#                                 text_lines.append(line["text"])
#
#                                 # 保存详细信息
#                                 result['lines'].append({
#                                     'text': line["text"],
#                                     'confidence': line.get("confidence", 0),
#                                     'bbox': line.get("bbox", []),
#                                     'polygon': line.get("polygon", [])
#                                 })
#
#                         result['text'] = "\n".join(text_lines)
#
#                     # 2. 运行表格识别（如果有表格预测器）
#                     if self.table_rec_predictor:
#                         table_predictions = self.table_rec_predictor([image])
#
#                         if table_predictions and len(table_predictions) > 0:
#                             for table in table_predictions[0]:
#                                 md_table = self._table_to_markdown(table)
#                                 result['tables'].append({
#                                     'markdown': md_table,
#                                     'data': table
#                                 })
#
#                     # # 3. 运行版面分析（可选）
#                     # if self.layout_predictor:
#                     #     try:
#                     #         layout_predictions = self.layout_predictor([image])
#                     #
#                     #         if layout_predictions and len(layout_predictions) > 0:
#                     #             # LayoutResult对象使用属性访问，而不是.get()
#                     #             layout_result = layout_predictions[0]
#                     #             # 尝试不同的可能属性名
#                     #             if hasattr(layout_result, 'bboxes'):
#                     #                 result['layout'] = layout_result.bboxes
#                     #             elif hasattr(layout_result, 'boxes'):
#                     #                 result['layout'] = layout_result.boxes
#                     #             else:
#                     #                 # 如果都不是，则记录对象类型供调试
#                     #                 result['layout'] = str(type(layout_result))
#                     #                 logger.debug(
#                     #                     f"版面结果类型: {type(layout_result)}, 属性: {dir(layout_result)[:10]}")
#                     #     except Exception as e:
#                     #         logger.warning(f"版面分析处理失败（可忽略）: {e}")
#
#             # 回退到RapidOCR
#             elif self.ocr_engine == 'rapidocr' and self.rapid_ocr:
#                 img_array = np.array(image)
#                 ocr_result, _ = self.rapid_ocr(img_array)
#
#                 if ocr_result:
#                     text_lines = []
#                     for item in ocr_result:
#                         text = item[1]
#                         confidence = item[2]
#                         if confidence > 0.5:
#                             text_lines.append(text)
#                             result['lines'].append({
#                                 'text': text,
#                                 'confidence': confidence,
#                                 'bbox': item[0] if len(item) > 0 else []
#                             })
#
#                     result['text'] = "\n".join(text_lines)
#
#             result['processing_time'] = round(time.time() - start_time, 2)
#
#         except Exception as e:
#             logger.error(f"页面OCR处理失败: {e}")
#             import traceback
#             logger.error(traceback.format_exc())
#
#         return result
#
#     def _table_to_markdown(self, table_data) -> str:
#         """将Surya表格数据转换为Markdown格式（兼容多种返回格式）"""
#         try:
#             # 1. 调试：打印原始数据结构和类型
#             logger.debug(f"表格原始数据类型: {type(table_data)}")
#
#             # 2. 处理元组类型的数据（Surya新API可能返回元组）
#             actual_table = None
#
#             if isinstance(table_data, tuple):
#                 logger.debug(f"表格数据是元组，长度: {len(table_data)}")
#                 # 元组可能包含多个元素，尝试找到包含表格数据的那个
#                 for i, item in enumerate(table_data):
#                     logger.debug(f"  元组[{i}] 类型: {type(item)}")
#
#                     # 检查是否为包含cells的表格对象
#                     if hasattr(item, 'cells'):
#                         actual_table = item
#                         logger.debug(f"  在元组索引 {i} 找到包含'cells'属性的表格对象")
#                         break
#                     elif isinstance(item, dict) and "cells" in item:
#                         actual_table = item
#                         logger.debug(f"  在元组索引 {i} 找到包含'cells'键的字典")
#                         break
#                     # 也可能是元组嵌套，尝试进一步展开
#                     elif isinstance(item, tuple):
#                         for j, sub_item in enumerate(item):
#                             if hasattr(sub_item, 'cells') or (isinstance(sub_item, dict) and "cells" in sub_item):
#                                 actual_table = sub_item
#                                 logger.debug(f"  在嵌套元组[{i}][{j}] 找到表格数据")
#                                 break
#                         if actual_table:
#                             break
#
#                 # 如果没找到，尝试第一个非空元素
#                 if actual_table is None and len(table_data) > 0:
#                     actual_table = table_data[0]
#                     logger.debug(f"  使用元组的第一个元素作为表格数据")
#             else:
#                 actual_table = table_data
#
#             # 3. 提取单元格数据
#             cells = []
#
#             if actual_table is None:
#                 logger.error("无法从数据中提取表格信息")
#                 return ""
#
#             # 检查实际表格数据的类型并提取cells
#             if hasattr(actual_table, 'cells'):
#                 # TableResult对象
#                 cells = actual_table.cells
#                 logger.debug(f"从TableResult对象获取到 {len(cells)} 个单元格")
#             elif isinstance(actual_table, dict) and "cells" in actual_table:
#                 # 字典格式
#                 cells = actual_table["cells"]
#                 logger.debug(f"从字典获取到 {len(cells)} 个单元格")
#             elif hasattr(actual_table, '__iter__'):
#                 # 尝试直接迭代（可能是单元格列表）
#                 try:
#                     cells = list(actual_table)
#                     logger.debug(f"通过直接迭代获取到 {len(cells)} 个单元格")
#                 except:
#                     logger.error(f"无法识别的表格数据结构: {type(actual_table)}")
#                     return ""
#             else:
#                 logger.error(f"无法识别的表格数据类型: {type(actual_table)}")
#                 return ""
#
#             if not cells:
#                 logger.debug("表格没有单元格数据")
#                 return ""
#
#             # 4. 按行、列组织单元格
#             cells_by_row_col = {}
#             max_row = 0
#             max_col = 0
#
#             for cell_idx, cell in enumerate(cells):
#                 try:
#                     # 尝试从对象获取
#                     if hasattr(cell, 'row_id'):
#                         row_id = cell.row_id
#                         col_id = cell.col_id
#                         cell_text = cell.text if hasattr(cell, 'text') else ''
#                     # 尝试从字典获取
#                     elif isinstance(cell, dict):
#                         row_id = cell.get("row_id", 0)
#                         col_id = cell.get("col_id", 0)
#                         cell_text = cell.get("text", "")
#                     # 尝试从元组获取（可能是(row, col, text)格式）
#                     elif isinstance(cell, tuple) and len(cell) >= 3:
#                         row_id = cell[0] if isinstance(cell[0], (int, float)) else 0
#                         col_id = cell[1] if isinstance(cell[1], (int, float)) else 0
#                         cell_text = str(cell[2])
#                     else:
#                         logger.warning(f"无法解析单元格 {cell_idx} 的数据: {type(cell)}")
#                         continue
#
#                     # 确保行列是整数
#                     try:
#                         row_id = int(row_id)
#                         col_id = int(col_id)
#                     except (ValueError, TypeError):
#                         row_id = 0
#                         col_id = 0
#
#                     # 更新最大行列
#                     max_row = max(max_row, row_id)
#                     max_col = max(max_col, col_id)
#
#                     # 存储单元格
#                     if row_id not in cells_by_row_col:
#                         cells_by_row_col[row_id] = {}
#                     cells_by_row_col[row_id][col_id] = str(cell_text).strip()
#
#                 except Exception as e:
#                     logger.warning(f"处理单元格 {cell_idx} 时出错: {e}")
#                     continue
#
#             if not cells_by_row_col:
#                 logger.error("未能成功提取任何单元格数据")
#                 return ""
#
#             logger.debug(f"表格尺寸: {max_row + 1} 行 x {max_col + 1} 列")
#
#             # 5. 构建Markdown表格
#             md_lines = []
#
#             for row in range(max_row + 1):
#                 row_cells = cells_by_row_col.get(row, {})
#                 row_text_parts = []
#
#                 for col in range(max_col + 1):
#                     cell_text = row_cells.get(col, "")
#                     # 清理文本中的Markdown特殊字符
#                     if cell_text:
#                         # 替换管道符等可能破坏表格的字符
#                         cell_text = cell_text.replace('|', '‖').replace('\n', ' ').strip()
#                     row_text_parts.append(cell_text)
#
#                 md_lines.append("| " + " | ".join(row_text_parts) + " |")
#
#             # 添加表头分隔线（假设第一行是表头）
#             if md_lines:
#                 separator = "| " + " | ".join(["---"] * (max_col + 1)) + " |"
#                 md_lines.insert(1, separator)
#
#             result = "\n".join(md_lines)
#             logger.debug(f"成功生成Markdown表格，共 {len(md_lines)} 行")
#
#             return result
#
#         except Exception as e:
#             logger.error(f"转换表格到Markdown失败: {e}")
#             import traceback
#             logger.error(traceback.format_exc())
#
#             # 尝试返回原始数据的字符串表示作为后备
#             try:
#                 return f"[表格数据（原始格式）]: {str(table_data)[:200]}..."
#             except:
#                 return "[表格识别成功，但转换失败]"
#
#     def extract_text_from_scanned_pdf(self, pdf_path: str, dpi: int = 150) -> str:
#         """
#         从扫描版PDF提取文本和表格
#         """
#         if not self.ocr_engine:
#             logger.error("OCR引擎未初始化")
#             return ""
#
#         doc = fitz.open(pdf_path)
#         full_content = []
#         total_tables = 0
#
#         try:
#             for page_num in range(len(doc)):
#                 page = doc[page_num]
#
#                 # 转换为PIL Image
#                 pix = page.get_pixmap(dpi=dpi)
#                 img_data = pix.tobytes("png")
#                 image = Image.open(io.BytesIO(img_data))
#
#                 # 提取页面内容
#                 page_result = self.extract_text_from_page(image)
#
#                 # 构建页面内容
#                 page_content = []
#
#                 # 添加文本
#                 if page_result['text'].strip():
#                     page_content.append(f"=== 第 {page_num + 1} 页 ===")
#                     page_content.append(f"处理时间: {page_result['processing_time']}秒")
#                     page_content.append("-" * 40)
#                     page_content.append(page_result['text'])
#
#                 # 添加表格
#                 if page_result['tables']:
#                     page_content.append(f"\n【第 {page_num + 1} 页表格】")
#                     for i, table in enumerate(page_result['tables']):
#                         page_content.append(f"\n表{i + 1}:")
#                         page_content.append(table['markdown'])
#                         total_tables += 1
#
#                 if page_content:
#                     full_content.append("\n".join(page_content))
#
#                 logger.info(f"处理第 {page_num + 1}/{len(doc)} 页，找到 {len(page_result['tables'])} 个表格")
#
#             # 添加摘要信息
#             summary = [
#                 f"\n{'=' * 60}",
#                 f"PDF处理摘要",
#                 f"{'=' * 60}",
#                 f"总页数: {len(doc)}",
#                 f"总表格数: {total_tables}",
#                 f"OCR引擎: {self.ocr_engine}",
#                 f"{'=' * 60}"
#             ]
#
#             full_content.append("\n".join(summary))
#
#             return "\n\n".join(full_content)
#
#         except Exception as e:
#             logger.error(f"处理PDF失败: {e}")
#             return ""
#         finally:
#             doc.close()
#
#     def is_scanned_pdf(self, pdf_path: str, threshold: int = 10) -> bool:
#         """判断PDF是否为扫描版"""
#         try:
#             doc = fitz.open(pdf_path)
#             text_pages = 0
#
#             for page_num in range(min(5, len(doc))):
#                 page = doc[page_num]
#                 text = page.get_text()
#                 if text.strip():
#                     text_pages += 1
#
#             doc.close()
#             return text_pages < threshold
#         except Exception as e:
#             logger.error(f"判断PDF类型失败: {e}")
#             return True
#
#     def process_pdf(self, pdf_path: str) -> Tuple[str, bool]:
#         """
#         处理PDF文件，返回文本和是否为扫描版的标志
#         """
#         try:
#             is_scanned = self.is_scanned_pdf(pdf_path)
#
#             if is_scanned:
#                 logger.info(f"检测到扫描版PDF: {pdf_path}")
#                 text = self.extract_text_from_scanned_pdf(pdf_path)
#             else:
#                 logger.info(f"检测到文字版PDF: {pdf_path}")
#                 from .pdf_processor import PDFProcessor
#                 processor = PDFProcessor()
#                 text = processor.extract_text(pdf_path)
#
#             return text, is_scanned
#
#         except Exception as e:
#             logger.error(f"处理PDF失败: {e}")
#             return "", True
#
#     def process_image_file(self, image_path: str) -> Dict[str, Any]:
#         """
#         处理单个图像文件
#         """
#         try:
#             image = Image.open(image_path)
#             return self.extract_text_from_page(image)
#         except Exception as e:
#             logger.error(f"处理图像失败: {e}")
#             return {'error': str(e)}