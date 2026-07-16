from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


class OFDConverter:
    """Convert OFD documents to PDF through the bundled ofdrw command-line tool."""

    DEFAULT_COMMAND = "java -jar /opt/qasystem/ofdrw/ofd-to-pdf.jar"

    def __init__(self, command: str | None = None, timeout: int = 120):
        self.command = command or os.getenv("OFD_CONVERTER_COMMAND") or self.DEFAULT_COMMAND
        self.timeout = timeout
        self.last_conversion_error: str | None = None

    def convert_to_pdf(self, file_path: str) -> str | None:
        self.last_conversion_error = None
        source = Path(file_path)
        if source.suffix.lower() != ".ofd":
            self.last_conversion_error = "OFD 转换失败：输入文件不是 .ofd 格式"
            return None
        if not source.is_file():
            self.last_conversion_error = "OFD 转换失败：输入文件不存在"
            return None

        command = shlex.split(self.command)
        if not command or shutil.which(command[0]) is None:
            self.last_conversion_error = (
                "缺少 OFD 转换器：请安装镜像内置的 Java/ofdrw，或设置 OFD_CONVERTER_COMMAND"
            )
            return None

        temp_dir = tempfile.mkdtemp(prefix="ofd_to_pdf_")
        output = Path(temp_dir) / f"{source.stem}.pdf"
        try:
            process = subprocess.run(
                [*command, str(source), str(output)],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            if process.returncode == 0 and output.is_file() and output.stat().st_size > 0:
                return str(output)
            detail = (process.stderr or process.stdout or "转换器未生成 PDF 文件").strip()
            self.last_conversion_error = f"OFD 转换为 PDF 失败：{detail}"
        except subprocess.TimeoutExpired:
            self.last_conversion_error = f"OFD 转换为 PDF 超时（{self.timeout} 秒）"
        except OSError as exc:
            self.last_conversion_error = f"OFD 转换器执行失败：{exc}"
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None
