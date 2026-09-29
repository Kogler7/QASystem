from __future__ import annotations

import base64
import os
import sys
import tempfile

import pytest

from agentcode.tools.base_tools.shell_tool import ShellTool, _safe_decode

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux container command tests")


def _make_script(content: str) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    f.write(content)
    f.close()
    return f.name


class TestExecution:
    def test_echo(self) -> None:
        st = ShellTool()
        result = st._execute("echo hello world")
        assert "hello world" in result
        assert not result.startswith("Error:")

    def test_command_with_args(self) -> None:
        st = ShellTool()
        result = st._execute("echo -n test123")
        assert "test123" in result

    def test_ls_tmp(self) -> None:
        st = ShellTool()
        result = st._execute("ls /tmp")
        assert not result.startswith("Error:")

    def test_python_inline(self) -> None:
        st = ShellTool(timeout=5)
        result = st._execute("python -c \"print('pytest_ok')\"")
        assert "pytest_ok" in result

    def test_empty_command(self) -> None:
        st = ShellTool()
        result = st._execute("")
        assert result.startswith("Error:")
        assert "命令为空" in result

    def test_whitespace_only(self) -> None:
        st = ShellTool()
        result = st._execute("   ")
        assert result.startswith("Error:")
        assert "命令为空" in result

    def test_nonexistent_command(self) -> None:
        st = ShellTool()
        result = st._execute("nonexistent_cmd_xyz123")
        assert result.startswith("Error:")
        assert "未找到可执行文件" in result

    def test_validation_reject_blocks_execution(self) -> None:
        st = ShellTool()
        result = st._execute("rm -rf /tmp/test --no-preserve-root")
        assert result.startswith("Error:")
        assert "安全策略拒绝" in result

    def test_output_truncation(self) -> None:
        st = ShellTool(max_output_bytes=50)
        result = st._execute("python -c \"print('x' * 5000)\"")
        assert "输出已截断" in result

    def test_work_dir(self) -> None:
        st = ShellTool(work_dir="/tmp")
        result = st._execute("ls -d")
        assert not result.startswith("Error:")


class TestBinaryOutputEncoding:
    def test_binary_output_falls_back_to_base64(self) -> None:
        path = _make_script("import sys\nsys.stdout.buffer.write(bytes(range(256)))\n")
        try:
            st = ShellTool(max_output_bytes=1024 * 1024)
            result = st._execute(f"python {path}")
            assert "[base64]" in result
        finally:
            os.unlink(path)

    def test_binary_output_base64_roundtrip(self) -> None:
        path = _make_script("import sys\nsys.stdout.buffer.write(bytes(range(256)))\n")
        try:
            st = ShellTool(max_output_bytes=1024 * 1024)
            original = bytes(range(256))
            result = st._execute(f"python {path}")
            lines = result.split("\n")
            b64_line = lines[1] if len(lines) > 1 else ""
            decoded = base64.b64decode(b64_line)
            assert decoded == original
        finally:
            os.unlink(path)

    def test_utf8_output_not_base64(self) -> None:
        st = ShellTool()
        result = st._execute("echo 你好世界")
        assert "[base64]" not in result
        assert "你好世界" in result


class TestSafeDecode:
    def test_empty_bytes(self) -> None:
        assert _safe_decode(b"", 100) == ""

    def test_utf8_text(self) -> None:
        assert "hello" == _safe_decode(b"hello\n", 100)

    def test_truncation(self) -> None:
        assert "输出已截断" in _safe_decode(b"x" * 200, 100)

    def test_binary_base64_mode(self) -> None:
        raw = bytes(range(256))
        result = _safe_decode(raw, 1024 * 1024)
        assert "[base64]" in result
        decoded = base64.b64decode(result.split("\n")[1])
        assert decoded == raw
