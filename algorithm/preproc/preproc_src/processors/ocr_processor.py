from docker.configuration import getenv as config_getenv
import os
import json
import importlib
from pathlib import Path
from typing import List, Optional, Union, Dict, Any
from loguru import logger
from . import BaseProcessor
from ..page_edges import (
    DEFAULT_MIN_CHARS,
    extract_edge_lines,
    lines_from_blocks,
    merge_page_edges,
    missing_lines,
    normalize_keep_page_edges,
    page_is_kept,
)
from ..table_json import convert_tables


class OCRProcessor(BaseProcessor):
    """
    MinerU PDF/图像解析处理器，封装了底层API调用。
    """

    @staticmethod
    def _validate_local_models_config(
        local_models_config: object,
        required_mode: Optional[str] = None,
    ) -> None:
        if not isinstance(local_models_config, dict):
            raise ValueError(
                "MinerU 本地模型配置无效：请在 mineru.json 中提供 'models-dir'，"
                "且其值为包含 pipeline/vlm 路径的对象"
            )
        required_modes = (required_mode,) if required_mode in {"pipeline", "vlm"} else ("pipeline", "vlm")
        missing_modes = [mode for mode in required_modes if not local_models_config.get(mode)]
        if missing_modes:
            raise ValueError(
                f"MinerU 本地模型配置缺失: {', '.join(missing_modes)}。"
                "请在 mineru.json 的 models-dir 中补全对应路径"
            )
        missing_paths = [
            f"{mode}={local_models_config[mode]}"
            for mode in required_modes
            if local_models_config.get(mode) and not Path(str(local_models_config[mode])).exists()
        ]
        if missing_paths:
            raise ValueError(
                "MinerU 本地模型目录不存在: "
                + "; ".join(missing_paths)
                + "。请确认部署机 .env 中 HOST_MODELS_DIR 已挂载到容器 /models，"
                "并且 MINERU_MODEL_PIPELINE_PATH / MINERU_MODEL_VLM_PATH 指向容器内真实目录。"
            )

    def __init__(
        self,
        config_path: Optional[str] = None,
        model_source: str = "local",
        required_mode: Optional[str] = None,
    ):
        """
        初始化处理器。

        :param config_path: MinerU 配置文件路径（如 mineru.json），若提供则设置环境变量 MINERU_CONFIG_PATH
        :param model_source: 模型源，可选 'local', 'modelscope', 'huggingface'。默认 'local' 表示使用本地已下载的模型。
        """
        _preproc_root = Path(__file__).resolve().parents[2]
        _default_json = _preproc_root / "mineru.json"
        effective_config_path = (
            config_path
            or config_getenv("MINERU_CONFIG_PATH")
            or config_getenv("MINERU_TOOLS_CONFIG_JSON")
            or (str(_default_json) if _default_json.is_file() else str(_default_json))
        )

        # If a relative path (not absolute) is provided from the root .env,
        # resolve it relative to the algorithm/preproc root so downstream libraries can find it.
        try:
            if effective_config_path and not Path(effective_config_path).is_absolute():
                candidate = _preproc_root / effective_config_path
                if candidate.exists():
                    effective_config_path = str(candidate)
                else:
                    # fallback: try repo root
                    repo_root = _preproc_root.parent
                    cand2 = repo_root / effective_config_path
                    if cand2.exists():
                        effective_config_path = str(cand2)
        except Exception:
            pass
        effective_model_source = model_source or config_getenv("MINERU_MODEL_SOURCE") or 'local'

        # 允许通过环境变量覆盖 mineru.json 中 models-dir 的路径，便于不同环境部署。
        # 覆盖变量：
        # - MINERU_MODEL_PIPELINE_PATH
        # - MINERU_MODEL_VLM_PATH
        pipeline_override = config_getenv("MINERU_MODEL_PIPELINE_PATH")
        vlm_override = config_getenv("MINERU_MODEL_VLM_PATH")
        if pipeline_override or vlm_override:
            try:
                with open(effective_config_path, "r", encoding="utf-8") as f:
                    runtime_cfg = json.load(f)
                models_dir = runtime_cfg.get("models-dir")
                if not isinstance(models_dir, dict):
                    models_dir = {}
                if pipeline_override:
                    models_dir["pipeline"] = pipeline_override
                if vlm_override:
                    models_dir["vlm"] = vlm_override
                runtime_cfg["models-dir"] = models_dir

                runtime_path = Path("/tmp") / f"mineru.runtime.{os.getpid()}.json"
                with open(runtime_path, "w", encoding="utf-8") as f:
                    json.dump(runtime_cfg, f, ensure_ascii=False, indent=4)
                effective_config_path = str(runtime_path)
            except Exception as e:
                logger.warning(
                    "Failed to apply MinerU model path overrides from env, fallback to original config. error={}",
                    e,
                )

        # MinerU 实际读取 MINERU_TOOLS_CONFIG_JSON，而不是 MINERU_CONFIG_PATH。
        from docker.configuration import configure_mineru_loader_environment
        configure_mineru_loader_environment(effective_config_path, effective_model_source)

        from mineru.utils import config_reader as mineru_config_reader
        importlib.reload(mineru_config_reader)

        if effective_model_source == 'local':
            local_models_config = mineru_config_reader.get_local_models_dir()
            self._validate_local_models_config(local_models_config, required_mode)

        # from mineru.cli.common import convert_pdf_bytes_to_bytes_by_pypdfium2, prepare_env, read_fn
        # from mineru.data.data_reader_writer import FileBasedDataWriter
        # from mineru.backend.pipeline.pipeline_analyze import doc_analyze as pipeline_doc_analyze
        # from mineru.backend.pipeline.pipeline_middle_json_mkcontent import union_make as pipeline_union_make
        # from mineru.backend.pipeline.model_json_to_middle_json import result_to_middle_json as pipeline_result_to_middle_json
        # from mineru.backend.vlm.vlm_analyze import doc_analyze as vlm_doc_analyze
        # from mineru.backend.vlm.vlm_middle_json_mkcontent import union_make as vlm_union_make
        # from mineru.utils.draw_bbox import draw_layout_bbox, draw_span_bbox
        # from mineru.utils.enum_class import MakeMode

        # self._convert_pdf_bytes = convert_pdf_bytes_to_bytes_by_pypdfium2
        # self._prepare_env = prepare_env
        # self._read_fn = read_fn
        # self._FileBasedDataWriter = FileBasedDataWriter
        # self._pipeline_doc_analyze = pipeline_doc_analyze
        # self._pipeline_union_make = pipeline_union_make
        # self._pipeline_result_to_middle_json = pipeline_result_to_middle_json
        # self._vlm_doc_analyze = vlm_doc_analyze
        # self._vlm_union_make = vlm_union_make
        # self._draw_layout_bbox = draw_layout_bbox
        # self._draw_span_bbox = draw_span_bbox
        # self._MakeMode = MakeMode

        logger.info(
            "MinerU initialized with effective config: {}, model source: {}",
            config_getenv("MINERU_TOOLS_CONFIG_JSON"),
            config_getenv("MINERU_MODEL_SOURCE"),
        )

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
                server_url=kwargs.get('server_url'),
                max_concurrency=kwargs.get('max_concurrency', 100),
                http_timeout=kwargs.get('http_timeout', 600),
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
                end_page_id=kwargs.get('end_page_id', None),
                keep_page_edges=kwargs.get('keep_page_edges'),
                page_edge_min_chars=kwargs.get('page_edge_min_chars'),
            )
            
            # 读取生成的 Markdown 文件
            # pipeline 后端通常使用 method（如 'auto'）作为子目录，VLM 后端使用 'vlm' 子目录
            method_used = kwargs.get('method', 'auto')
            backend_used = kwargs.get('backend', 'pipeline')

            if backend_used.startswith('pipeline'):
                preferred_dir = method_used
            else:
                preferred_dir = 'vlm'

            candidates = [preferred_dir, 'auto', 'vlm', backend_used]
            file_output_dir = None
            md_file = None
            stem = Path(file_path).stem
            for cand in candidates:
                try_dir = Path(output_dir) / stem / str(cand)
                candidate_md = try_dir / f"{stem}.md"
                if candidate_md.exists():
                    file_output_dir = try_dir
                    md_file = candidate_md
                    break

            if md_file is not None:
                content = md_file.read_text(encoding='utf-8')
            else:
                content = ""
                metadata['error'] = 'Markdown file not found'

            metadata['output_dir'] = str(file_output_dir) if file_output_dir is not None else str(Path(output_dir) / stem)
            
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
        max_concurrency: int = 100,
        http_timeout: int = 600,
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
        keep_page_edges: Any = None,
        page_edge_min_chars: Optional[int] = None,
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
        source_paths: list[str] = []
        pdf_bytes_list = []
        lang_list = []

        for path in paths:
            if not path.exists():
                logger.warning(f"文件不存在，跳过: {path}")
                continue
            file_names.append(path.stem)          # 不含扩展名的文件名
            source_paths.append(str(path))        # 原始路径，用于表单类文件判定（文件名含 `表单`）
            pdf_bytes_list.append(self._read_fn(path))  # 读取文件为字节
            lang_list.append(lang)

        if not file_names:
            logger.error("没有有效的输入文件")
            return []

        # 调用底层解析函数
        if backend.startswith("pipeline"):
            # 处理 pipeline 后端
            # 先对每个 PDF 进行页面裁剪（如果需要）
            for i, pdf_bytes in enumerate(pdf_bytes_list):
                pdf_bytes_list[i] = self._convert_pdf_bytes(
                    pdf_bytes, start_page_id, end_page_id
                )

            infer_results, all_image_lists, all_pdf_docs, lang_list_out, ocr_enabled_list = self._pipeline_doc_analyze(
                pdf_bytes_list,
                lang_list,
                parse_method=method,
                formula_enable=formula_enable,
                table_enable=table_enable
            )

            for idx, model_list in enumerate(infer_results):
                pdf_file_name = file_names[idx]
                # 表单类文件判定用原始文件名（文件名含 `表单`）
                source_name = source_paths[idx] if idx < len(source_paths) else pdf_file_name
                local_image_dir, local_md_dir = self._prepare_env(str(output_dir), pdf_file_name, method)
                image_writer = self._FileBasedDataWriter(local_image_dir)
                md_writer = self._FileBasedDataWriter(local_md_dir)

                images_list = all_image_lists[idx]
                pdf_doc = all_pdf_docs[idx]
                _lang = lang_list_out[idx]
                _ocr_enable = ocr_enabled_list[idx]

                middle_json = self._pipeline_result_to_middle_json(
                    model_list, images_list, pdf_doc, image_writer,
                    _lang, _ocr_enable, formula_enable
                )

                self._process_output(
                    pdf_info=middle_json["pdf_info"],
                    pdf_bytes=pdf_bytes_list[idx],
                    pdf_file_name=pdf_file_name,
                    source_name=source_name,
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
                    f_make_md_mode=self._MakeMode.MM_MD,
                    middle_json=middle_json,
                    model_output=model_list,
                    is_pipeline=True,
                    keep_page_edges=keep_page_edges,
                    page_edge_min_chars=page_edge_min_chars,
                )
                logger.success(f"解析完成: {pdf_file_name} -> {local_md_dir}")

        else:  # vlm 系列后端
            # 规范化 backend 值：支持简写 'vlm' 映射到 'vlm-transformers'
            # VLM 后端可能的完整值示例: 'vlm-transformers', 'vlm-vllm-engine', 'vlm-http-client'
            if backend == 'vlm':
                logger.info("Received shorthand backend 'vlm', mapping to 'vlm-transformers'")
                backend = 'vlm-transformers'
            if backend.startswith("vlm-"):
                backend = backend[4:]  # 去掉 'vlm-' 前缀
            for idx, pdf_bytes in enumerate(pdf_bytes_list):
                pdf_file_name = file_names[idx]
                source_name = source_paths[idx] if idx < len(source_paths) else pdf_file_name
                # VLM 也支持页面范围裁剪
                pdf_bytes = self._convert_pdf_bytes(pdf_bytes, start_page_id, end_page_id)
                local_image_dir, local_md_dir = self._prepare_env(str(output_dir), pdf_file_name, "vlm")
                image_writer = self._FileBasedDataWriter(local_image_dir)
                md_writer = self._FileBasedDataWriter(local_md_dir)

                middle_json, infer_result = self._vlm_doc_analyze(
                    pdf_bytes,
                    image_writer=image_writer,
                    backend=backend,
                    server_url=server_url,
                    max_concurrency=max_concurrency,
                    http_timeout=http_timeout,
                )

                self._process_output(
                    pdf_info=middle_json["pdf_info"],
                    pdf_bytes=pdf_bytes,
                    pdf_file_name=pdf_file_name,
                    source_name=source_name,
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
                    f_make_md_mode=self._MakeMode.MM_MD,
                    middle_json=middle_json,
                    model_output=infer_result,
                    is_pipeline=False,
                    keep_page_edges=keep_page_edges,
                    page_edge_min_chars=page_edge_min_chars,
                )
                logger.success(f"VLM解析完成: {pdf_file_name} -> {local_md_dir}")

        # 返回所有输出目录路径
        return [output_dir / name for name in file_names]

    def _process_output(self, **kwargs):
        """内部函数：处理输出文件的写入"""
        pdf_info = kwargs["pdf_info"]
        pdf_bytes = kwargs["pdf_bytes"]
        pdf_file_name = kwargs["pdf_file_name"]
        # 表单类文件判定用原始文件名；未显式传入时退回 pdf_file_name（stem）。
        source_name = kwargs.get("source_name") or pdf_file_name
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
        keep_page_edges = kwargs.get("keep_page_edges")
        page_edge_min_chars = kwargs.get("page_edge_min_chars")

        if f_draw_layout_bbox:
            self._draw_layout_bbox(pdf_info, pdf_bytes, local_md_dir, f"{pdf_file_name}_layout.pdf")
        if f_draw_span_bbox:
            self._draw_span_bbox(pdf_info, pdf_bytes, local_md_dir, f"{pdf_file_name}_span.pdf")
        if f_dump_orig_pdf:
            md_writer.write(f"{pdf_file_name}_origin.pdf", pdf_bytes)

        image_dir = str(os.path.basename(local_image_dir))

        if f_dump_md:
            make_func = self._pipeline_union_make if is_pipeline else self._vlm_union_make
            selection = normalize_keep_page_edges(keep_page_edges)
            if selection is None:
                md_content = make_func(pdf_info, f_make_md_mode, image_dir)
            else:
                md_content, injected = self._render_markdown_with_page_edges(
                    pdf_info=pdf_info,
                    make_func=make_func,
                    make_mode=f_make_md_mode,
                    image_dir=image_dir,
                    pdf_bytes=pdf_bytes,
                    selection=selection,
                    min_chars=DEFAULT_MIN_CHARS if page_edge_min_chars is None else int(page_edge_min_chars),
                )
                if injected:
                    logger.info(
                        "保留页眉/页脚: {} 页补回边缘文本 {}", len(injected), injected
                    )
            # MinerU 以 HTML 形式输出表格：行列式表出 columns/rows JSON，稀疏表出键值对 JSON。
            md_content, table_stats = convert_tables(md_content, source_name=source_name)
            if table_stats["tables"]:
                logger.info("表格转 JSON: {} 张", table_stats["tables"])
            if table_stats["forms"]:
                logger.info("稀疏表转键值对 JSON: {} 张", table_stats["forms"])
            md_writer.write_string(f"{pdf_file_name}.md", md_content)

        if f_dump_content_list:
            make_func = self._pipeline_union_make if is_pipeline else self._vlm_union_make
            content_list = make_func(pdf_info, self._MakeMode.CONTENT_LIST, image_dir)
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

    def _render_markdown_with_page_edges(
        self,
        *,
        pdf_info: list,
        make_func,
        make_mode: str,
        image_dir: str,
        pdf_bytes: bytes,
        selection: Any,
        min_chars: int,
    ) -> tuple[str, list[dict]]:
        """逐页渲染 Markdown，并为选中的页补回引擎丢弃的页眉/页脚。

        MinerU 的 Markdown 只渲染 ``para_blocks``，版面模型判为 ``abandon`` 的页面边缘
        内容（``discarded_blocks``）不会出现。这里按页渲染后用 PDF 文本层取回边缘行，
        并且只补回该页 Markdown 中缺失的行，避免与正文重复。
        """
        rendered: list[str] = []
        injected: list[dict] = []
        for index, page in enumerate(pdf_info or []):
            page_markdown = make_func([page], make_mode, image_dir)
            page_number = int(page.get("page_idx", index) or 0) + 1
            if page_is_kept(selection, page_number, 1):
                edges = extract_edge_lines(pdf_bytes, page_number, min_chars=min_chars)
                if not edges["top"] and not edges["bottom"]:
                    # 扫描件没有可用文本层，退回引擎自己判定的边缘块。
                    edges = self._discarded_edge_lines(page, min_chars)
                top = missing_lines(edges["top"], page_markdown)
                bottom = missing_lines(edges["bottom"], page_markdown)
                if top or bottom:
                    page_markdown = merge_page_edges(page_markdown, top, bottom)
                    injected.append({"page": page_number, "top": top, "bottom": bottom})
            rendered.append(page_markdown)
        content = "\n\n".join(text.strip() for text in rendered if text and text.strip())
        return (content + "\n", injected) if content else ("", injected)

    @staticmethod
    def _discarded_edge_lines(page: dict, min_chars: int) -> dict:
        """扫描件回退：按页面上下半区把 ``discarded_blocks`` 的文本行分成 top/bottom。

        注意 MinerU 的 bbox 以左上角为原点、y 向下增长。
        """
        size = page.get("page_size") or []
        height = float(size[1]) if len(size) == 2 and size[1] else 0.0
        edges: Dict[str, List[str]] = {"top": [], "bottom": []}
        for block in page.get("discarded_blocks") or []:
            bbox = block.get("bbox") or []
            center = (float(bbox[1]) + float(bbox[3])) / 2 if len(bbox) == 4 else 0.0
            zone = "bottom" if height and center > height / 2 else "top"
            edges[zone].extend(lines_from_blocks([block], min_chars))
        return edges
