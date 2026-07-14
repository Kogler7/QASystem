from __future__ import annotations

import asyncio
import json as json_module
import logging
import os
import sys
import threading
import concurrent.futures
from typing import Any

logger = logging.getLogger(__name__)


def _normalize_windows_stdio_command(
    command: str, args: list[str] | None, env: dict[str, str] | None
) -> tuple[str, list[str], dict[str, str]]:
    """Windows 平台下对非可执行文件的命令做 cmd /c 包装。

    Args:
        command: 要执行的命令（如 ``python``、``node``）。
        args: 命令参数列表，可为 None。
        env: 环境变量字典，可为 None。

    Returns:
        ``(command, args, env)`` 三元组。在 Windows 且 command 不以 .exe/.cmd/.bat
        结尾时，会将原 command 塞入 args[0]，command 重写为 ``"cmd"`` 并前置 ``"/c"``。
    """
    if sys.platform == "win32" and command and not command.lower().endswith(
        (".exe", ".cmd", ".bat")
    ):
        args = [command, *(args or [])]
        command = "cmd"
        args = ["/c", *args]
    return command, args or [], env or {}


async def connect_single_server(
    name: str, cfg: dict[str, Any]
) -> tuple[str, Any, Any]:
    """连接单个 MCP 服务器，支持 stdio / sse / streamable_http 三种传输。

    使用 ``AsyncExitStack`` 管理所有异步上下文（传输层 + ClientSession），
    异常时自动释放已分配资源。

    Args:
        name: 服务器名称（用于日志和后续索引）。
        cfg: 服务器配置字典，需包含 ``transport`` 字段：
            - ``"stdio"`` — 需要 ``command``，可选 ``args``、``env``。
            - ``"sse"`` — 需要 ``url``，可选 ``headers``。
            - ``"streamable_http"`` / ``"http"`` — 需要 ``url``，可选 ``headers``。

    Returns:
        ``(name, stack, session)`` 三元组：
        - *stack* — ``AsyncExitStack`` 实例，持有所有传输层上下文。
        - *session* — 已调用 ``initialize()`` 的 ``ClientSession``。
        连接失败时返回 ``(name, None, None)``。
    """
    from contextlib import AsyncExitStack

    from mcp import ClientSession

    stack = AsyncExitStack()
    await stack.__aenter__()

    try:
        transport = (cfg.get("transport") or "streamable_http").strip().lower()

        if transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            command, args, env = _normalize_windows_stdio_command(
                cfg.get("command") or "",
                cfg.get("args", []),
                cfg.get("env"),
            )
            if not command:
                raise ValueError("stdio transport requires 'command' field")
            params = StdioServerParameters(command=command, args=args, env=env)
            read, write = await stack.enter_async_context(stdio_client(params))

        elif transport == "sse":
            import httpx
            from mcp.client.sse import sse_client

            url = (cfg.get("url") or "").strip()
            if not url:
                raise ValueError("sse transport requires 'url' field")

            def _httpx_client_factory(
                headers: dict[str, str] | None = None,
                timeout: httpx.Timeout | None = None,
                auth: httpx.Auth | None = None,
            ) -> httpx.AsyncClient:
                """构造 SSE 专用 httpx 客户端，合并配置和调用方传入的 headers。

                Accept 头固定为 ``application/json, text/event-stream``，满足 SSE 协议要求。
                """
                merged_headers = {
                    "Accept": "application/json, text/event-stream",
                    **(cfg.get("headers") or {}),
                    **(headers or {}),
                }
                return httpx.AsyncClient(
                    headers=merged_headers or None,
                    follow_redirects=True,
                    timeout=timeout,
                    auth=auth,
                )

            read, write = await stack.enter_async_context(
                sse_client(url, httpx_client_factory=_httpx_client_factory)
            )

        elif transport in ("streamable_http", "http"):
            import httpx
            from mcp.client.streamable_http import streamable_http_client

            url = (cfg.get("url") or "").strip()
            if not url:
                raise ValueError(f"{transport} transport requires 'url' field")
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=cfg.get("headers") or None,
                    follow_redirects=True,
                    timeout=None,
                )
            )
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(url, http_client=http_client)
            )

        else:
            logger.warning(
                "MCP server '%s': unknown transport '%s'", name, transport
            )
            await stack.aclose()
            return name, None, None

        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        logger.info("MCP server '%s' connected via %s", name, transport)
        return name, stack, session

    except Exception:
        logger.exception("MCP server '%s': connection failed", name)
        await stack.aclose()
        return name, None, None


class MCPToolWrapper:
    """MCP 工具包装器：管理多 MCP 服务器的连接、工具注册和调用。

    内部维护一个专属的 ``asyncio`` 事件循环（``self._loop``），所有 session 的
    生命周期绑定在该循环上。工具调用通过 ``_make_handler`` 生成的闭包实现，
    确保每次调用复用同一个事件循环，避免跨循环访问导致的挂起。

    Attributes:
        _stacks: ``{server_name: AsyncExitStack}``，持有每个服务器的传输层上下文。
        _sessions: ``{server_name: ClientSession}``，已初始化的 MCP 会话。
        _tool_schemas: ``{tool_name: schema_dict}``，注册的工具描述。
        _tool_handlers: ``{tool_name: callable}``，工具调用闭包。
        _loop: 本 wrapper 专属的事件循环。
    """

    def __init__(self) -> None:
        self._stacks: dict[str, Any] = {}
        self._sessions: dict[str, Any] = {}
        self._tool_schemas: dict[str, dict] = {}
        self._tool_handlers: dict[str, Any] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

    def connect_all(self, servers: list[Any]) -> None:
        """并行连接所有配置的 MCP 服务器。

        为每个服务器创建独立的事件循环，使用 ``asyncio.gather`` 并发握手。
        单个服务器失败不影响其他服务器的连接和工具注册。

        Args:
            servers: ``McpServerConfig`` 列表，每个至少包含 ``name`` 和 ``transport`` 字段。
        """
        if not servers:
            logger.info("No MCP servers configured")
            return

        async def _connect() -> list[Any]:
            async def _connect_one(srv: Any) -> Any:
                cfg: dict[str, Any] = {"transport": srv.transport}
                if srv.url:
                    cfg["url"] = srv.url
                if srv.command:
                    cfg["command"] = srv.command
                if srv.args:
                    cfg["args"] = srv.args
                if srv.env:
                    cfg["env"] = srv.env
                if srv.headers:
                    cfg["headers"] = srv.headers
                if srv.auth_ref:
                    token = os.getenv(srv.auth_ref)
                    if token:
                        cfg.setdefault("headers", {}).setdefault(
                            "Authorization", f"Bearer {token}"
                        )
                try:
                    return await connect_single_server(srv.name, cfg)
                except BaseException as exc:
                    return srv.name, exc

            tasks = [_connect_one(srv) for srv in servers]
            return await asyncio.gather(*tasks)

        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            results = loop.run_until_complete(_connect())
        except Exception:
            loop.close()
            self._loop = None
            raise

        for item in results:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], BaseException):
                name, exc = item
                logger.error("MCP server '%s': connection failed (%s): %s", name, type(exc).__name__, exc)
                continue
            name, stack, session = item
            if stack is None or session is None:
                logger.error("MCP server '%s': connection failed", name)
                continue

            self._stacks[name] = stack
            self._sessions[name] = session

            tools = loop.run_until_complete(session.list_tools())

            for mcp_tool in tools.tools:
                tool_name = f"{name}__{mcp_tool.name}"
                schema = self._convert_schema(tool_name, mcp_tool)
                self._tool_schemas[tool_name] = schema
                self._tool_handlers[tool_name] = self._make_handler(
                    name, mcp_tool.name
                )

            logger.info(
                "MCP server '%s': registered %d tools", name, len(tools.tools)
            )

        self._loop_thread = threading.Thread(
            target=loop.run_forever,
            daemon=True,
            name="agentcode-mcp-loop",
        )
        self._loop_thread.start()

    def _convert_schema(self, tool_name: str, mcp_tool: Any) -> dict:
        """将 MCP SDK 返回的 Tool 对象转换为 agent 内部使用的工具描述格式。

        Args:
            tool_name: 全局工具名，格式为 ``"{server_name}__{local_name}"``。
            mcp_tool: MCP SDK ``list_tools()`` 返回的 ``Tool`` 对象。

        Returns:
            ``{"name": ..., "description": ..., "input_schema": ...}`` 字典。
            ``input_schema`` 来自 Tool 的 ``inputSchema`` 属性。
        """
        schema = {}
        if hasattr(mcp_tool, "inputSchema") and mcp_tool.inputSchema:
            schema = mcp_tool.inputSchema
        return {
            "name": tool_name,
            "description": mcp_tool.description or mcp_tool.name,
            "input_schema": schema,
        }

    def _make_handler(self, server_name: str, tool_name: str):
        """为指定 MCP 工具创建一个可跨线程调用的闭包 handler。

        闭包在调用时通过 ``self._loop`` 执行 ``session.call_tool()`` 协程，
        确保与连接建立时使用同一个事件循环，避免 ``asyncio.run()`` 新建循环
        导致的跨循环挂起问题。

        Args:
            server_name: MCP 服务器名称（如 ``"qasystem-tools"``）。
            tool_name: 该服务器上的本地工具名（原始名，不含 server 前缀）。

        Returns:
            一个签名为 ``(**kwargs) -> str`` 的可调用对象。
        """
        def handler(**kwargs: Any) -> str:
            timeout_s = max(0.1, float(kwargs.pop("_agent_timeout_s", 60)))
            cancel_token = kwargs.pop("_agent_cancel_token", None)
            session = self._sessions.get(server_name)
            if not session:
                return f"Error: MCP server '{server_name}' is not connected"
            try:
                if self._loop and self._loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        session.call_tool(tool_name, kwargs), self._loop
                    )
                    deadline = self._loop.time() + timeout_s
                    while True:
                        if cancel_token is not None and cancel_token.is_cancelled():
                            future.cancel()
                            return "Error: Task cancelled"
                        remaining = deadline - self._loop.time()
                        if remaining <= 0:
                            future.cancel()
                            return f"Error: MCP tool timed out after {timeout_s:g}s"
                        try:
                            result = future.result(timeout=min(0.2, remaining))
                            break
                        except concurrent.futures.TimeoutError:
                            continue
                elif self._loop:
                    result = self._loop.run_until_complete(
                        asyncio.wait_for(session.call_tool(tool_name, kwargs), timeout_s)
                    )
                else:
                    result = asyncio.run(session.call_tool(tool_name, kwargs))
                return _format_mcp_result(result)
            except Exception as e:
                logger.exception(
                    "MCP call '%s.%s' failed", server_name, tool_name
                )
                return f"Error calling MCP tool '{tool_name}': {e}"

        return handler

    def get_tool_schemas(self) -> list[dict]:
        """获取所有已注册工具的 schema 列表，供 LLM function calling 使用。

        Returns:
            工具描述字典列表，每个元素形如
            ``{"name": "server__tool", "description": "...", "input_schema": {...}}``。
        """
        return list(self._tool_schemas.values())

    def get_tool_definitions(self) -> dict[str, dict]:
        """获取工具名 → schema 的映射字典。

        Returns:
            ``{tool_name: schema_dict}`` 的浅拷贝。
        """
        return dict(self._tool_schemas)

    def get_tool_handlers(self) -> dict[str, Any]:
        """获取工具名 → handler 可调用对象的映射字典。

        Returns:
            ``{tool_name: callable}`` 的浅拷贝。
        """
        return dict(self._tool_handlers)

    def get_session(self, server_name: str) -> Any:
        """按服务器名获取已建立的 MCP ClientSession。

        Args:
            server_name: 服务器名称。

        Returns:
            ``ClientSession`` 实例，未连接时返回 ``None``。
        """
        return self._sessions.get(server_name)

    def disconnect_all(self) -> None:
        """断开所有 MCP 服务器连接并释放资源。

        依次关闭每个服务器的 ``AsyncExitStack``（含传输层和 ClientSession），
        最后关闭专属事件循环 ``self._loop``。
        """
        async def _disconnect() -> None:
            for name, stack in list(self._stacks.items()):
                try:
                    await stack.aclose()
                    logger.info("MCP server '%s' disconnected", name)
                except Exception:
                    logger.exception(
                        "Error disconnecting MCP server '%s'", name
                    )

        try:
            if self._loop and self._stacks:
                if self._loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(_disconnect(), self._loop)
                    future.result(timeout=30)
                else:
                    self._loop.run_until_complete(_disconnect())
        except Exception:
            logger.exception("Error during MCP disconnect")
        finally:
            self._stacks.clear()
            self._sessions.clear()
            self._tool_schemas.clear()
            self._tool_handlers.clear()
            if self._loop:
                if self._loop.is_running():
                    self._loop.call_soon_threadsafe(self._loop.stop)
                if self._loop_thread is not None:
                    self._loop_thread.join(timeout=5)
                self._loop.close()
                self._loop = None
                self._loop_thread = None


def _format_mcp_result(result: Any) -> str:
    """将 MCP 工具调用返回的 ``CallToolResult`` 格式化为人类可读字符串。

    处理规则：
    - ``None`` → ``"(no output)"``
    - 有 ``.content`` 属性 → 遍历列表，拼接 ``text`` / JSON(data) / ``[mimeType]``
    - 其他 → ``str(result)``

    Args:
        result: ``session.call_tool()`` 返回的原始结果。

    Returns:
        格式化后的字符串，可直接注入 LLM 对话上下文。
    """
    if result is None:
        return "(no output)"
    if hasattr(result, "content"):
        parts: list[str] = []
        for item in result.content:
            if hasattr(item, "text"):
                parts.append(item.text)
            elif hasattr(item, "data"):
                parts.append(
                    json_module.dumps(item.data, ensure_ascii=False)
                )
            elif hasattr(item, "mimeType"):
                parts.append(f"[{item.mimeType}]")
        return "\n".join(parts) if parts else str(result)
    return str(result)
