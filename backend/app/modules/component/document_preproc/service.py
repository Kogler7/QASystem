"""
文档预处理组件：进程内调用 algorithm/preproc，按函数封装，不启动独立服务。
支持 PDF/DOCX/Excel 等转 Markdown/文本；使用前需安装 algorithm/preproc 依赖。
"""
import logging
import os
import sys
from pathlib import Path
from typing import Any
import importlib.util
from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parents[4]  # backend
_ROOT = _THIS.parents[5]  # qasystem
_PREPROC_ROOT = _ROOT / "algorithm" / "preproc"
if _PREPROC_ROOT.exists() and str(_PREPROC_ROOT) not in sys.path:
    sys.path.insert(0, str(_PREPROC_ROOT))

_OUTPUT_DIR = _BACKEND / "data" / "preproc_output"
_UPLOAD_DIR = _BACKEND / "data" / "preproc_upload"

_interface = None


def _get_interface():
    global _interface
    if _interface is None:
        try:
            from document_ingestion import DocumentConversionInterface
            _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

            # 解析后端 DATABASE_URL（如果可用），将 MySQL 参数传递给 preproc 的 DocumentConversionInterface
            db_config = None
            url = None
            try:
                env_db = os.environ.get('DATABASE_URL') or os.environ.get('DATABASE') or os.environ.get('DB_URL')
                if env_db:
                    url = make_url(env_db)
            except Exception:
                url = None

            if url is None:
                try:
                    # 尝试使用 backend 配置中的 settings
                    from ...config import settings as backend_settings  # type: ignore
                    url = make_url(backend_settings.DATABASE_URL)
                except Exception:
                    url = None

            if url is not None:
                try:
                    db_config = {
                        'mysql_host': url.host,
                        'mysql_port': url.port or 3306,
                        'mysql_database': url.database,
                        'mysql_username': url.username or '',
                        'mysql_password': url.password or ''
                    }
                except Exception:
                    db_config = None
            # 尝试从环境或 backend/.env 中补充 Elasticsearch 连接信息（es_host, es_port, es_username, es_password, es_scheme, es_verify_cert）
            try:
                # 优先使用环境变量
                es_host = os.environ.get('ES_HOST') or os.environ.get('CONFIDENCE_ES_HOSTS')
                es_port = os.environ.get('ES_PORT')
                es_username = os.environ.get('ES_USERNAME') or os.environ.get('ELASTIC_USERNAME')
                es_password = os.environ.get('ES_PASSWORD') or os.environ.get('ELASTIC_PASSWORD')
                es_scheme = os.environ.get('ES_SCHEME')
                es_verify = os.environ.get('ES_VERIFY_CERT')

                # 如果未在环境变量中找到，则尝试从 backend/.env 中读取（兼容本地 .env 启动）
                if not any([es_host, es_port, es_username, es_password, es_scheme, es_verify]):
                    env_path = _BACKEND / '.env'
                    if env_path.exists():
                        for ln in env_path.read_text(encoding='utf-8').splitlines():
                            ln = ln.strip()
                            if not ln or ln.startswith('#'):
                                continue
                            if '=' not in ln:
                                continue
                            k, v = ln.split('=', 1)
                            k = k.strip()
                            v = v.strip()
                            if k in ('ES_HOST', 'CONFIDENCE_ES_HOSTS') and not es_host:
                                es_host = v
                            if k == 'ES_PORT' and not es_port:
                                es_port = v
                            if k in ('ES_USERNAME', 'ELASTIC_USERNAME') and not es_username:
                                es_username = v
                            if k in ('ES_PASSWORD', 'ELASTIC_PASSWORD') and not es_password:
                                es_password = v
                            if k == 'ES_SCHEME' and not es_scheme:
                                es_scheme = v
                            if k == 'ES_VERIFY_CERT' and not es_verify:
                                es_verify = v

                # 解析 CONFIDENCE_ES_HOSTS/ES_HOST 可能包含 scheme://host:port
                if es_host and es_host.startswith('http') and (es_host.startswith('http://') or es_host.startswith('https://')):
                    try:
                        from urllib.parse import urlparse
                        parsed = urlparse(es_host)
                        if parsed.hostname:
                            es_scheme = es_scheme or parsed.scheme
                            es_host = parsed.hostname
                            es_port = es_port or (str(parsed.port) if parsed.port else None)
                    except Exception:
                        pass

                # 将解析到的 ES 信息写入 db_config
                if db_config is None:
                    db_config = {}
                if es_host:
                    db_config['es_host'] = es_host
                if es_port:
                    try:
                        db_config['es_port'] = int(es_port)
                    except Exception:
                        db_config['es_port'] = es_port
                if es_username:
                    db_config['es_username'] = es_username
                if es_password:
                    db_config['es_password'] = es_password
                if es_scheme:
                    db_config['es_scheme'] = es_scheme
                if es_verify is not None:
                    db_config['es_verify_cert'] = str(es_verify).lower() in ('1', 'true', 'yes')
            except Exception:
                pass

            # 先检查环境变量 PREPROC_ENABLE_DB；如果未设置，回退到 backend/.env 中的配置（便于本地 .env 启动场景）
            env_val = os.environ.get('PREPROC_ENABLE_DB')
            if env_val is None:
                # 读取 backend/.env（如果存在）寻找 PREPROC_ENABLE_DB
                try:
                    env_path = _BACKEND / '.env'
                    if env_path.exists():
                        for ln in env_path.read_text(encoding='utf-8').splitlines():
                            if ln.strip().startswith('PREPROC_ENABLE_DB'):
                                parts = ln.split('=', 1)
                                if len(parts) == 2:
                                    env_val = parts[1].strip()
                                break
                except Exception:
                    env_val = None

            enable_db_storage = False
            if env_val is not None:
                enable_db_storage = str(env_val).lower() in ('1', 'true', 'yes')

            logger.info(f"PREPROC_ENABLE_DB 生效值: {enable_db_storage}")

            # LibreOffice path (allow env override). If relative path is provided in backend/.env,
            # resolve it relative to the project preproc root so that deployments using relative paths work.
            libreoffice_path = os.environ.get('LIBREOFFICE_PATH') or os.environ.get('SOFFICE_PATH')
            if libreoffice_path and not Path(libreoffice_path).is_absolute():
                # Try resolving relative to algorithm/preproc first, then backend root, then repo root
                candidate = _PREPROC_ROOT / libreoffice_path
                if candidate.exists():
                    libreoffice_path = str(candidate)
                else:
                    candidate2 = _BACKEND / libreoffice_path
                    if candidate2.exists():
                        libreoffice_path = str(candidate2)
                    else:
                        candidate3 = _ROOT / libreoffice_path
                        if candidate3.exists():
                            libreoffice_path = str(candidate3)

            # Try to resolve mineru config path from env or backend/.env and convert relative paths into absolute
            mineru_config_path = None
            try:
                mineru_config_path = os.environ.get('MINERU_CONFIG_PATH') or os.environ.get('MINERU_TOOLS_CONFIG_JSON')
                if not mineru_config_path:
                    # try backend/.env parsed earlier
                    env_path = _BACKEND / '.env'
                    if env_path.exists():
                        for ln in env_path.read_text(encoding='utf-8').splitlines():
                            ln = ln.strip()
                            if not ln or ln.startswith('#'):
                                continue
                            if '=' not in ln:
                                continue
                            k, v = ln.split('=', 1)
                            k = k.strip()
                            v = v.strip()
                            if k in ('MINERU_CONFIG_PATH', 'MINERU_TOOLS_CONFIG_JSON') and not mineru_config_path:
                                mineru_config_path = v
                                break
                if mineru_config_path and not Path(mineru_config_path).is_absolute():
                    # resolve relative to algorithm/preproc, then backend, then repo root
                    cand = _PREPROC_ROOT / mineru_config_path
                    if cand.exists():
                        mineru_config_path = str(cand)
                    else:
                        cand2 = _BACKEND / mineru_config_path
                        if cand2.exists():
                            mineru_config_path = str(cand2)
                        else:
                            cand3 = _ROOT / mineru_config_path
                            if cand3.exists():
                                mineru_config_path = str(cand3)
            except Exception:
                mineru_config_path = None

            _interface = DocumentConversionInterface(
                output_format="md",
                save_to_file=True,
                output_dir=str(_OUTPUT_DIR),
                enable_db_storage=enable_db_storage,
                db_config=db_config,
                excel_to_mysql=True,
                mineru_config_path=mineru_config_path,
                mineru_model_source=os.environ.get("MINERU_MODEL_SOURCE", "local"),
                libreoffice_path=libreoffice_path,
            )
        except Exception as e:
            logger.warning("加载文档预处理模块失败: %s", e)
            raise
    return _interface


def health() -> dict[str, Any]:
    """检查文档预处理模块是否可加载。"""
    try:
        _get_interface()
        return {"status": "ok", "service": "document-preproc"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"文档预处理模块不可用: {e}。请确认已安装 algorithm/preproc 依赖（见 README/requirements）。",
        }


def convert_document(
    input_path: str,
    model_name: str | None = None,
    output_filename: str | None = None,
    save_metadata: bool = False,
    store_excel_raw: bool = False,
) -> dict[str, Any]:
    """
    处理单个文档，转为 Markdown/文本。
    由路由层用 asyncio.to_thread 调用。
    """
    if not input_path or not Path(input_path).exists():
        return {
            "success": False,
            "input_path": input_path,
            "error": "文件不存在或路径为空",
        }
    iface = _get_interface()
    try:
        result = iface.convert_document(
            input_path=input_path,
            model_name=model_name,
            output_filename=output_filename,
            save_metadata=save_metadata,
            store_excel_raw=store_excel_raw,
        )
    except Exception as e:
        logger.error("文档预处理异常: %s", e, exc_info=True)
        return {
            "success": False,
            "input_path": input_path,
            "error": f"{type(e).__name__}: {e}",
        }
    if result is None:
        return {
            "success": False,
            "input_path": input_path,
            "error": "文档预处理未返回结果",
        }
    # 不要截断 content：知识沉淀链路需要完整文本；仅提供 preview 供前端展示/日志排查。
    if result.get("content") and isinstance(result["content"], str):
        content_str = result["content"]
        if len(content_str) > 20000:
            result["content_preview"] = content_str[:20000] + "\n...(预览截断)"
    return result


def get_available_models() -> dict[str, Any]:
    """获取可用的文档处理模型信息。"""
    try:
        iface = _get_interface()
        processors = iface.processor.available_processors
        
        result = {}
        for file_type, config in processors.items():
            result[file_type] = {
                "default": config["default"],
                "models": {}
            }
            for model_name, model_data in config["models"].items():
                metadata = model_data["metadata"]
                result[file_type]["models"][model_name] = {
                    "name": metadata["name"],
                    "description": metadata["description"],
                    "pros": [metadata["优点"]],
                    "cons": [metadata["缺点"]],
                    "scenario": metadata["适用场景"]
                }
        return result
    except Exception as e:
        logger.error("获取可用模型信息失败: %s", e)
        return {}


def get_upload_dir() -> Path:
    """返回上传临时目录，供路由保存文件。"""
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return _UPLOAD_DIR


def convert_to_pdf(input_path: str) -> str | None:
    """
    封装预处理模块中对 DOC/DOCX 转 PDF 的能力，优先使用 preproc 的 DocxProcessor.convert_to_pdf。
    返回生成的 PDF 文件绝对路径，失败时返回 None 或抛出异常。
    """
    if not input_path or not Path(input_path).exists():
        return None

    try:
        # 尝试以包路径导入（preproc 已被插入到 sys.path）
        try:
            from preproc_src.processors.docx_processor import DocxProcessor  # type: ignore
        except Exception:
            try:
                from processors.docx_processor import DocxProcessor  # type: ignore
            except Exception:
                # 最后按文件路径动态加载模块
                module_path = _PREPROC_ROOT / "preproc_src" / "processors" / "docx_processor.py"
                if not module_path.exists():
                    raise ImportError("找不到 docx_processor 模块")
                spec = importlib.util.spec_from_file_location("docx_processor", str(module_path))
                mod = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(mod)  # type: ignore
                DocxProcessor = getattr(mod, "DocxProcessor")

        processor = DocxProcessor()
        pdf_path = processor.convert_to_pdf(input_path)
        return pdf_path
    except Exception:
        raise
