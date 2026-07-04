from __future__ import annotations

import fnmatch
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ...errors import ErrorCode
from .mcp import MCPToolWrapper
from ...models import SessionConfig
from ..base_tools import ShellTool

logger = logging.getLogger(__name__)


class ToolManager:
    """中央工具注册与调度器，所有 agent 工具调用的统一入口。

    在构造时完成：
    1. 并行连接所有 MCP 服务器（通过 ``MCPToolWrapper``）。
    2. 注册所有远程工具到 handler 字典和 ``tool_map`` 索引。

    后续 runtime / CLI 只需调用 ``list_tools()`` 获取工具列表、
    ``call_many()`` 批量执行工具，无需关心底层 MCP 协议细节。

    Attributes:
        _mcp_wrapper: ``MCPToolWrapper`` 实例，持有 MCP 连接和工具 handler。
        _tool_map: ``{tool_name: (server_name, local_name)}`` 反向索引。
        _handlers: ``{tool_name: callable}`` 工具执行闭包。
    """

    def __init__(self, config: SessionConfig) -> None:
        """初始化工具管理器，连接所有 MCP 服务器并注册工具。

        Args:
            config: 会话配置，包含 ``mcp_servers`` 列表。
        """
        self._config = config
        self._mcp_wrapper = MCPToolWrapper()
        self._shell_tool = ShellTool()
        self._tool_map: dict[str, tuple[str, str]] = {}
        self._handlers: dict[str, Any] = {}

        self._mcp_wrapper.connect_all(config.mcp_servers)
        self._register_mcp_tools()
        self._register_base_tools()

    def _register_mcp_tools(self) -> None:
        """将 MCPToolWrapper 已注册的工具导入本地 handler 字典和反向索引。

        handler 字典供 ``call_tool`` 直接调度；``tool_map`` 维护
        ``tool_name -> (server_name, local_name)`` 的映射，方便调试和查询。
        """
        for tool_name, schema in self._mcp_wrapper.get_tool_definitions().items():
            self._handlers[tool_name] = self._mcp_wrapper.get_tool_handlers()[tool_name]
            parts = tool_name.split("__", 1)
            if len(parts) == 2:
                self._tool_map[tool_name] = (parts[0], parts[1])

    def _register_base_tools(self) -> None:
        """将内置基础工具（如命令行执行）注册到 handler 字典和反向索引。

        与 _register_mcp_tools 采用相同的数据结构，确保 base tools
        和 MCP tools 能够被 ToolManager 统一调度。
        """
        for tool_name, schema in self._shell_tool.get_tool_definitions().items():
            self._handlers[tool_name] = self._shell_tool.get_tool_handlers()[tool_name]
            parts = tool_name.split("__", 1)
            if len(parts) == 2:
                self._tool_map[tool_name] = (parts[0], parts[1])

    def list_tools(self) -> list[dict[str, Any]]:
        """获取所有可用工具的 schema 列表，供 LLM function calling 使用。

        Returns:
            工具描述列表，每个元素包含 ``name``、``description``、``input_schema`` 字段。
        """
        return self._mcp_wrapper.get_tool_schemas() + self._shell_tool.get_tool_schemas()

    @staticmethod
    def is_tool_allowed(tool_name: str, allowlist: list[str]) -> bool:
        """检查工具名是否命中白名单（fnmatch 通配符匹配）。

        空白名单表示全部允许。

        Args:
            tool_name: 全局工具名，例如 ``"sql-search__execute_query"``。
            allowlist: 通配符模式列表，如 ``["sql-*", "graph__*"]``。

        Returns:
            白名单为空或 tool_name 匹配任一模式时为 ``True``。
        """
        if not allowlist:
            return True
        return any(fnmatch.fnmatch(tool_name, pattern) for pattern in allowlist)

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_s: int,
        retries: int,
    ) -> dict[str, Any]:
        """执行单个工具调用，支持自动重试。

        Args:
            tool_name: 全局工具名。
            arguments: 工具参数键值对。
            timeout_s: 单次调用超时（秒），当前由底层 handler 的 ``future.result(timeout=60)`` 控制。
            retries: 失败后的额外重试次数（总尝试次数 = retries + 1）。

        Returns:
            - 成功: ``{"ok": True, "content": ...}``
            - 未知工具: ``{"ok": False, "error_code": "TOOL_UNAVAILABLE", "error": "Unknown tool"}``
            - 全部重试失败: ``{"ok": False, "error_code": "MCP_PROTOCOL_ERROR", "error": "..."}``
        """
        if tool_name not in self._handlers:
            return {
                "ok": False,
                "error_code": ErrorCode.TOOL_UNAVAILABLE.value,
                "error": "Unknown tool",
            }

        handler = self._handlers[tool_name]
        attempts = retries + 1
        last_error = ""

        for _ in range(attempts):
            try:
                raw = handler(**arguments)
                if isinstance(raw, str) and raw.startswith("Error:"):
                    last_error = raw
                    continue
                return {"ok": True, "content": raw}
            except Exception as exc:
                last_error = str(exc)

        return {
            "ok": False,
            "error_code": ErrorCode.MCP_PROTOCOL_ERROR.value,
            "error": last_error,
        }

    def call_many(
        self,
        calls: list[dict[str, Any]],
        timeout_s: int,
        retries: int,
        execution_mode: str = "sequential",
    ) -> list[dict[str, Any]]:
        """批量执行工具调用，支持顺序和并行两种模式。

        并行模式使用 ``ThreadPoolExecutor`` 并发调度，适合互不依赖的工具调用。

        Args:
            calls: 工具调用列表，每个元素包含 ``id``、``name``、``input`` 字段。
            timeout_s: 单个工具调用的超时（秒）。
            retries: 单个工具的额外重试次数。
            execution_mode: ``"sequential"`` 逐个执行，``"parallel"`` 并发执行。

        Returns:
            与输入顺序一致的 ``tool_result`` 列表，每项结构为:
            ``{"type": "tool_result", "tool_use_id": ..., "name": ..., "is_error": bool, "content": ..., "error_code": ..., "error": ...}``
        """
        if execution_mode == "parallel" and len(calls) > 1:
            with ThreadPoolExecutor(max_workers=len(calls)) as executor:
                futures = [
                    executor.submit(
                        self.call_tool,
                        call["name"],
                        call.get("input", {}),
                        timeout_s,
                        retries,
                    )
                    for call in calls
                ]
                results = [future.result() for future in futures]
        else:
            results = [
                self.call_tool(
                    call["name"], call.get("input", {}), timeout_s, retries
                )
                for call in calls
            ]

        mapped: list[dict[str, Any]] = []
        for call, result in zip(calls, results):
            mapped.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "name": call["name"],
                    "is_error": not result.get("ok", False),
                    "content": result.get("content")
                    if result.get("ok", False)
                    else None,
                    "error_code": result.get("error_code"),
                    "error": result.get("error"),
                }
            )
        return mapped

    def close(self) -> None:
        """断开所有 MCP 服务器连接，释放资源。

        委托给 ``MCPToolWrapper.disconnect_all()``，依次关闭传输层、
        ClientSession 和专属事件循环。
        """
        self._mcp_wrapper.disconnect_all()
