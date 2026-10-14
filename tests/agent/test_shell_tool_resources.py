from __future__ import annotations

import os
import shlex
import sys
import tempfile
import uuid

import pytest

from agentcode.tools.base_tools.shell_tool import ShellTool
from agentcode.tools.base_tools import shell_tool

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
        # Darwin's existing VM map exceeds the default address-space limit.
        # Exercise the remaining supported limits here; memory is tested below.
        st = ShellTool(mem_limit_mb=0) if sys.platform == "darwin" else ShellTool()
        result = st._execute("echo resource_limit_ok")
        assert "resource_limit_ok" in result


class TestResourceLimitSetupFailure:
    @pytest.mark.parametrize("error_type", [ValueError, OSError])
    @pytest.mark.parametrize(
        ("option", "limit_name"),
        [
            ("mem_limit_mb", "RLIMIT_AS"),
            ("cpu_limit_s", "RLIMIT_CPU"),
            ("file_size_limit_mb", "RLIMIT_FSIZE"),
            ("nproc_limit", "RLIMIT_NPROC"),
        ],
    )
    def test_rejected_limit_prevents_command_side_effects(self, tmp_path, monkeypatch, option, limit_name, error_type) -> None:
        marker = tmp_path / "must_not_exist"
        script = tmp_path / "side_effect.py"
        script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")

        def reject_limit(*args):
            raise error_type("injected setrlimit failure")

        monkeypatch.setattr(shell_tool._resource_module, "setrlimit", reject_limit)
        limits = dict(mem_limit_mb=0, cpu_limit_s=0, file_size_limit_mb=0, nproc_limit=0)
        limits[option] = 1
        st = ShellTool(**limits)
        result = st._execute(f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
        assert result.startswith("Error: 无法应用子进程资源限制")
        assert limit_name in result
        assert "命令未执行" in result
        assert not marker.exists()

    def test_missing_resource_backend_prevents_execution(self, monkeypatch) -> None:
        monkeypatch.setattr(shell_tool, "_HAS_RESOURCE", False)

        def must_not_run(*args, **kwargs):
            pytest.fail("unsupported resource limits must prevent subprocess creation")

        monkeypatch.setattr(shell_tool.subprocess, "run", must_not_run)
        result = ShellTool()._execute("echo must_not_execute")
        assert result.startswith("Error: 当前平台不支持所请求的资源限制")
        assert "命令未执行" in result


class TestMemoryLimit:
    @pytest.mark.skipif(
        ShellTool()._resource_enabled is False,
        reason="resource module not available on this platform",
    )
    def test_memory_limit_blocks_large_alloc(self, tmp_path) -> None:
        marker = tmp_path / "command_started"
        path = _make_script(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
            "try:\n    x = bytearray(512 * 1024 * 1024)\n    print('alloc_ok')\nexcept MemoryError:\n    print('oom_caught')\n"
        )
        try:
            st = ShellTool(mem_limit_mb=64, timeout=10, cpu_limit_s=0, file_size_limit_mb=0, nproc_limit=0, max_output_bytes=1024 * 1024)
            result = st._execute(f"{shlex.quote(sys.executable)} {shlex.quote(path)}")
            assert "alloc_ok" not in result
            if result.startswith("Error: 无法应用子进程资源限制"):
                # Darwin rejects RLIMIT_AS below its existing VM map. Refusing
                # to execute is the safe outcome; a generic launch error is not.
                assert "RLIMIT_AS(MB)=64" in result
                assert not marker.exists()
            else:
                assert marker.exists(), result
                assert "oom_caught" in result
        finally:
            os.unlink(path)


class TestCpuLimit:
    @pytest.mark.skipif(
        ShellTool()._resource_enabled is False,
        reason="resource module not available on this platform",
    )
    def test_cpu_limit_kills_infinite_loop(self, tmp_path) -> None:
        marker = tmp_path / "cpu_command_started"
        path = _make_script(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nwhile True:\n    pass\n")
        try:
            st = ShellTool(cpu_limit_s=1, timeout=5, mem_limit_mb=0, file_size_limit_mb=0, nproc_limit=0)
            result = st._execute(f"{shlex.quote(sys.executable)} {shlex.quote(path)}")
            assert marker.exists(), result
            assert "被终止" in result
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
            result = st._execute(f"{shlex.quote(sys.executable)} {shlex.quote(path)}")
            assert os.path.exists(testfile), result
            sz = os.path.getsize(testfile)
            assert sz <= 1048576, f"File too large: {sz}"
            assert "done" not in result
        finally:
            os.unlink(path)
            if os.path.exists(testfile):
                os.unlink(testfile)


class TestNprocLimit:
    @pytest.mark.skipif(
        ShellTool()._resource_enabled is False,
        reason="resource module not available on this platform",
    )
    def test_nproc_limit_stops_additional_forks(self) -> None:
        path = _make_script(
            # Bound the experiment even if the kernel fails to enforce NPROC.
            # Children exit immediately; only the original process can fork.
            "import os\nchildren = []\ntry:\n"
            "    for _ in range(8):\n        pid = os.fork()\n"
            "        if pid == 0:\n            os._exit(0)\n"
            "        children.append(pid)\n"
            "except OSError:\n    print('fork_blocked')\n"
            "finally:\n    for pid in children:\n        os.waitpid(pid, 0)\n"
        )
        try:
            st = ShellTool(nproc_limit=5, timeout=5, mem_limit_mb=0, cpu_limit_s=0, file_size_limit_mb=0, max_output_bytes=1024 * 1024)
            result = st._execute(f"{shlex.quote(sys.executable)} {shlex.quote(path)}")
            assert "fork_blocked" in result
        finally:
            os.unlink(path)
