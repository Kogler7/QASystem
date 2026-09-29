from __future__ import annotations

import os
import sys
import tempfile
import uuid

import pytest

from agentcode.tools.base_tools.shell_tool import ShellTool

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource-limit tests")


def _make_script(content: str) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    f.write(content)
    f.close()
    return f.name


class TestResourceLimitsConfig:
    def test_default_limits_set(self) -> None:
        st = ShellTool()
        assert st._mem_limit_mb == 512
        assert st._cpu_limit_s == 0
        assert st._file_size_limit_mb == 10
        assert st._nproc_limit == 50
        assert st._limits_preexec is not None

    def test_custom_limits(self) -> None:
        st = ShellTool(mem_limit_mb=256, cpu_limit_s=5, file_size_limit_mb=2, nproc_limit=10)
        assert st._mem_limit_mb == 256
        assert st._cpu_limit_s == 5
        assert st._file_size_limit_mb == 2
        assert st._nproc_limit == 10

    def test_all_limits_zero_returns_none_preexec(self) -> None:
        st = ShellTool(mem_limit_mb=0, cpu_limit_s=0, file_size_limit_mb=0, nproc_limit=0)
        assert st._limits_preexec is None

    @pytest.mark.skipif(
        ShellTool()._resource_enabled is False,
        reason="resource module not available on this platform",
    )
    def test_normal_command_with_limits_works(self) -> None:
        st = ShellTool()
        result = st._execute("echo resource_limit_ok")
        assert "resource_limit_ok" in result


class TestMemoryLimit:
    @pytest.mark.skipif(
        ShellTool()._resource_enabled is False,
        reason="resource module not available on this platform",
    )
    def test_memory_limit_blocks_large_alloc(self) -> None:
        path = _make_script(
            "try:\n    x = bytearray(512 * 1024 * 1024)\n    print('alloc_ok')\nexcept MemoryError:\n    print('oom_caught')\n"
        )
        try:
            st = ShellTool(mem_limit_mb=64, timeout=10, cpu_limit_s=0, file_size_limit_mb=0, nproc_limit=0, max_output_bytes=1024 * 1024)
            result = st._execute(f"python {path}")
            assert "alloc_ok" not in result
        finally:
            os.unlink(path)


class TestCpuLimit:
    @pytest.mark.skipif(
        ShellTool()._resource_enabled is False,
        reason="resource module not available on this platform",
    )
    def test_cpu_limit_kills_infinite_loop(self) -> None:
        path = _make_script("while True:\n    pass\n")
        try:
            st = ShellTool(cpu_limit_s=1, timeout=5, mem_limit_mb=0, file_size_limit_mb=0, nproc_limit=0)
            result = st._execute(f"python {path}")
            assert "被终止" in result or "Error" in result
        finally:
            os.unlink(path)


class TestFileSizeLimit:
    @pytest.mark.skipif(
        ShellTool()._resource_enabled is False,
        reason="resource module not available on this platform",
    )
    def test_file_size_limit_prevents_large_write(self) -> None:
        testfile = f"/tmp/st_test_{uuid.uuid4().hex[:8]}.bin"
        path = _make_script(
            f"with open('{testfile}', 'wb') as f:\n    f.write(b'x' * (5 * 1024 * 1024))\nprint('done')\n"
        )
        try:
            st = ShellTool(file_size_limit_mb=1, timeout=10, mem_limit_mb=0, cpu_limit_s=0, nproc_limit=0, max_output_bytes=1024 * 1024)
            result = st._execute(f"python {path}")
            if os.path.exists(testfile):
                sz = os.path.getsize(testfile)
                assert sz <= 1048576 + 1024, f"File too large: {sz}"
                os.unlink(testfile)
        finally:
            os.unlink(path)


class TestNprocLimit:
    @pytest.mark.skipif(
        ShellTool()._resource_enabled is False,
        reason="resource module not available on this platform",
    )
    def test_nproc_limit_stops_fork_bomb(self) -> None:
        path = _make_script(
            "import os\ntry:\n    while True:\n        os.fork()\nexcept:\n    print('fork_blocked')\n"
        )
        try:
            st = ShellTool(nproc_limit=5, timeout=5, mem_limit_mb=0, cpu_limit_s=0, file_size_limit_mb=0, max_output_bytes=1024 * 1024)
            result = st._execute(f"python {path}")
            assert "fork_blocked" in result
        finally:
            os.unlink(path)
