from __future__ import annotations
from docker.configuration import getenv as config_getenv

import base64
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Any

try:
    import resource as _resource_module

    _HAS_RESOURCE = True
except ImportError:
    _HAS_RESOURCE = False

logger = logging.getLogger(__name__)

from pathlib import Path as _Path

_log_dir = _Path(config_getenv("AGENT_LOG_DIR") or tempfile.gettempdir()) / "agentcode"
try:
    _log_dir.mkdir(parents=True, exist_ok=True)
    _fh = logging.FileHandler(str(_log_dir / "shell_tool.log"), encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_fh)
except OSError:
    logger.addHandler(logging.NullHandler())

BLOCKED_COMMANDS = {
    "rm",
    "mkfs",
    "dd",
    "chmod",
    "chown",
    "chattr",
    "sudo",
    "su",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "init",
    "systemctl",
    "service",
    "killall",
    "pkill",
    "xargs",
    "crontab",
    "iptables",
    "ufw",
    "firewall-cmd",
}

DANGEROUS_ARG_PATTERNS = [
    re.compile(r"rm\s+.*-r\s*\S*\s*/", re.IGNORECASE),
    re.compile(r"rm\s+.*-rf", re.IGNORECASE),
    re.compile(r"rm\s+.*--no-preserve-root", re.IGNORECASE),
    re.compile(r">\s*/dev/sd[a-z]", re.IGNORECASE),
    re.compile(r">\s*/dev/nvme", re.IGNORECASE),
    re.compile(r">\s*/dev/hd[a-z]", re.IGNORECASE),
    re.compile(r">\s*/dev/xvd", re.IGNORECASE),
    re.compile(r">\s*/dev/vd[a-z]", re.IGNORECASE),
    re.compile(r">\s*/dev/mmcblk", re.IGNORECASE),
    re.compile(r"dd\s+.*if=.*of=", re.IGNORECASE),
    re.compile(r"mkfs\.\S+", re.IGNORECASE),
    re.compile(r"chmod\s+.*777", re.IGNORECASE),
    re.compile(r"chmod\s+.*-R\s+777", re.IGNORECASE),
    re.compile(r":\(\)\s*\{.*:\|:&.*\};:", re.IGNORECASE),
    re.compile(r"wget\s+.*\|\s*(?:ba)?sh", re.IGNORECASE),
    re.compile(r"curl\s+.*\|\s*(?:ba)?sh", re.IGNORECASE),
]

SHELL_METACHARACTERS = {"&", "|", ";", "`", "$(", ">", ">>", "<", "<<", "${", "&&", "||"}


def _safe_decode(raw: bytes, max_bytes: int) -> str:
    """将子进程输出的原始字节安全转为可读字符串。

    优先尝试 UTF-8 解码；若失败（如命令输出了二进制数据），
    则截断后用 base64 编码，保留完整信息供后续分析。

    Args:
        raw: 子进程 stdout/stderr 原始字节。
        max_bytes: 输出截断阈值（字节）。

    Returns:
        解码后的字符串，可能包含 base64 标注头或截断提示。
    """
    if not raw:
        return ""

    truncated = False
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        truncated = True

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        b64 = base64.b64encode(raw).decode("ascii")
        result = f"[base64]\n{b64}"
        if truncated:
            result += "\n\n[输出已截断]"
        return result

    text = text.rstrip("\n\r")
    if truncated:
        text += "\n\n[输出已截断]"
    return text


class ShellTool:
    """Agent 命令行执行工具，允许 agent 在受控环境中执行 shell 命令。

    提供 **execute_command** 工具，LLM 可通过 function calling 执行命令。
    内置多层安全防护防止危险操作。

    Attributes:
        _allowed_commands: 允许执行的可执行文件白名单（为空则仅依赖黑名单拦截）。
        _work_dir: 命令执行工作目录。
        _timeout: 单次命令执行超时（秒）。
        _max_output_bytes: 输出截断阈值（字节）。
        _mem_limit_mb: 子进程虚拟地址空间上限（MB），0 表示不限制；不是 RSS 上限。
        _cpu_limit_s: 子进程最大 CPU 时间限制（秒），0 表示不限制。
        _file_size_limit_mb: 子进程能创建的单个文件最大大小（MB），0 表示不限制。
        _nproc_limit: 子进程能创建的最大进程数，0 表示不限制。
        _resource_enabled: 当前平台是否提供 resource 模块；不代表任意限制值都可生效。
    """

    TOOL_NAME = "base__execute_command"

    def __init__(
        self,
        allowed_commands: list[str] | None = None,
        work_dir: str | None = None,
        timeout: int = 30,
        max_output_bytes: int = 100 * 1024,
        mem_limit_mb: int = 512,
        cpu_limit_s: int = 0,
        file_size_limit_mb: int = 10,
        nproc_limit: int = 50,
    ) -> None:
        self._allowed_commands = allowed_commands or []
        self._work_dir = work_dir
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes
        self._mem_limit_mb = mem_limit_mb
        self._cpu_limit_s = cpu_limit_s
        self._file_size_limit_mb = file_size_limit_mb
        self._nproc_limit = nproc_limit
        self._resource_enabled = _HAS_RESOURCE

        self._tool_schemas: dict[str, dict] = {}
        self._tool_handlers: dict[str, Any] = {}
        self._register()
        self._limits_preexec = self._build_preexec()

    def _build_preexec(self):
        """构建 preexec_fn 闭包，在子进程 fork 后、exec 前施加资源限制。

        仅在支持 resource 模块的平台（Linux/macOS）生效，Windows 返回 None。
        任何 setrlimit 失败均向 Popen 传播，使其在 exec 前终止子进程。
        macOS 会拒绝小于当前虚拟地址空间的 RLIMIT_AS；不能将拒绝视为成功，
        也不能用 RLIMIT_RSS 替代（Darwin 中两者是别名）。
        若所有限制均为 0 也返回 None，避免空函数调用开销。

        Returns:
            可选的 preexec 函数，或不支持/不必要时返回 None。
        """
        if not self._resource_enabled:
            return None

        mem_limit_mb = self._mem_limit_mb
        cpu_limit_s = self._cpu_limit_s
        file_size_limit_mb = self._file_size_limit_mb
        nproc_limit = self._nproc_limit

        if mem_limit_mb <= 0 and cpu_limit_s <= 0 and file_size_limit_mb <= 0 and nproc_limit <= 0:
            return None

        def _apply_limits() -> None:
            # Do not log or recover here: this runs after fork, before exec.
            # Ignoring a rejected limit would run the command without its contract.
            if mem_limit_mb > 0:
                limit_bytes = mem_limit_mb * 1024 * 1024
                _resource_module.setrlimit(_resource_module.RLIMIT_AS, (limit_bytes, limit_bytes))

            if cpu_limit_s > 0:
                _resource_module.setrlimit(_resource_module.RLIMIT_CPU, (cpu_limit_s, cpu_limit_s))

            if file_size_limit_mb > 0:
                limit_bytes = file_size_limit_mb * 1024 * 1024
                _resource_module.setrlimit(_resource_module.RLIMIT_FSIZE, (limit_bytes, limit_bytes))

            if nproc_limit > 0:
                _resource_module.setrlimit(_resource_module.RLIMIT_NPROC, (nproc_limit, nproc_limit))

        return _apply_limits

    def _register(self) -> None:
        self._tool_schemas[self.TOOL_NAME] = {
            "name": self.TOOL_NAME,
            "description": (
                "在受控的 shell 环境中执行一条命令。"
                "命令必须是一个单独的可执行文件调用（不支持管道、重定向、命令链等 shell 特性）。"
                "输出将被截断至 {} KB。".format(self._max_output_bytes // 1024)
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的完整命令，例如 'ls -la /tmp' 或 'python script.py'。仅支持调用单个可执行文件及其参数。",
                    }
                },
                "required": ["command"],
            },
        }
        self._tool_handlers[self.TOOL_NAME] = self._make_handler()

    def _make_handler(self):
        def handler(command: str = "", **kwargs: Any) -> str:
            token = kwargs.get("_agent_cancel_token")
            if token is not None and token.is_cancelled():
                return "Error: Task cancelled"
            requested_timeout = kwargs.get("_agent_timeout_s")
            timeout_s = min(self._timeout, float(requested_timeout)) if requested_timeout else self._timeout
            return self._execute(command, timeout_s=timeout_s)

        return handler

    def _execute(self, command: str, timeout_s: float | None = None) -> str:
        if not command or not command.strip():
            return "Error: 命令为空"

        command = command.strip()
        logger.info("Executing command: %s", command)

        validation_error = self._validate(command)
        if validation_error:
            logger.warning("Command rejected: %s — %s", command, validation_error)
            return f"Error: 命令被安全策略拒绝 — {validation_error}"

        try:
            tokens = self._split_command(command)
        except ValueError as e:
            logger.warning("Command parse error: %s — %s", command, e)
            return f"Error: 命令解析失败 — {e}"

        if not tokens:
            return "Error: 命令解析结果为空"

        requested_limits = {
            "RLIMIT_AS(MB)": self._mem_limit_mb,
            "RLIMIT_CPU(s)": self._cpu_limit_s,
            "RLIMIT_FSIZE(MB)": self._file_size_limit_mb,
            "RLIMIT_NPROC": self._nproc_limit,
        }
        limit_description = ", ".join(f"{name}={value}" for name, value in requested_limits.items() if value > 0)
        if limit_description and not self._resource_enabled:
            return f"Error: 当前平台不支持所请求的资源限制（{limit_description}）；命令未执行"

        try:
            result = subprocess.run(
                tokens,
                capture_output=True,
                timeout=timeout_s or self._timeout,
                cwd=self._work_dir,
                preexec_fn=self._limits_preexec,
            )
        except subprocess.TimeoutExpired:
            effective_timeout = timeout_s or self._timeout
            logger.warning("Command timed out (%ss): %s", effective_timeout, command)
            return f"Error: 命令执行超时（>{effective_timeout:g}s）"
        except FileNotFoundError:
            logger.warning("Executable not found: %s", tokens[0])
            return f"Error: 未找到可执行文件 '{tokens[0]}'"
        except PermissionError:
            logger.warning("Permission denied: %s", tokens[0])
            return f"Error: 没有权限执行 '{tokens[0]}'"
        except subprocess.SubprocessError as e:
            if self._limits_preexec is None:
                return f"Error: 命令执行异常 — {e}"
            # Popen intentionally hides the child-side preexec exception details.
            # Report the requested limits, never imply that execution succeeded.
            logger.warning("Resource limit setup failed on %s (%s): %s", sys.platform, limit_description, e)
            return f"Error: 无法应用子进程资源限制（{sys.platform}: {limit_description}）；命令未执行"
        except Exception as e:
            logger.error("Command exception: %s — %s", command, e)
            return f"Error: 命令执行异常 — {e}"

        if result.returncode < 0:
            signal_num = -result.returncode
            signal_reasons = {
                9: "超出内存限制",
                24: "超出 CPU 时间限制",
                25: "超出文件大小限制",
            }
            reason = signal_reasons.get(signal_num, f"收到信号 {signal_num}")
            logger.warning("Command killed by signal %d (%s): %s", signal_num, reason, command)
            return f"Error: 子进程因{reason}而被终止"

        logger.info("Command finished (rc=%d): %s", result.returncode, command)

        output_parts: list[str] = []

        if result.stdout:
            decoded = _safe_decode(result.stdout, self._max_output_bytes)
            output_parts.append(decoded)

        if result.stderr:
            decoded_stderr = _safe_decode(result.stderr, self._max_output_bytes)
            output_parts.append(f"[stderr]\n{decoded_stderr}")

        output = "\n".join(output_parts) if output_parts else "(no output)"

        return output

    def _validate(self, command: str) -> str | None:
        """多层安全校验，返回错误信息或 None 表示通过。"""

        if any(meta in command for meta in SHELL_METACHARACTERS):
            return "命令中包含禁止的 shell 特殊字符（& | ; ` $ > <），不支持命令链/管道/重定向"

        try:
            tokens = self._split_command(command)
        except ValueError:
            return "命令语法错误，无法解析"

        if not tokens:
            return "命令为空"

        executable = tokens[0]

        executable_basename = executable.rsplit("/", 1)[-1] if "/" in executable else executable

        if executable_basename.lower() in BLOCKED_COMMANDS:
            if self._allowed_commands and executable_basename in self._allowed_commands:
                pass
            else:
                return f"禁止执行 '{executable_basename}' 命令，该命令在黑名单中"

        for pattern in DANGEROUS_ARG_PATTERNS:
            if pattern.search(command):
                return f"命令匹配危险模式: {pattern.pattern}"

        if self._allowed_commands and executable_basename not in self._allowed_commands:
            return f"命令 '{executable_basename}' 不在白名单中"

        return None

    @staticmethod
    def _split_command(command: str) -> list[str]:
        tokens = shlex.split(command, posix=os.name != "nt")
        if os.name == "nt":
            tokens = [
                token[1:-1]
                if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}
                else token
                for token in tokens
            ]
        return tokens

    def get_tool_schemas(self) -> list[dict]:
        """获取所有工具的 schema 列表，供 LLM function calling。"""
        return list(self._tool_schemas.values())

    def get_tool_definitions(self) -> dict[str, dict]:
        """获取 tool_name → schema 映射。"""
        return dict(self._tool_schemas)

    def get_tool_handlers(self) -> dict[str, Any]:
        """获取 tool_name → handler 映射。"""
        return dict(self._tool_handlers)
