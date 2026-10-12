from __future__ import annotations
from docker.configuration import getenv as config_getenv

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
    # MinerU 2.x 已退役：本地解析回退层是 4.x，可用性由 main_processor 的
    # _local_mineru4_available（主解释器安装版本为 4.x）判定；
    # 4.0 模型目录另见 runtime_checks 的 mineru4 检查。
    "mineru_pipeline": Path("/models/mineru/pipeline"),
}
OPTIONAL_MODEL_ENV = (
    "VECTOR_MODEL_PATH",
    "NL2CYPHER_EMBEDDING_MODEL_PATH",
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
        checks["mineru4"] = _directory_check(Path(config_getenv("MINERU_MODEL_BASE_DIR", "/models/mineru4")))
        if config_getenv("VLLM_ENABLED", "true").lower() in {"0", "false", "off", "no"}:
            checks["mineru_vlm"] = {"ok": True, "skipped": True, "reason": "VLLM 未启用，2.x VLM 权重不参与运行"}
        else:
            checks["mineru_vlm"] = _directory_check(Path("/models/mineru/vlm"))
        checks.update({name.lower(): _optional_directory_check(name) for name in OPTIONAL_MODEL_ENV})
    return checks


def dependency_checks() -> dict[str, Any]:
    database_port = config_getenv("DATABASE_PORT", "3306")
    try:
        database_port_number = int(database_port)
    except ValueError:
        database_port_number = None
    from external_services.application import get_application_service_url

    checks = {
        "mysql": _tcp_check("mysql", config_getenv("DATABASE_HOST", "").strip(), database_port_number),
        "knowledge_llm": _url_tcp_check(
            "knowledge_llm", config_getenv("KG_EXTRACT_LLM_BASE_URL", "").strip(), 80
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
