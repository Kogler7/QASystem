from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv

import importlib.util
import os
import shutil
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from algorithm.runtime_profile import is_remote_only

MODEL_PATHS = {
    "docling": Path("/models/docling-models"),
}
OPTIONAL_MODEL_ENV = (
    "VECTOR_MODEL_PATH",
)


def _directory_check(path: Path) -> dict[str, Any]:
    exists = path.is_dir()
    populated = exists and any(path.iterdir())
    return {"path": str(path), "ok": populated, "reason": None if populated else "目录不存在或为空"}


def _optional_directory_check(name: str) -> dict[str, Any]:
    value = config_getenv(name, "").strip()
    if not value:
        return {"ok": True, "skipped": True, "reason": "未配置"}
    return _directory_check(Path(value))


def _tcp_check(name: str, host: str | None, port: int | None) -> dict[str, Any]:
    if not host or not port:
        return {"ok": True, "skipped": True, "reason": "未配置"}
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError as exc:
        return {"ok": False, "host": host, "port": port, "reason": str(exc)}
    return {"ok": True, "host": host, "port": port}


def _url_tcp_check(name: str, value: str, default_port: int) -> dict[str, Any]:
    parsed = urlparse(value if "://" in value else f"//{value}")
    try:
        port = parsed.port or default_port
    except ValueError as exc:
        return {"ok": False, "reason": f"地址格式无效：{exc}"}
    return _tcp_check(name, parsed.hostname, port)


def _required_url_tcp_check(name: str, value: str, default_port: int) -> dict[str, Any]:
    if not value:
        return {"ok": False, "reason": "remote-only 模式必须配置外部服务 URL"}
    return _url_tcp_check(name, value, default_port)


def runtime_checks() -> dict[str, Any]:
    from algorithm.preproc.preproc_src.processors.mineru4_processor import mineru_major_version

    mineru_major = mineru_major_version()
    checks = {
        "docling_module": {"ok": importlib.util.find_spec("docling") is not None},
        "mineru_module": {
            "ok": mineru_major == 4,
            "major_version": mineru_major,
            "reason": None if mineru_major == 4 else "主 Python 环境未安装受支持的 MinerU 4.x",
        },
        "libreoffice": {"ok": shutil.which("soffice") is not None},
        "java": {"ok": shutil.which("java") is not None},
        "ofd_converter": {"ok": Path("/opt/qasystem/ofdrw/ofd-to-pdf.jar").is_file()},
    }
    if is_remote_only():
        checks = {
            "local_models": {"ok": True, "skipped": True, "reason": "remote-only 模式不加载本地模型"},
            "gpu_runtime": {"ok": True, "skipped": True, "reason": "remote-only 模式不启动 GPU 进程"},
            **checks,
        }
    else:
        checks.update({name: _directory_check(path) for name, path in MODEL_PATHS.items()})
        from qasystem_config.mineru_adapter import settings, model_report
        try:
            checks["mineru4"] = model_report(settings(), config_getenv("MINERU4_TIER"), check_lock=True)
        except (ValueError, OSError) as exc:
            checks["mineru4"] = {"ok": False, "reason": str(exc)}
        from qasystem_config.model_registry import worker_record
        record = worker_record()
        if not record or not record.get('enabled'):
            checks["mineru_vlm"] = {"ok": True, "skipped": True, "reason": "VLLM 未启用，2.x VLM 权重不参与运行"}
        else:
            checks["mineru_vlm"] = _directory_check(Path("/models") / record["model_dir"])
        checks.update({name.lower(): _optional_directory_check(name) for name in OPTIONAL_MODEL_ENV})
    return checks


def _sql_database_check(value: str) -> dict[str, Any]:
    """Inspect the actual SQL connection without exposing its credentials."""
    if not value.strip():
        return {"ok": True, "skipped": True, "reason": "未配置关系型 SQL 数据库"}
    from sqlalchemy.engine import make_url
    try:
        url = make_url(value)
        dialect = url.get_backend_name()
        if dialect == "sqlite":
            return {"ok": True, "skipped": True, "dialect": dialect, "reason": "SQLite 无网络端口；本检查不验证文件可读性"}
        default_ports = {"mysql": 3306, "mariadb": 3306, "postgresql": 5432, "mssql": 1433, "oracle": 1521}
        port = url.port or default_ports.get(dialect)
        host = url.host
        if not host or not port or not 0 < port <= 65535:
            return {"ok": False, "reason": "SQL 连接缺少有效主机或端口；特殊连接需显式提供端口"}
    except (ValueError, TypeError):
        return {"ok": False, "reason": "关系型 SQL 数据库连接格式无效"}
    except Exception:
        return {"ok": False, "reason": "关系型 SQL 数据库连接无法解析"}
    return {"dialect": dialect, **_tcp_check("sql_database", host, port)}


def dependency_checks() -> dict[str, Any]:
    from external_services.application import get_application_service_url
    from external_services.model_routes import resolve_service
    llm_service = resolve_service('extract')

    checks = {
        "sql_database": _sql_database_check(config_getenv("DATABASE_URL", "")),
        "knowledge_llm": _url_tcp_check(
            "knowledge_llm", llm_service['base_url'] if llm_service else '', 80
        ),
        "application_service": _url_tcp_check(
            "application_service", get_application_service_url(), 80
        ),
    }
    if is_remote_only():
        checks["document_parse_service"] = _required_url_tcp_check(
            "document_parse_service", config_getenv("DOCUMENT_PARSE_SERVICE_URL", "").strip(), 80
        )
        checks["embedding_service"] = _required_url_tcp_check(
            "embedding_service", config_getenv("EMBEDDING_SERVICE_URL", "").strip(), 80
        )
    return checks


def full_health() -> dict[str, Any]:
    checks = {**runtime_checks(), **dependency_checks()}
    failed = [name for name, detail in checks.items() if not detail["ok"]]
    return {"status": "ok" if not failed else "failed", "checks": checks, "failed": failed}
