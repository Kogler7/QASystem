"""Fail-fast verification for the built QASystem runtime image."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path


MODULES = (
    "PIL",
    "agentcode",
    "bs4",
    "docling",
    "dotenv",
    "elasticsearch",
    "fastapi",
    "httpx",
    "jsonschema",
    "langchain_core",
    "langchain_text_splitters",
    "loguru",
    "matplotlib",
    "mcp",
    "mineru",
    "neo4j",
    "numpy",
    "openai",
    "openpyxl",
    "pandas",
    "pydantic",
    "pymysql",
    "pypdf",
    "redis",
    "requests",
    "sentence_transformers",
    "sqlalchemy",
    "tiktoken",
    "torch",
    "torchvision",
    "uvicorn",
    "yaml",
)

PROGRAMS = (
    "bash",
    "curl",
    "ffmpeg",
    "file",
    "git",
    "java",
    "jq",
    "libreoffice",
    "node",
    "opencode",
    "rg",
    "soffice",
    "tini",
)


def _check_modules() -> None:
    failures: list[str] = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # pragma: no cover - executed inside image
            failures.append(f"{name}: {exc!r}")
    if failures:
        raise RuntimeError("module import failures:\n" + "\n".join(failures))


def _check_programs() -> None:
    missing = [name for name in PROGRAMS if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"missing runtime programs: {', '.join(missing)}")

    subprocess.run(
        ["libreoffice", "--headless", "--version"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )


def _check_application() -> None:
    from docling.datamodel.base_models import InputFormat

    assert hasattr(InputFormat, "PPTX")
    assert hasattr(InputFormat, "IMAGE")
    if not shutil.which("java"):
        raise RuntimeError("missing Java runtime required by the OFD converter")
    if not Path("/opt/qasystem/ofdrw/ofd-to-pdf.jar").is_file():
        raise RuntimeError("missing bundled ofdrw OFD converter")
    converter = subprocess.run(
        ["java", "-jar", "/opt/qasystem/ofdrw/ofd-to-pdf.jar"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )
    if converter.returncode != 2 or "Usage: ofd-to-pdf" not in converter.stdout:
        raise RuntimeError(f"bundled OFD converter is not executable: {converter.stdout}")

    from agentcode.api import app as agent_app
    from backend.app.main import app as backend_app

    assert any(route.path == "/health" for route in agent_app.routes)
    assert any(route.path == "/health" for route in backend_app.routes)


def main() -> int:
    _check_modules()
    _check_programs()
    _check_application()
    print("QASystem runtime verification passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
