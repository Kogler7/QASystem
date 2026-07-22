from __future__ import annotations

import importlib.util
import os
import shutil
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MODEL_PATHS = {
    "docling": Path("/models/docling-models"),
    "mineru_pipeline": Path("/models/mineru/pipeline"),
    "mineru_vlm": Path("/models/mineru/vlm"),
}
OPTIONAL_MODEL_ENV = (
    "VECTOR_MODEL_PATH",
    "CONFIDENCE_SBERT_MODEL_PATH",
    "NL2CYPHER_EMBEDDING_MODEL_PATH",
)


def _directory_check(path: Path) -> dict[str, Any]:
    exists = path.is_dir()
    populated = exists and any(path.iterdir())
    return {"path": str(path), "ok": populated, "reason": None if populated else "目录不存在或为空"}


def _optional_directory_check(name: str) -> dict[str, Any]:
    value = os.getenv(name, "").strip()
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


def runtime_checks() -> dict[str, Any]:
    checks = {
        "docling_module": {"ok": importlib.util.find_spec("docling") is not None},
        "mineru_module": {"ok": importlib.util.find_spec("mineru") is not None},
        "libreoffice": {"ok": shutil.which("soffice") is not None},
        "java": {"ok": shutil.which("java") is not None},
        "ofd_converter": {"ok": Path("/opt/qasystem/ofdrw/ofd-to-pdf.jar").is_file()},
    }
    checks.update({name: _directory_check(path) for name, path in MODEL_PATHS.items()})
    checks.update({name.lower(): _optional_directory_check(name) for name in OPTIONAL_MODEL_ENV})
    return checks


def dependency_checks() -> dict[str, Any]:
    database_port = os.getenv("DATABASE_PORT", "3306")
    try:
        database_port_number = int(database_port)
    except ValueError:
        database_port_number = None
    es_hosts = os.getenv("CONFIDENCE_ES_HOSTS", "").split(",")[0].strip()
    return {
        "mysql": _tcp_check("mysql", os.getenv("DATABASE_HOST", "").strip(), database_port_number),
        "neo4j": _url_tcp_check("neo4j", os.getenv("NEO4J_URI", "").strip(), 7687),
        "elasticsearch": _url_tcp_check("elasticsearch", es_hosts, 9200),
        "knowledge_llm": _url_tcp_check(
            "knowledge_llm", os.getenv("KG_EXTRACT_LLM_BASE_URL", "").strip(), 80
        ),
        "file_service": _url_tcp_check(
            "file_service", os.getenv("FILE_SERVICE_URL", "").strip(), 80
        ),
    }


def full_health() -> dict[str, Any]:
    checks = {**runtime_checks(), **dependency_checks()}
    failed = [name for name, detail in checks.items() if not detail["ok"]]
    return {"status": "ok" if not failed else "failed", "checks": checks, "failed": failed}
